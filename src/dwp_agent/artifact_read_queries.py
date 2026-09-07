from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg import connect
from psycopg.rows import dict_row

from .artifact_contracts import (
    ArtifactPreflightReceipt,
    ArtifactSourceEvidence,
    ArtifactSourceReference,
    ArtifactVersionDetail,
    ArtifactVersionSummary,
    DlpFinding,
)
from .governed_domain_core import GovernedDomainConflict, GovernedDomainNotFound
from .personal_domain_security import PersonalDomainIdentity


class ArtifactReadQueries:
    database_url: str

    def list_versions(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        *,
        limit: int,
        before_version: int | None,
    ) -> list[ArtifactVersionSummary]:
        if not 1 <= limit <= 50 or (
            before_version is not None and before_version < 1
        ):
            raise GovernedDomainConflict("Artifact version page bounds are invalid.")
        with connect(self.database_url, row_factory=dict_row) as connection:
            self._locked_artifact(connection, identity, artifact_id, lock=False)
            if before_version is None:
                rows = connection.execute(
                    """SELECT artifact_id, version_number, content_fingerprint,
                              source_count, created_at
                         FROM ai_artifact_versions
                        WHERE artifact_id = %s AND tenant_id = %s AND user_id = %s
                        ORDER BY version_number DESC LIMIT %s""",
                    (artifact_id, identity.tenant_id, identity.user_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT artifact_id, version_number, content_fingerprint,
                              source_count, created_at
                         FROM ai_artifact_versions
                        WHERE artifact_id = %s AND tenant_id = %s AND user_id = %s
                          AND version_number < %s
                        ORDER BY version_number DESC LIMIT %s""",
                    (
                        artifact_id,
                        identity.tenant_id,
                        identity.user_id,
                        before_version,
                        limit,
                    ),
                ).fetchall()
            return [ArtifactVersionSummary.model_validate(row) for row in rows]

    def get_version(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        version_number: int,
    ) -> ArtifactVersionDetail:
        with connect(self.database_url, row_factory=dict_row) as connection:
            self._locked_artifact(connection, identity, artifact_id, lock=False)
            row = self._version_row(
                connection, identity, artifact_id, version_number
            )
            evidence = self._source_evidence(
                connection, identity, artifact_id, version_number
            )
            self._require_source_access(
                identity, [item.source for item in evidence]
            )
            return ArtifactVersionDetail(
                artifact_id=artifact_id,
                version_number=version_number,
                content_fingerprint=row["content_fingerprint"],
                source_count=row["source_count"],
                created_at=row["created_at"],
                content=self._version_content(row),
                source_evidence=evidence,
            )

    def current_preflight(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
    ) -> ArtifactPreflightReceipt:
        with connect(self.database_url, row_factory=dict_row) as connection:
            artifact = self._locked_artifact(
                connection, identity, artifact_id, lock=False
            )
            version_number = int(artifact["current_version_number"])
            if version_number == 0:
                raise GovernedDomainNotFound(
                    "No immutable artifact version has been created."
                )
            version = self._version_row(
                connection, identity, artifact_id, version_number
            )
            sources = self._version_sources(
                connection, identity, artifact_id, version_number
            )
            self._require_source_access(identity, sources)
            row = connection.execute(
                """SELECT preflight_id, artifact_id, version_number,
                          artifact_revision, policy_key, policy_version,
                          outcome, findings_envelope,
                          created_at, expires_at
                     FROM ai_artifact_preflight_runs
                    WHERE artifact_id = %s AND version_number = %s
                      AND tenant_id = %s AND user_id = %s
                      AND content_fingerprint = %s
                    ORDER BY created_at DESC, preflight_id DESC LIMIT 1""",
                (
                    artifact_id,
                    version_number,
                    identity.tenant_id,
                    identity.user_id,
                    version["content_fingerprint"],
                ),
            ).fetchone()
            if row is None:
                raise GovernedDomainNotFound(
                    "No DLP preflight exists for the current artifact version."
                )
            now = connection.execute(
                "SELECT CURRENT_TIMESTAMP AS now"
            ).fetchone()["now"]
            current = row["expires_at"] > now
            findings = self.codec.decrypt_json(
                row["findings_envelope"],
                tenant_id=identity.tenant_id,
                resource_type="artifact-preflight",
                resource_id=str(row["preflight_id"]),
                field="findings",
            )
            allowed = current and row["outcome"] == "PASS"
            return ArtifactPreflightReceipt(
                preflight_id=row["preflight_id"],
                artifact_id=artifact_id,
                artifact_revision=row["artifact_revision"],
                version_number=version_number,
                policy_key=row["policy_key"],
                policy_version=row["policy_version"],
                outcome=row["outcome"],
                findings=[
                    DlpFinding.model_validate(item)
                    for item in findings.get("findings", [])
                ],
                evaluated_at=row["created_at"],
                expires_at=row["expires_at"],
                current=current,
                publish_allowed=allowed,
                export_allowed=allowed,
            )

    def _source_evidence(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        version_number: int,
    ) -> list[ArtifactSourceEvidence]:
        rows = connection.execute(
            """SELECT source_link_id, verification_state, reference_envelope
                 FROM ai_artifact_version_sources
                WHERE artifact_id = %s AND version_number = %s
                ORDER BY source_type, reference_fingerprint""",
            (artifact_id, version_number),
        ).fetchall()
        return [
            ArtifactSourceEvidence(
                source=ArtifactSourceReference.model_validate(
                    self.codec.decrypt_json(
                        row["reference_envelope"],
                        tenant_id=identity.tenant_id,
                        resource_type="artifact-version-source",
                        resource_id=str(row["source_link_id"]),
                        field="reference",
                    )
                ),
                verification_state=row["verification_state"],
            )
            for row in rows
        ]
