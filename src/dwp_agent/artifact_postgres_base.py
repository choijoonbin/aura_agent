from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from .artifact_contracts import (
    ArtifactDraftContent,
    ArtifactSourceReference,
    GovernedArtifact,
)
from .artifact_runtime_capabilities import artifact_runtime_capabilities
from .governed_domain_core import (
    CommandProof,
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedFingerprints,
    GovernedPayloadCodec,
    advisory_lock,
    require_command_replay,
)
from .personal_domain_security import PersonalDomainIdentity


SOURCE_PERMISSIONS = {
    "WORK_ITEM": "APP.WORK:VIEW",
    "MAIL": "APP.MAIL:VIEW",
    "CALENDAR": "APP.CALENDAR:VIEW",
    "APPROVAL_TASK": "APP.APPROVALS:VIEW",
    "APPROVAL_REQUEST": "APP.APPROVALS:VIEW",
    "APPROVAL_FORM": "APP.APPROVALS:VIEW",
    "APPROVAL_OPERATION": "APP.APPROVALS:VIEW",
}


class ArtifactPostgresBase:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.codec = GovernedPayloadCodec()
        self.fingerprints = GovernedFingerprints.load()

    def _proof(
        self,
        identity: PersonalDomainIdentity,
        command_type: str,
        artifact_id: UUID | None,
        request: Any,
    ) -> CommandProof:
        return self.fingerprints.command(
            tenant_id=identity.tenant_id,
            session_id=identity.auth_session_id,
            purpose=f"governed-artifact:{command_type.lower()}",
            payload={
                "artifactId": str(artifact_id) if artifact_id else None,
                "request": request.model_dump(mode="json", by_alias=True),
            },
        )

    def _replay(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        command_id: UUID,
        command_type: str,
        proof: CommandProof,
    ) -> dict[str, object] | None:
        advisory_lock(
            connection,
            "artifact-command",
            identity.tenant_id,
            identity.user_id,
            command_id,
        )
        row = connection.execute(
            """SELECT command_type, session_fingerprint, request_fingerprint,
                      result_envelope
                 FROM ai_artifact_commands
                WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
            (identity.tenant_id, identity.user_id, command_id),
        ).fetchone()
        if row is None:
            return None
        if row["command_type"] != command_type:
            raise GovernedDomainConflict("The command ID is already in use.")
        require_command_replay(
            row["session_fingerprint"], row["request_fingerprint"], proof
        )
        return self.codec.decrypt_json(
            row["result_envelope"],
            tenant_id=identity.tenant_id,
            resource_type="artifact-command",
            resource_id=str(command_id),
            field="result",
        )

    def _record_command(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        command_type: str,
        request: Any,
        proof: CommandProof,
        result: Any,
    ) -> None:
        envelope = self.codec.encrypt_json(
            result.model_dump(mode="json", by_alias=True),
            tenant_id=identity.tenant_id,
            resource_type="artifact-command",
            resource_id=str(request.command_id),
            field="result",
        )
        connection.execute(
            """INSERT INTO ai_artifact_commands (
                   tenant_id, user_id, command_id, artifact_id, command_type,
                   session_fingerprint, request_fingerprint, result_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                identity.tenant_id,
                identity.user_id,
                request.command_id,
                artifact_id,
                command_type,
                proof.session_fingerprint,
                proof.request_fingerprint,
                envelope,
            ),
        )

    def _event(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        command_id: UUID,
        event_type: str,
        previous_state: str | None,
        current_state: str,
        revision: int,
        request_fingerprint: str,
        reason_code: str,
        change_reason: str | None,
    ) -> None:
        event_id = uuid4()
        envelope = None
        if change_reason is not None:
            envelope = self.codec.encrypt_json(
                {"changeReason": change_reason},
                tenant_id=identity.tenant_id,
                resource_type="artifact-event",
                resource_id=str(event_id),
                field="change-reason",
            )
        connection.execute(
            """INSERT INTO ai_artifact_events (
                   event_id, artifact_id, tenant_id, user_id, actor_user_id,
                   correlation_id, command_id, event_type, previous_state,
                   current_state, revision, request_fingerprint, reason_code,
                   change_reason_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                event_id,
                artifact_id,
                identity.tenant_id,
                identity.user_id,
                identity.user_id,
                identity.correlation_id,
                command_id,
                event_type,
                previous_state,
                current_state,
                revision,
                request_fingerprint,
                reason_code,
                envelope,
            ),
        )

    def _locked_artifact(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        *,
        lock: bool = True,
    ) -> Any:
        row = connection.execute(
            _ARTIFACT_SELECT
            + " WHERE a.artifact_id = %s AND a.tenant_id = %s AND a.user_id = %s"
            + (" FOR UPDATE" if lock else ""),
            (artifact_id, identity.tenant_id, identity.user_id),
        ).fetchone()
        if row is None:
            raise GovernedDomainNotFound("The governed artifact is unavailable.")
        return row

    def _artifact(self, connection: Any, row: Any) -> GovernedArtifact:
        content = ArtifactDraftContent.model_validate(
            self.codec.decrypt_json(
                row["content_envelope"],
                tenant_id=row["tenant_id"],
                resource_type="artifact-draft",
                resource_id=str(row["artifact_id"]),
                field="content",
            )
        )
        return GovernedArtifact(
            artifact_id=row["artifact_id"],
            artifact_type=row["artifact_type"],
            state=row["artifact_state"],
            revision=row["revision"],
            draft_revision=row["draft_revision"],
            current_version_number=row["current_version_number"],
            published_version_number=row["published_version_number"],
            content=content,
            sources=self._draft_sources(connection, row["tenant_id"], row["artifact_id"]),
            capabilities=artifact_runtime_capabilities(),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _draft_sources(
        self, connection: Any, tenant_id: int, artifact_id: UUID
    ) -> list[ArtifactSourceReference]:
        return [
            record[0]
            for record in self._draft_source_records(
                connection, tenant_id, artifact_id
            )
        ]

    def _draft_source_records(
        self, connection: Any, tenant_id: int, artifact_id: UUID
    ) -> list[tuple[ArtifactSourceReference, str, Any, str | None]]:
        rows = connection.execute(
            """SELECT source_link_id, source_type, reference_envelope,
                      verification_state, verified_at,
                      verification_evidence_fingerprint
                 FROM ai_artifact_draft_sources
                WHERE artifact_id = %s ORDER BY source_type, reference_fingerprint""",
            (artifact_id,),
        ).fetchall()
        return [
            (
                ArtifactSourceReference.model_validate(
                    self.codec.decrypt_json(
                        row["reference_envelope"],
                        tenant_id=tenant_id,
                        resource_type="artifact-draft-source",
                        resource_id=str(row["source_link_id"]),
                        field="reference",
                    )
                ),
                row["verification_state"],
                row["verified_at"],
                row["verification_evidence_fingerprint"],
            )
            for row in rows
        ]

    def _replace_draft_sources(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        sources: list[ArtifactSourceReference],
        *,
        verified_source_references: frozenset[str] = frozenset(),
    ) -> None:
        connection.execute(
            "DELETE FROM ai_artifact_draft_sources WHERE artifact_id = %s",
            (artifact_id,),
        )
        verified_at = connection.execute(
            "SELECT CURRENT_TIMESTAMP AS now"
        ).fetchone()["now"]
        for source in sources:
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
                resource_type="artifact-draft-source",
                resource_id=str(source_link_id),
                field="reference",
            )
            verified = source.reference in verified_source_references
            evidence_fingerprint = None
            if verified:
                evidence_fingerprint = self.fingerprints.value(
                    tenant_id=identity.tenant_id,
                    purpose="artifact-source-verification",
                    payload={
                        "source": payload,
                        "verification": "SERVER_VERIFIED_CONVERSATION_CITATION",
                    },
                )
            connection.execute(
                """INSERT INTO ai_artifact_draft_sources (
                       source_link_id, artifact_id, source_type,
                       reference_fingerprint, reference_envelope,
                       verification_state, verified_at,
                       verification_evidence_fingerprint)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    source_link_id,
                    artifact_id,
                    source.source_type.value,
                    fingerprint,
                    envelope,
                    "SERVER_VERIFIED" if verified else "UNVERIFIED",
                    verified_at if verified else None,
                    evidence_fingerprint,
                ),
            )

    def _version_row(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        version_number: int,
    ) -> Any:
        row = connection.execute(
            """SELECT artifact_id, version_number, tenant_id, user_id,
                      content_envelope, content_fingerprint, source_count, created_at
                 FROM ai_artifact_versions
                WHERE artifact_id = %s AND version_number = %s
                  AND tenant_id = %s AND user_id = %s""",
            (artifact_id, version_number, identity.tenant_id, identity.user_id),
        ).fetchone()
        if row is None:
            raise GovernedDomainNotFound("The artifact version is unavailable.")
        return row

    def _version_content(self, row: Any) -> ArtifactDraftContent:
        return ArtifactDraftContent.model_validate(
            self.codec.decrypt_json(
                row["content_envelope"],
                tenant_id=row["tenant_id"],
                resource_type="artifact-version",
                resource_id=f"{row['artifact_id']}:{row['version_number']}",
                field="content",
            )
        )

    def _version_sources(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        version_number: int,
    ) -> list[ArtifactSourceReference]:
        rows = connection.execute(
            """SELECT source_link_id, source_type, reference_envelope
                 FROM ai_artifact_version_sources
                WHERE artifact_id = %s AND version_number = %s
                ORDER BY source_type, reference_fingerprint""",
            (artifact_id, version_number),
        ).fetchall()
        return [
            ArtifactSourceReference.model_validate(
                self.codec.decrypt_json(
                    row["reference_envelope"],
                    tenant_id=identity.tenant_id,
                    resource_type="artifact-version-source",
                    resource_id=str(row["source_link_id"]),
                    field="reference",
                )
            )
            for row in rows
        ]

    @staticmethod
    def _require_source_access(
        identity: PersonalDomainIdentity, sources: list[ArtifactSourceReference]
    ) -> None:
        identity.require(*(SOURCE_PERMISSIONS[source.source_type.value] for source in sources))

    @staticmethod
    def _expected(row: Any, expected_revision: int) -> None:
        if int(row["revision"]) != expected_revision:
            raise GovernedDomainConflict("The artifact revision has changed.")
        if row["artifact_state"] == "ARCHIVED":
            raise GovernedDomainConflict("An archived artifact cannot be changed.")

    @staticmethod
    def _retention_days(connection: Any, tenant_id: int, domain: str) -> int:
        row = connection.execute(
            """SELECT retention_days FROM ai_domain_retention_policies
                WHERE tenant_id = %s AND domain_key = %s""",
            (tenant_id, domain),
        ).fetchone()
        if row is None:
            raise GovernedDomainConflict(
                f"An explicit {domain.lower()} retention policy is required."
            )
        return int(row["retention_days"])


_ARTIFACT_SELECT = """SELECT a.artifact_id, a.tenant_id, a.user_id,
       a.artifact_type, a.artifact_state, a.revision,
       a.current_draft_revision AS draft_revision, a.current_version_number,
       a.published_version_number, a.created_at, a.updated_at,
       d.content_envelope, d.content_fingerprint
  FROM ai_artifacts a
  JOIN ai_artifact_drafts d ON d.artifact_id = a.artifact_id"""
