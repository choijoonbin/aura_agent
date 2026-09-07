from __future__ import annotations

from datetime import timedelta
from functools import wraps
from typing import Any, Callable, TypeVar
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .artifact_contracts import (
    ArtifactExportReceipt,
    ArtifactPreflightReceipt,
    ArtifactPublicationReceipt,
    ArtifactState,
    ArtifactVersionReceipt,
    AutosaveArtifactRequest,
    CreateArtifactRequest,
    CreateArtifactVersionRequest,
    ExportArtifactRequest,
    GovernedArtifact,
    PublishArtifactRequest,
    RunArtifactPreflightRequest,
)
from .artifact_dlp import assess_artifact
from .artifact_postgres_base import ArtifactPostgresBase, _ARTIFACT_SELECT
from .artifact_read_queries import ArtifactReadQueries
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
    retention_deadline,
)
from .personal_domain_security import PersonalDomainIdentity
from .transactional_outbox import enqueue_internal_intent


T = TypeVar("T")


def _translated(function: Callable[..., T]) -> Callable[..., T]:
    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> T:
        try:
            return function(*args, **kwargs)
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable("Governed artifacts are unavailable.") from error

    return wrapped


class PostgresArtifactStore(ArtifactReadQueries, ArtifactPostgresBase):
    list_versions = _translated(ArtifactReadQueries.list_versions)
    get_version = _translated(ArtifactReadQueries.get_version)
    current_preflight = _translated(ArtifactReadQueries.current_preflight)

    @_translated
    def list(self, identity: PersonalDomainIdentity) -> list[GovernedArtifact]:
        with connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                _ARTIFACT_SELECT
                + " WHERE a.tenant_id = %s AND a.user_id = %s"
                + " ORDER BY a.updated_at DESC",
                (identity.tenant_id, identity.user_id),
            ).fetchall()
            return [self._artifact(connection, row) for row in rows]

    @_translated
    def get(
        self, identity: PersonalDomainIdentity, artifact_id: UUID
    ) -> GovernedArtifact:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = self._locked_artifact(connection, identity, artifact_id, lock=False)
            return self._artifact(connection, row)

    @_translated
    def create(
        self, identity: PersonalDomainIdentity, request: CreateArtifactRequest
    ) -> GovernedArtifact:
        self._require_source_access(identity, request.sources)
        if request.expected_revision != 0:
            raise GovernedDomainConflict("An artifact must start at revision zero.")
        proof = self._proof(identity, "CREATE", None, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "CREATE", proof)
            if replay is not None:
                return GovernedArtifact.model_validate(replay)
            days = self._retention_days(connection, identity.tenant_id, "ARTIFACT")
            now = connection.execute(
                "SELECT CURRENT_TIMESTAMP AS now"
            ).fetchone()["now"]
            artifact_id = uuid4()
            payload = request.content.model_dump(mode="json", by_alias=True)
            envelope = self.codec.encrypt_json(
                payload,
                tenant_id=identity.tenant_id,
                resource_type="artifact-draft",
                resource_id=str(artifact_id),
                field="content",
            )
            fingerprint = self.fingerprints.value(
                tenant_id=identity.tenant_id,
                purpose="artifact-content",
                payload=payload,
            )
            connection.execute(
                """INSERT INTO ai_artifacts (
                       artifact_id, tenant_id, user_id, artifact_type,
                       retention_until, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    artifact_id,
                    identity.tenant_id,
                    identity.user_id,
                    request.artifact_type.value,
                    retention_deadline(now, days),
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO ai_artifact_drafts (
                       artifact_id, tenant_id, user_id, draft_revision,
                       content_envelope, content_fingerprint, updated_by_user_id,
                       updated_at)
                   VALUES (%s, %s, %s, 1, %s, %s, %s, %s)""",
                (
                    artifact_id,
                    identity.tenant_id,
                    identity.user_id,
                    envelope,
                    fingerprint,
                    identity.user_id,
                    now,
                ),
            )
            self._replace_draft_sources(
                connection, identity, artifact_id, request.sources
            )
            result = self._artifact(
                connection,
                self._locked_artifact(connection, identity, artifact_id, lock=False),
            )
            self._record_command(
                connection, identity, artifact_id, "CREATE", request, proof, result
            )
            self._event(connection, identity, artifact_id, request.command_id, "CREATED", None, result.state.value, 1, proof.request_fingerprint, request.reason_code, None)
            return result

    @_translated
    def autosave(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        request: AutosaveArtifactRequest,
    ) -> GovernedArtifact:
        self._require_source_access(identity, request.sources)
        proof = self._proof(identity, "AUTOSAVE", artifact_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "AUTOSAVE", proof)
            if replay is not None:
                return GovernedArtifact.model_validate(replay)
            row = self._locked_artifact(connection, identity, artifact_id)
            self._expected(row, request.expected_revision)
            payload = request.content.model_dump(mode="json", by_alias=True)
            envelope = self.codec.encrypt_json(
                payload,
                tenant_id=identity.tenant_id,
                resource_type="artifact-draft",
                resource_id=str(artifact_id),
                field="content",
            )
            fingerprint = self.fingerprints.value(
                tenant_id=identity.tenant_id,
                purpose="artifact-content",
                payload=payload,
            )
            revision = int(row["revision"]) + 1
            draft_revision = int(row["draft_revision"]) + 1
            base_version = int(row["current_version_number"]) or None
            connection.execute(
                """UPDATE ai_artifact_drafts
                      SET draft_revision = %s, base_version_number = %s,
                          content_envelope = %s, content_fingerprint = %s,
                          updated_by_user_id = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE artifact_id = %s AND tenant_id = %s AND user_id = %s""",
                (draft_revision, base_version, envelope, fingerprint, identity.user_id, artifact_id, identity.tenant_id, identity.user_id),
            )
            connection.execute(
                """UPDATE ai_artifacts
                      SET artifact_state = 'DRAFT', revision = %s,
                          current_draft_revision = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE artifact_id = %s AND tenant_id = %s AND user_id = %s""",
                (revision, draft_revision, artifact_id, identity.tenant_id, identity.user_id),
            )
            self._replace_draft_sources(connection, identity, artifact_id, request.sources)
            result = self._artifact(connection, self._locked_artifact(connection, identity, artifact_id, lock=False))
            self._record_command(connection, identity, artifact_id, "AUTOSAVE", request, proof, result)
            self._event(connection, identity, artifact_id, request.command_id, "AUTOSAVED", row["artifact_state"], result.state.value, revision, proof.request_fingerprint, request.reason_code, None)
            return result

    @_translated
    def create_version(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        request: CreateArtifactVersionRequest,
    ) -> ArtifactVersionReceipt:
        proof = self._proof(identity, "VERSION", artifact_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "VERSION", proof)
            if replay is not None:
                return ArtifactVersionReceipt.model_validate(replay)
            row = self._locked_artifact(connection, identity, artifact_id)
            self._expected(row, request.expected_revision)
            sources = self._draft_sources(connection, identity.tenant_id, artifact_id)
            self._require_source_access(identity, sources)
            content = self._artifact(connection, row).content
            version = int(row["current_version_number"]) + 1
            version_key = f"{artifact_id}:{version}"
            content_payload = content.model_dump(mode="json", by_alias=True)
            envelope = self.codec.encrypt_json(
                content_payload,
                tenant_id=identity.tenant_id,
                resource_type="artifact-version",
                resource_id=version_key,
                field="content",
            )
            fingerprint = self.fingerprints.value(
                tenant_id=identity.tenant_id,
                purpose="artifact-content",
                payload=content_payload,
            )
            now = connection.execute(
                "SELECT CURRENT_TIMESTAMP AS now"
            ).fetchone()["now"]
            connection.execute(
                """INSERT INTO ai_artifact_versions (
                       artifact_id, version_number, tenant_id, user_id,
                       content_envelope, content_fingerprint, source_count,
                       created_by_user_id, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (artifact_id, version, identity.tenant_id, identity.user_id, envelope, fingerprint, len(sources), identity.user_id, now),
            )
            for source in sources:
                self._insert_version_source(connection, identity, artifact_id, version, source)
            revision = int(row["revision"]) + 1
            connection.execute(
                """UPDATE ai_artifacts
                      SET current_version_number = %s, revision = %s,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE artifact_id = %s AND tenant_id = %s AND user_id = %s""",
                (version, revision, artifact_id, identity.tenant_id, identity.user_id),
            )
            result = ArtifactVersionReceipt(
                artifact_id=artifact_id,
                artifact_revision=revision,
                version_number=version,
                content_fingerprint=fingerprint,
                source_count=len(sources),
                created_at=now,
            )
            self._record_command(connection, identity, artifact_id, "VERSION", request, proof, result)
            self._event(connection, identity, artifact_id, request.command_id, "VERSION_CREATED", row["artifact_state"], row["artifact_state"], revision, proof.request_fingerprint, request.reason_code, None)
            return result

    @_translated
    def preflight(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        request: RunArtifactPreflightRequest,
    ) -> ArtifactPreflightReceipt:
        proof = self._proof(identity, "PREFLIGHT", artifact_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "PREFLIGHT", proof)
            if replay is not None:
                return ArtifactPreflightReceipt.model_validate(replay)
            artifact = self._locked_artifact(connection, identity, artifact_id)
            self._expected(artifact, request.expected_revision)
            if request.version_number != int(artifact["current_version_number"]):
                raise GovernedDomainConflict("Preflight requires the current immutable version.")
            version = self._version_row(connection, identity, artifact_id, request.version_number)
            sources = self._version_sources(connection, identity, artifact_id, request.version_number)
            self._require_source_access(identity, sources)
            assessment = assess_artifact(
                self._version_content(version), all_sources_verified=not sources
            )
            preflight_id = uuid4()
            now = connection.execute(
                "SELECT CURRENT_TIMESTAMP AS now"
            ).fetchone()["now"]
            expires_at = now + timedelta(minutes=15)
            findings_payload = {
                "findings": [item.model_dump(mode="json", by_alias=True) for item in assessment.findings]
            }
            findings_envelope = self.codec.encrypt_json(
                findings_payload,
                tenant_id=identity.tenant_id,
                resource_type="artifact-preflight",
                resource_id=str(preflight_id),
                field="findings",
            )
            next_state = (
                artifact["artifact_state"]
                if assessment.outcome.value == "PASS"
                else ArtifactState.REVIEW_REQUIRED.value
            )
            revision = int(artifact["revision"]) + 1
            connection.execute(
                """INSERT INTO ai_artifact_preflight_runs (
                       preflight_id, artifact_id, version_number, tenant_id, user_id,
                       command_id, artifact_revision, content_fingerprint,
                       policy_key, policy_version,
                       outcome, findings_envelope, created_at, expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                           'DWP_DETERMINISTIC_DLP_V1', 1, %s, %s, %s, %s)""",
                (preflight_id, artifact_id, request.version_number, identity.tenant_id, identity.user_id, request.command_id, revision, version["content_fingerprint"], assessment.outcome.value, findings_envelope, now, expires_at),
            )
            connection.execute(
                """UPDATE ai_artifacts SET artifact_state = %s, revision = %s,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE artifact_id = %s AND tenant_id = %s AND user_id = %s""",
                (next_state, revision, artifact_id, identity.tenant_id, identity.user_id),
            )
            allowed = assessment.outcome.value == "PASS"
            result = ArtifactPreflightReceipt(
                preflight_id=preflight_id,
                artifact_id=artifact_id,
                artifact_revision=revision,
                version_number=request.version_number,
                outcome=assessment.outcome,
                findings=assessment.findings,
                evaluated_at=now,
                expires_at=expires_at,
                publish_allowed=allowed,
                export_allowed=allowed,
            )
            self._record_command(connection, identity, artifact_id, "PREFLIGHT", request, proof, result)
            self._event(connection, identity, artifact_id, request.command_id, "PREFLIGHT_COMPLETED", artifact["artifact_state"], next_state, revision, proof.request_fingerprint, request.reason_code, None)
            return result

    @_translated
    def publish(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        request: PublishArtifactRequest,
    ) -> ArtifactPublicationReceipt:
        proof = self._proof(identity, "PUBLISH", artifact_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "PUBLISH", proof)
            if replay is not None:
                return ArtifactPublicationReceipt.model_validate(replay)
            artifact = self._locked_artifact(connection, identity, artifact_id)
            self._expected(artifact, request.expected_revision)
            self._require_current_version(artifact, request.version_number)
            version = self._version_row(connection, identity, artifact_id, request.version_number)
            sources = self._version_sources(connection, identity, artifact_id, request.version_number)
            self._require_source_access(identity, sources)
            self._require_passing_preflight(connection, identity, version, request.preflight_id)
            revision = int(artifact["revision"]) + 1
            connection.execute(
                """UPDATE ai_artifacts
                      SET artifact_state = 'PUBLISHED', published_version_number = %s,
                          revision = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE artifact_id = %s AND tenant_id = %s AND user_id = %s""",
                (request.version_number, revision, artifact_id, identity.tenant_id, identity.user_id),
            )
            result = ArtifactPublicationReceipt(
                artifact_id=artifact_id,
                artifact_revision=revision,
                published_version_number=request.version_number,
            )
            self._record_command(connection, identity, artifact_id, "PUBLISH", request, proof, result)
            self._event(connection, identity, artifact_id, request.command_id, "PUBLISHED", artifact["artifact_state"], ArtifactState.PUBLISHED.value, revision, proof.request_fingerprint, request.reason_code, request.change_reason)
            return result

    @_translated
    def export(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        request: ExportArtifactRequest,
    ) -> ArtifactExportReceipt:
        proof = self._proof(identity, "EXPORT", artifact_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(connection, identity, request.command_id, "EXPORT", proof)
            if replay is not None:
                return ArtifactExportReceipt.model_validate(replay)
            artifact = self._locked_artifact(connection, identity, artifact_id)
            self._expected(artifact, request.expected_revision)
            if artifact["artifact_state"] != ArtifactState.PUBLISHED.value or artifact["published_version_number"] != request.version_number:
                raise GovernedDomainConflict("Only the current published artifact version can be exported.")
            version = self._version_row(connection, identity, artifact_id, request.version_number)
            sources = self._version_sources(connection, identity, artifact_id, request.version_number)
            self._require_source_access(identity, sources)
            self._require_passing_preflight(connection, identity, version, request.preflight_id)
            days = self._retention_days(connection, identity.tenant_id, "ARTIFACT_EXPORT")
            now = connection.execute(
                "SELECT CURRENT_TIMESTAMP AS now"
            ).fetchone()["now"]
            export_job_id = uuid4()
            deadline = retention_deadline(now, days)
            revision = int(artifact["revision"]) + 1
            connection.execute(
                """INSERT INTO ai_artifact_export_jobs (
                       export_job_id, artifact_id, version_number, preflight_id,
                       tenant_id, user_id, command_id, export_format,
                       retention_until)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (export_job_id, artifact_id, request.version_number, request.preflight_id, identity.tenant_id, identity.user_id, request.command_id, request.export_format.value, deadline),
            )
            connection.execute(
                """UPDATE ai_artifacts SET revision = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE artifact_id = %s AND tenant_id = %s AND user_id = %s""",
                (revision, artifact_id, identity.tenant_id, identity.user_id),
            )
            enqueue_internal_intent(
                connection,
                codec=self.codec,
                fingerprints=self.fingerprints,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                topic="ai.artifact.export-requested.v1",
                aggregate_type="ARTIFACT_EXPORT",
                aggregate_id=str(export_job_id),
                payload={
                    "exportJobId": str(export_job_id),
                    "artifactId": str(artifact_id),
                    "versionNumber": request.version_number,
                    "format": request.export_format.value,
                    "externalWritePerformed": False,
                },
                retention_until=deadline,
            )
            result = ArtifactExportReceipt(
                export_job_id=export_job_id,
                artifact_id=artifact_id,
                artifact_revision=revision,
                version_number=request.version_number,
                export_format=request.export_format,
            )
            self._record_command(connection, identity, artifact_id, "EXPORT", request, proof, result)
            self._event(connection, identity, artifact_id, request.command_id, "EXPORT_REQUESTED", artifact["artifact_state"], artifact["artifact_state"], revision, proof.request_fingerprint, request.reason_code, request.change_reason)
            return result

    def _insert_version_source(self, connection: Any, identity: PersonalDomainIdentity, artifact_id: UUID, version: int, source: Any) -> None:
        source_link_id = uuid4()
        payload = source.model_dump(mode="json", by_alias=True)
        fingerprint = self.fingerprints.value(tenant_id=identity.tenant_id, purpose="artifact-source-reference", payload=payload)
        envelope = self.codec.encrypt_json(
            payload, tenant_id=identity.tenant_id, resource_type="artifact-version-source",
            resource_id=str(source_link_id), field="reference",
        )
        connection.execute(
            """INSERT INTO ai_artifact_version_sources (
                   source_link_id, artifact_id, version_number, source_type,
                   reference_fingerprint, reference_envelope, verification_state)
               VALUES (%s, %s, %s, %s, %s, %s, 'UNVERIFIED')""",
            (source_link_id, artifact_id, version, source.source_type.value, fingerprint, envelope),
        )

    @staticmethod
    def _require_current_version(artifact: Any, version_number: int) -> None:
        if int(artifact["current_version_number"]) != version_number:
            raise GovernedDomainConflict("The immutable artifact version is no longer current.")

    @staticmethod
    def _require_passing_preflight(connection: Any, identity: PersonalDomainIdentity, version: Any, preflight_id: UUID) -> None:
        row = connection.execute(
            """SELECT outcome, content_fingerprint, expires_at
                 FROM ai_artifact_preflight_runs
                WHERE preflight_id = %s AND artifact_id = %s AND version_number = %s
                  AND tenant_id = %s AND user_id = %s""",
            (preflight_id, version["artifact_id"], version["version_number"], identity.tenant_id, identity.user_id),
        ).fetchone()
        now = connection.execute(
            "SELECT CURRENT_TIMESTAMP AS now"
        ).fetchone()["now"]
        if row is None or row["outcome"] != "PASS" or row["content_fingerprint"] != version["content_fingerprint"] or row["expires_at"] <= now:
            raise GovernedDomainConflict("A current passing DLP preflight is required.")
