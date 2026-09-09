from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from psycopg import connect
from psycopg.rows import dict_row

from .artifact_contracts import ArtifactDraftContent, ExportFormat
from .artifact_dlp import assess_artifact
from .artifact_export_renderer import render_artifact_export
from .governed_domain_core import GovernedFingerprints, GovernedPayloadCodec
from .transactional_outbox import OutboxLease


class ArtifactExportRejected(RuntimeError):
    def __init__(self, safe_error_code: str) -> None:
        super().__init__(safe_error_code)
        self.safe_error_code = safe_error_code


@dataclass(frozen=True)
class ArtifactExportJobLease:
    export_job_id: UUID
    artifact_id: UUID
    version_number: int
    tenant_id: int
    user_id: str
    export_format: ExportFormat
    generation: int
    lease_token: UUID
    lease_expires_at: datetime


class PostgresArtifactExportWorker:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.codec = GovernedPayloadCodec()
        self.fingerprints = GovernedFingerprints.load()

    def process(self, outbox_lease: OutboxLease) -> str:
        if (
            outbox_lease.topic != "ai.artifact.export-requested.v1"
            or outbox_lease.aggregate_type != "ARTIFACT_EXPORT"
        ):
            raise ValueError("The outbox intent is not an artifact export.")
        export_job_id = UUID(outbox_lease.aggregate_id)
        if str(outbox_lease.payload.get("exportJobId")) != str(export_job_id):
            raise ArtifactExportRejected("EXPORT_INTENT_BINDING_INVALID")
        claimed = self._claim(
            export_job_id,
            tenant_id=outbox_lease.tenant_id,
            user_id=outbox_lease.user_id,
        )
        if claimed is None:
            return self._state(export_job_id, outbox_lease.tenant_id)
        try:
            self._execute(claimed)
        except ArtifactExportRejected as error:
            self.fail(claimed, safe_error_code=error.safe_error_code)
        except Exception:
            self.retry(claimed, safe_error_code="EXPORT_WORKER_RETRY")
            raise
        return self._state(export_job_id, outbox_lease.tenant_id)

    def retry(self, lease: ArtifactExportJobLease, *, safe_error_code: str) -> bool:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT attempt_count FROM ai_artifact_export_jobs
                    WHERE export_job_id = %s AND tenant_id = %s
                      AND export_state = 'CLAIMED' AND generation = %s
                      AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP
                    FOR UPDATE""",
                (
                    lease.export_job_id,
                    lease.tenant_id,
                    lease.generation,
                    lease.lease_token,
                ),
            ).fetchone()
            if row is None:
                return False
            terminal = int(row["attempt_count"]) >= 5
            next_state = "FAILED" if terminal else "PENDING"
            updated = connection.execute(
                """UPDATE ai_artifact_export_jobs
                      SET export_state = %s, lease_token = NULL,
                          lease_expires_at = NULL, safe_error_code = %s,
                          completed_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END
                    WHERE export_job_id = %s AND tenant_id = %s
                      AND export_state = 'CLAIMED' AND generation = %s
                      AND lease_token = %s
                    RETURNING export_job_id""",
                (
                    next_state,
                    safe_error_code,
                    terminal,
                    lease.export_job_id,
                    lease.tenant_id,
                    lease.generation,
                    lease.lease_token,
                ),
            ).fetchone()
            if updated is not None:
                self._event(
                    connection,
                    lease,
                    event_type="FAILED" if terminal else "RETRY_SCHEDULED",
                    previous_state="CLAIMED",
                    current_state=next_state,
                    safe_error_code=safe_error_code,
                )
            return terminal

    def fail(self, lease: ArtifactExportJobLease, *, safe_error_code: str) -> None:
        with connect(self.database_url) as connection:
            updated = connection.execute(
                """UPDATE ai_artifact_export_jobs
                      SET export_state = 'FAILED', lease_token = NULL,
                          lease_expires_at = NULL, safe_error_code = %s,
                          completed_at = CURRENT_TIMESTAMP
                    WHERE export_job_id = %s AND tenant_id = %s
                      AND export_state = 'CLAIMED' AND generation = %s
                      AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP
                    RETURNING export_job_id""",
                (
                    safe_error_code,
                    lease.export_job_id,
                    lease.tenant_id,
                    lease.generation,
                    lease.lease_token,
                ),
            ).fetchone()
            if updated is None:
                raise RuntimeError("The artifact export lease is stale or expired.")
            self._event(
                connection,
                lease,
                event_type="FAILED",
                previous_state="CLAIMED",
                current_state="FAILED",
                safe_error_code=safe_error_code,
            )

    def _claim(
        self, export_job_id: UUID, *, tenant_id: int, user_id: str
    ) -> ArtifactExportJobLease | None:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT export_job_id, artifact_id, version_number, tenant_id,
                          user_id, export_format, export_state, generation
                     FROM ai_artifact_export_jobs
                    WHERE export_job_id = %s AND tenant_id = %s AND user_id = %s
                    FOR UPDATE""",
                (export_job_id, tenant_id, user_id),
            ).fetchone()
            if row is None:
                raise ArtifactExportRejected("EXPORT_JOB_NOT_FOUND")
            if row["export_state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return None
            token = uuid4()
            generation = int(row["generation"]) + 1
            claimed = connection.execute(
                """UPDATE ai_artifact_export_jobs
                      SET export_state = 'CLAIMED', generation = %s,
                          lease_token = %s,
                          lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '120 seconds',
                          attempt_count = attempt_count + 1, safe_error_code = NULL
                    WHERE export_job_id = %s
                      AND (export_state = 'PENDING'
                        OR (export_state = 'CLAIMED'
                            AND lease_expires_at <= CURRENT_TIMESTAMP))
                    RETURNING lease_expires_at""",
                (generation, token, export_job_id),
            ).fetchone()
            if claimed is None:
                raise RuntimeError("The artifact export job already has a live lease.")
            lease = ArtifactExportJobLease(
                export_job_id=export_job_id,
                artifact_id=row["artifact_id"],
                version_number=int(row["version_number"]),
                tenant_id=tenant_id,
                user_id=user_id,
                export_format=ExportFormat(row["export_format"]),
                generation=generation,
                lease_token=token,
                lease_expires_at=claimed["lease_expires_at"],
            )
            self._event(
                connection,
                lease,
                event_type="CLAIMED",
                previous_state=row["export_state"],
                current_state="CLAIMED",
            )
            return lease

    def _execute(self, lease: ArtifactExportJobLease) -> None:
        with connect(self.database_url, row_factory=dict_row) as connection:
            job = connection.execute(
                """SELECT x.preflight_id, x.retention_until, a.artifact_state,
                          a.published_version_number, v.content_envelope,
                          v.content_fingerprint, CURRENT_TIMESTAMP AS database_now
                     FROM ai_artifact_export_jobs x
                     JOIN ai_artifacts a ON a.artifact_id = x.artifact_id
                     JOIN ai_artifact_versions v
                       ON v.artifact_id = x.artifact_id
                      AND v.version_number = x.version_number
                    WHERE x.export_job_id = %s AND x.tenant_id = %s
                      AND x.user_id = %s AND x.export_state = 'CLAIMED'
                      AND x.generation = %s AND x.lease_token = %s
                      AND x.lease_expires_at > CURRENT_TIMESTAMP
                    FOR UPDATE OF x""",
                (
                    lease.export_job_id,
                    lease.tenant_id,
                    lease.user_id,
                    lease.generation,
                    lease.lease_token,
                ),
            ).fetchone()
            if job is None:
                raise RuntimeError("The artifact export lease is stale or expired.")
            if job["retention_until"] <= job["database_now"]:
                raise ArtifactExportRejected("EXPORT_RETENTION_EXPIRED")
            if (
                job["artifact_state"] != "PUBLISHED"
                or int(job["published_version_number"] or 0) != lease.version_number
            ):
                raise ArtifactExportRejected("PUBLISHED_VERSION_CHANGED")
            preflight = connection.execute(
                """SELECT outcome, content_fingerprint, expires_at
                     FROM ai_artifact_preflight_runs
                    WHERE preflight_id = %s AND artifact_id = %s
                      AND version_number = %s AND tenant_id = %s AND user_id = %s""",
                (
                    job["preflight_id"],
                    lease.artifact_id,
                    lease.version_number,
                    lease.tenant_id,
                    lease.user_id,
                ),
            ).fetchone()
            if (
                preflight is None
                or preflight["outcome"] != "PASS"
                or preflight["content_fingerprint"] != job["content_fingerprint"]
                or preflight["expires_at"] <= job["database_now"]
            ):
                raise ArtifactExportRejected("PREFLIGHT_PROOF_INVALID")
            source_counts = connection.execute(
                """SELECT COUNT(*) AS source_count, COUNT(*) FILTER (
                             WHERE verification_state = 'SERVER_VERIFIED'
                               AND verified_at IS NOT NULL
                               AND verification_evidence_fingerprint IS NOT NULL)
                               AS verified_count
                     FROM ai_artifact_version_sources
                    WHERE artifact_id = %s AND version_number = %s""",
                (lease.artifact_id, lease.version_number),
            ).fetchone()
            source_count = int(source_counts["source_count"])
            verified_count = int(source_counts["verified_count"])
            all_sources_verified = source_count == verified_count
            content = ArtifactDraftContent.model_validate(
                self.codec.decrypt_json(
                    job["content_envelope"],
                    tenant_id=lease.tenant_id,
                    resource_type="artifact-version",
                    resource_id=f"{lease.artifact_id}:{lease.version_number}",
                    field="content",
                )
            )
            assessment = assess_artifact(
                content, all_sources_verified=all_sources_verified
            )
            if assessment.outcome.value != "PASS":
                raise ArtifactExportRejected(
                    "SOURCE_NOT_VERIFIED"
                    if not all_sources_verified
                    else "DLP_PREFLIGHT_BLOCKED"
                )
            rendered = render_artifact_export(content, lease.export_format)
            content_sha256 = hashlib.sha256(rendered.content).hexdigest()
            fingerprint = self.fingerprints.value(
                tenant_id=lease.tenant_id,
                purpose="artifact-export-content",
                payload={
                    "exportJobId": str(lease.export_job_id),
                    "sha256": content_sha256,
                    "byteSize": len(rendered.content),
                },
            )
            file_name = (
                f"artifact-{lease.artifact_id}-v{lease.version_number}."
                f"{rendered.extension}"
            )
            content_envelope = self.codec.encrypt_json(
                {"base64": base64.b64encode(rendered.content).decode("ascii")},
                tenant_id=lease.tenant_id,
                resource_type="artifact-export-output",
                resource_id=str(lease.export_job_id),
                field="content",
            )
            manifest = {
                "schemaVersion": "dwaion-artifact-export-manifest-v1",
                "exportJobId": str(lease.export_job_id),
                "artifactId": str(lease.artifact_id),
                "versionNumber": lease.version_number,
                "format": lease.export_format.value,
                "mediaType": rendered.media_type,
                "fileName": file_name,
                "byteSize": len(rendered.content),
                "sha256": content_sha256,
                "dlpPolicy": "DWP_DETERMINISTIC_DLP_V1",
                "sourceCount": int(source_count),
                "verifiedSourceCount": int(verified_count),
            }
            manifest_envelope = self.codec.encrypt_json(
                manifest,
                tenant_id=lease.tenant_id,
                resource_type="artifact-export-output",
                resource_id=str(lease.export_job_id),
                field="manifest",
            )
            connection.execute(
                """INSERT INTO ai_artifact_export_outputs (
                       export_job_id, tenant_id, user_id, media_type, file_name,
                       byte_size, content_fingerprint, content_envelope,
                       manifest_envelope, retention_until)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    lease.export_job_id,
                    lease.tenant_id,
                    lease.user_id,
                    rendered.media_type,
                    file_name,
                    len(rendered.content),
                    fingerprint,
                    content_envelope,
                    manifest_envelope,
                    job["retention_until"],
                ),
            )
            reference_envelope = self.codec.encrypt_json(
                {
                    "outputStore": "POSTGRES_ENCRYPTED",
                    "contentFingerprint": fingerprint,
                    "fileName": file_name,
                },
                tenant_id=lease.tenant_id,
                resource_type="artifact-export-job",
                resource_id=str(lease.export_job_id),
                field="output-reference",
            )
            updated = connection.execute(
                """UPDATE ai_artifact_export_jobs
                      SET export_state = 'SUCCEEDED', lease_token = NULL,
                          lease_expires_at = NULL,
                          output_reference_envelope = %s,
                          safe_error_code = NULL, completed_at = CURRENT_TIMESTAMP
                    WHERE export_job_id = %s AND tenant_id = %s
                      AND export_state = 'CLAIMED' AND generation = %s
                      AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP
                    RETURNING export_job_id""",
                (
                    reference_envelope,
                    lease.export_job_id,
                    lease.tenant_id,
                    lease.generation,
                    lease.lease_token,
                ),
            ).fetchone()
            if updated is None:
                raise RuntimeError("The artifact export lease was lost before commit.")
            self._event(
                connection,
                lease,
                event_type="SUCCEEDED",
                previous_state="CLAIMED",
                current_state="SUCCEEDED",
                content_fingerprint=fingerprint,
            )

    def _state(self, export_job_id: UUID, tenant_id: int) -> str:
        with connect(self.database_url) as connection:
            row = connection.execute(
                """SELECT export_state FROM ai_artifact_export_jobs
                    WHERE export_job_id = %s AND tenant_id = %s""",
                (export_job_id, tenant_id),
            ).fetchone()
        if row is None:
            raise ArtifactExportRejected("EXPORT_JOB_NOT_FOUND")
        return str(row[0])

    @staticmethod
    def _event(
        connection: object,
        lease: ArtifactExportJobLease,
        *,
        event_type: str,
        previous_state: str | None,
        current_state: str,
        content_fingerprint: str | None = None,
        safe_error_code: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO ai_artifact_export_events (
                   event_id, export_job_id, tenant_id, user_id, event_type,
                   previous_state, current_state, generation,
                   content_fingerprint, safe_error_code)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(),
                lease.export_job_id,
                lease.tenant_id,
                lease.user_id,
                event_type,
                previous_state,
                current_state,
                lease.generation,
                content_fingerprint,
                safe_error_code,
            ),
        )
