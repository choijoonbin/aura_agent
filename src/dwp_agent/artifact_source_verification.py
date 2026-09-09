from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from .governed_domain_core import GovernedDomainConflict
from .personal_domain_security import PersonalDomainIdentity


class ArtifactSourceVerification:
    codec: Any
    fingerprints: Any

    def _insert_version_source(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        version: int,
        source: Any,
        *,
        verification_state: str,
        verified_at: Any,
        evidence_fingerprint: str | None,
    ) -> None:
        source_link_id = uuid4()
        payload = source.model_dump(mode="json", by_alias=True)
        fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="artifact-source-reference",
            payload=payload,
        )
        envelope = self.codec.encrypt_json(
            payload,
            tenant_id=identity.tenant_id,
            resource_type="artifact-version-source",
            resource_id=str(source_link_id),
            field="reference",
        )
        connection.execute(
            """INSERT INTO ai_artifact_version_sources (
                   source_link_id, artifact_id, version_number, source_type,
                   reference_fingerprint, reference_envelope, verification_state,
                   verified_at, verification_evidence_fingerprint)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                source_link_id,
                artifact_id,
                version,
                source.source_type.value,
                fingerprint,
                envelope,
                verification_state,
                verified_at,
                evidence_fingerprint,
            ),
        )

    @staticmethod
    def _all_version_sources_verified(
        connection: Any, artifact_id: UUID, version_number: int
    ) -> bool:
        counts = connection.execute(
            """SELECT COUNT(*) AS source_count, COUNT(*) FILTER (
                         WHERE verification_state = 'SERVER_VERIFIED'
                           AND verified_at IS NOT NULL
                           AND verification_evidence_fingerprint IS NOT NULL)
                           AS verified_count
                 FROM ai_artifact_version_sources
                WHERE artifact_id = %s AND version_number = %s""",
            (artifact_id, version_number),
        ).fetchone()
        return int(counts["source_count"]) == int(counts["verified_count"])

    @staticmethod
    def _require_passing_preflight(
        connection: Any,
        identity: PersonalDomainIdentity,
        version: Any,
        preflight_id: UUID,
    ) -> None:
        row = connection.execute(
            """SELECT outcome, content_fingerprint, expires_at
                 FROM ai_artifact_preflight_runs
                WHERE preflight_id = %s AND artifact_id = %s AND version_number = %s
                  AND tenant_id = %s AND user_id = %s""",
            (
                preflight_id,
                version["artifact_id"],
                version["version_number"],
                identity.tenant_id,
                identity.user_id,
            ),
        ).fetchone()
        now = connection.execute("SELECT CURRENT_TIMESTAMP AS now").fetchone()["now"]
        if (
            row is None
            or row["outcome"] != "PASS"
            or row["content_fingerprint"] != version["content_fingerprint"]
            or row["expires_at"] <= now
        ):
            raise GovernedDomainConflict("A current passing DLP preflight is required.")
