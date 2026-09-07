from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from .governed_domain_core import (
    CommandProof,
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
    GovernedFingerprints,
    GovernedPayloadCodec,
    advisory_lock,
    require_command_replay,
)
from .personal_domain_security import PersonalDomainIdentity
from .personal_memory_contracts import (
    AiSourceKey,
    AiSourcePreference,
    ExplicitMemoryValue,
    MemoryPreferenceState,
    MemoryState,
    PersonalAiControls,
    PersonalMemory,
)


SOURCE_PERMISSIONS = {
    "WORK_ITEM": "APP.WORK:VIEW",
    "MAIL": "APP.MAIL:VIEW",
    "CALENDAR": "APP.CALENDAR:VIEW",
}


class PersonalMemoryPostgresBase:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise GovernedDomainUnavailable(
                "Personal memory encryption is unavailable."
            ) from error

    def _proof(
        self,
        identity: PersonalDomainIdentity,
        command_type: str,
        memory_id: UUID | None,
        request: Any,
        source_key: AiSourceKey | None = None,
    ) -> CommandProof:
        return self.fingerprints.command(
            tenant_id=identity.tenant_id,
            session_id=identity.auth_session_id,
            purpose=f"personal-memory:{command_type.lower()}",
            payload={
                "memoryId": str(memory_id) if memory_id else None,
                "sourceKey": source_key.value if source_key else None,
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
            "personal-memory-command",
            identity.tenant_id,
            identity.user_id,
            command_id,
        )
        row = connection.execute(
            """SELECT command_type, session_fingerprint, request_fingerprint,
                      result_envelope
                 FROM ai_user_memory_commands
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
            resource_type="personal-memory-command",
            resource_id=str(command_id),
            field="result",
        )

    def _record_command(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        memory_id: UUID | None,
        command_type: str,
        request: Any,
        proof: CommandProof,
        result: Any,
    ) -> None:
        envelope = self.codec.encrypt_json(
            result.model_dump(mode="json", by_alias=True),
            tenant_id=identity.tenant_id,
            resource_type="personal-memory-command",
            resource_id=str(request.command_id),
            field="result",
        )
        connection.execute(
            """INSERT INTO ai_user_memory_commands (
                   tenant_id, user_id, command_id, memory_id, command_type,
                   session_fingerprint, request_fingerprint, result_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                identity.tenant_id,
                identity.user_id,
                request.command_id,
                memory_id,
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
        command_id: UUID,
        target_type: str,
        target_id: str,
        event_type: str,
        previous_state: str | None,
        current_state: str,
        revision: int,
        proof: CommandProof,
        reason_code: str,
        change_reason: str | None,
    ) -> None:
        event_id = uuid4()
        envelope = None
        if change_reason is not None:
            envelope = self.codec.encrypt_json(
                {"changeReason": change_reason},
                tenant_id=identity.tenant_id,
                resource_type="personal-memory-event",
                resource_id=str(event_id),
                field="change-reason",
            )
        connection.execute(
            """INSERT INTO ai_user_memory_events (
                   event_id, tenant_id, user_id, actor_user_id, correlation_id,
                   command_id, target_type, target_id, event_type, previous_state,
                   current_state, revision, session_fingerprint, request_fingerprint,
                   reason_code, change_reason_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                event_id,
                identity.tenant_id,
                identity.user_id,
                identity.user_id,
                identity.correlation_id,
                command_id,
                target_type,
                target_id,
                event_type,
                previous_state,
                current_state,
                revision,
                proof.session_fingerprint,
                proof.request_fingerprint,
                reason_code,
                envelope,
            ),
        )

    def _locked(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        memory_id: UUID,
        *,
        lock: bool = True,
    ) -> Any:
        row = connection.execute(
            _MEMORY_SELECT
            + " WHERE memory_id = %s AND tenant_id = %s AND user_id = %s"
            + (" FOR UPDATE" if lock else ""),
            (memory_id, identity.tenant_id, identity.user_id),
        ).fetchone()
        if row is None:
            raise GovernedDomainNotFound("The personal memory is unavailable.")
        return row

    def _memory(self, row: Any) -> PersonalMemory:
        payload = self.codec.decrypt_json(
            row["payload_envelope"],
            tenant_id=row["tenant_id"],
            resource_type="personal-memory",
            resource_id=str(row["memory_id"]),
            field="payload",
        )
        return PersonalMemory(
            memory_id=row["memory_id"],
            kind=row["memory_kind"],
            state=row["memory_state"],
            revision=row["revision"],
            memory=ExplicitMemoryValue.model_validate(payload),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _encoded_memory(
        self,
        identity: PersonalDomainIdentity,
        memory_id: UUID,
        memory: ExplicitMemoryValue,
    ) -> tuple[str, str]:
        payload = memory.model_dump(mode="json", by_alias=True)
        return (
            self.codec.encrypt_json(
                payload,
                tenant_id=identity.tenant_id,
                resource_type="personal-memory",
                resource_id=str(memory_id),
                field="payload",
            ),
            self.fingerprints.value(
                tenant_id=identity.tenant_id,
                purpose="personal-memory-value",
                payload=payload,
            ),
        )

    @staticmethod
    def _controls(
        row: Any | None, source_preferences: list[AiSourcePreference]
    ) -> PersonalAiControls:
        state = (
            MemoryPreferenceState(row["memory_state"])
            if row
            else MemoryPreferenceState.UNSET
        )
        return PersonalAiControls(
            memory_state=state,
            revision=int(row["revision"]) if row else 0,
            memory_enabled=state == MemoryPreferenceState.ENABLED,
            memory_effective=False,
            source_preferences=source_preferences,
            updated_at=row["updated_at"] if row else None,
        )

    def _source_preferences(
        self, connection: Any, identity: PersonalDomainIdentity
    ) -> list[AiSourcePreference]:
        rows = connection.execute(
            """SELECT source_key, enabled, revision, updated_at
                 FROM ai_user_ai_source_preferences
                WHERE tenant_id = %s AND user_id = %s""",
            (identity.tenant_id, identity.user_id),
        ).fetchall()
        stored = {row["source_key"]: row for row in rows}
        return [
            self._source_preference(
                source,
                stored.get(source.value),
                SOURCE_PERMISSIONS[source.value] in identity.permissions,
            )
            for source in AiSourceKey
        ]

    @staticmethod
    def _source_preference(
        source_key: AiSourceKey, row: Any | None, available: bool
    ) -> AiSourcePreference:
        enabled = bool(row["enabled"]) if row else False
        return AiSourcePreference(
            source_key=source_key,
            available=available,
            enabled=enabled,
            effective=available and enabled,
            revision=int(row["revision"]) if row else 0,
            updated_at=row["updated_at"] if row else None,
        )

    @staticmethod
    def _expected_active(row: Any, expected_revision: int) -> None:
        if row["memory_state"] == MemoryState.DELETED.value:
            raise GovernedDomainConflict("The personal memory was deleted.")
        if int(row["revision"]) != expected_revision:
            raise GovernedDomainConflict("The personal memory revision has changed.")

    @staticmethod
    def _require_enabled(connection: Any, identity: PersonalDomainIdentity) -> None:
        row = connection.execute(
            """SELECT memory_state FROM ai_user_memory_preferences
                WHERE tenant_id = %s AND user_id = %s""",
            (identity.tenant_id, identity.user_id),
        ).fetchone()
        if row is None or row["memory_state"] != MemoryPreferenceState.ENABLED.value:
            raise GovernedDomainConflict("Explicit personal memory consent is required.")

    @staticmethod
    def _retention_days(connection: Any, tenant_id: int) -> int:
        row = connection.execute(
            """SELECT retention_days FROM ai_domain_retention_policies
                WHERE tenant_id = %s AND domain_key = 'MEMORY'""",
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise GovernedDomainConflict(
                "An explicit memory retention policy is required."
            )
        return int(row["retention_days"])


_MEMORY_SELECT = """SELECT memory_id, tenant_id, user_id, memory_kind, memory_state,
       revision, payload_envelope, payload_fingerprint, created_at, updated_at
  FROM ai_user_memories"""
