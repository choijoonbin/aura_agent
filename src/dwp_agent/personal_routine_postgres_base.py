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
from .personal_routine_contracts import (
    PersonalRoutine,
    RoutineConsentSet,
    RoutineConsentState,
    RoutineDefinition,
)
from .personal_routine_schedule import SOURCE_PERMISSIONS


class PersonalRoutinePostgresBase:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise GovernedDomainUnavailable(
                "Personal routine encryption is unavailable."
            ) from error

    def _proof(
        self,
        identity: PersonalDomainIdentity,
        command_type: str,
        routine_id: UUID | None,
        request: Any,
    ) -> CommandProof:
        return self.fingerprints.command(
            tenant_id=identity.tenant_id,
            session_id=identity.auth_session_id,
            purpose=f"personal-routine:{command_type.lower()}",
            payload={
                "routineId": str(routine_id) if routine_id else None,
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
            "personal-routine-command",
            identity.tenant_id,
            identity.user_id,
            command_id,
        )
        row = connection.execute(
            """SELECT command_type, session_fingerprint, request_fingerprint,
                      result_envelope
                 FROM ai_personal_routine_commands
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
            resource_type="personal-routine-command",
            resource_id=str(command_id),
            field="result",
        )

    def _record_command(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        command_type: str,
        request: Any,
        proof: CommandProof,
        result: Any,
    ) -> None:
        envelope = self.codec.encrypt_json(
            result.model_dump(mode="json", by_alias=True),
            tenant_id=identity.tenant_id,
            resource_type="personal-routine-command",
            resource_id=str(request.command_id),
            field="result",
        )
        connection.execute(
            """INSERT INTO ai_personal_routine_commands (
                   tenant_id, user_id, command_id, routine_id, command_type,
                   session_fingerprint, request_fingerprint, result_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                identity.tenant_id,
                identity.user_id,
                request.command_id,
                routine_id,
                command_type,
                proof.session_fingerprint,
                proof.request_fingerprint,
                envelope,
            ),
        )

    def _locked(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        *,
        lock: bool = True,
    ) -> Any:
        row = connection.execute(
            _ROUTINE_SELECT
            + " WHERE routine_id = %s AND tenant_id = %s AND user_id = %s"
            + (" FOR UPDATE" if lock else ""),
            (routine_id, identity.tenant_id, identity.user_id),
        ).fetchone()
        if row is None:
            raise GovernedDomainNotFound("The personal routine is unavailable.")
        return row

    def _routine(self, row: Any) -> PersonalRoutine:
        return PersonalRoutine(
            routine_id=row["routine_id"],
            lifecycle_state=row["lifecycle_state"],
            consent_state=row["consent_state"],
            consents=RoutineConsentSet(
                source_access=row["source_access_consent_state"],
                analysis=row["analysis_consent_state"],
                proposal_delivery=row["proposal_delivery_consent_state"],
            ),
            execution_mode=row["execution_mode"],
            revision=row["revision"],
            definition=self._definition(row),
            scheduling_available=False,
            next_run_at=None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _definition(self, row: Any) -> RoutineDefinition:
        return RoutineDefinition.model_validate(
            self.codec.decrypt_json(
                row["definition_envelope"],
                tenant_id=row["tenant_id"],
                resource_type="personal-routine",
                resource_id=str(row["routine_id"]),
                field="definition",
            )
        )

    def _replace_sources(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        definition: RoutineDefinition,
    ) -> None:
        connection.execute(
            "DELETE FROM ai_personal_routine_sources WHERE routine_id = %s",
            (routine_id,),
        )
        for source in definition.sources:
            connection.execute(
                """INSERT INTO ai_personal_routine_sources (
                       routine_id, tenant_id, user_id, source_key, required_permission)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    routine_id,
                    identity.tenant_id,
                    identity.user_id,
                    source.value,
                    SOURCE_PERMISSIONS[source.value],
                ),
            )

    def _consent_event(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        command_id: UUID,
        previous_state: str,
        current_state: str,
        revision: int,
        scope_fingerprint: str,
        proof: CommandProof,
        reason_code: str,
        change_reason: str,
        consent_scope: str,
    ) -> None:
        event_id = uuid4()
        envelope = self.codec.encrypt_json(
            {"changeReason": change_reason},
            tenant_id=identity.tenant_id,
            resource_type="personal-routine-consent",
            resource_id=str(event_id),
            field="change-reason",
        )
        connection.execute(
            """INSERT INTO ai_personal_routine_consents (
                   consent_event_id, routine_id, tenant_id, user_id, actor_user_id,
                   correlation_id, command_id, previous_state, current_state,
                   consent_scope, routine_revision, scope_fingerprint, session_fingerprint,
                   request_fingerprint, reason_code, change_reason_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                event_id,
                routine_id,
                identity.tenant_id,
                identity.user_id,
                identity.user_id,
                identity.correlation_id,
                command_id,
                previous_state,
                current_state,
                consent_scope,
                revision,
                scope_fingerprint,
                proof.session_fingerprint,
                proof.request_fingerprint,
                reason_code,
                envelope,
            ),
        )

    @staticmethod
    def _consent_values(row: Any) -> dict[str, str]:
        return {
            "SOURCE_ACCESS": row["source_access_consent_state"],
            "ANALYSIS": row["analysis_consent_state"],
            "PROPOSAL_DELIVERY": row["proposal_delivery_consent_state"],
        }

    @staticmethod
    def _aggregate_consent(values: dict[str, str]) -> str:
        states = set(values.values())
        if RoutineConsentState.RECONSENT_REQUIRED.value in states:
            return RoutineConsentState.RECONSENT_REQUIRED.value
        if states == {RoutineConsentState.ENABLED.value}:
            return RoutineConsentState.ENABLED.value
        if RoutineConsentState.DISABLED.value in states:
            return RoutineConsentState.DISABLED.value
        return RoutineConsentState.UNSET.value

    def _event(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        command_id: UUID,
        event_type: str,
        previous_state: str | None,
        current_state: str,
        revision: int,
        reason_code: str,
        request_fingerprint: str,
        change_reason: str | None,
    ) -> None:
        event_id = uuid4()
        envelope = None
        if change_reason is not None:
            envelope = self.codec.encrypt_json(
                {"changeReason": change_reason},
                tenant_id=identity.tenant_id,
                resource_type="personal-routine-event",
                resource_id=str(event_id),
                field="change-reason",
            )
        connection.execute(
            """INSERT INTO ai_personal_routine_events (
                   event_id, routine_id, tenant_id, user_id, actor_user_id,
                   correlation_id, command_id, event_type, previous_state,
                   current_state, revision, request_fingerprint, reason_code,
                   change_reason_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                event_id,
                routine_id,
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

    def _definition_fingerprint(
        self, tenant_id: int, definition: RoutineDefinition
    ) -> str:
        return self.fingerprints.value(
            tenant_id=tenant_id,
            purpose="personal-routine-definition",
            payload=definition.model_dump(mode="json", by_alias=True),
        )

    @staticmethod
    def _require_source_access(
        identity: PersonalDomainIdentity, definition: RoutineDefinition
    ) -> None:
        identity.require(
            *(SOURCE_PERMISSIONS[source.value] for source in definition.sources)
        )

    @staticmethod
    def _require_source_preferences(
        connection: Any,
        identity: PersonalDomainIdentity,
        definition: RoutineDefinition,
    ) -> None:
        rows = connection.execute(
            """SELECT source_key FROM ai_user_ai_source_preferences
                WHERE tenant_id = %s AND user_id = %s AND enabled = TRUE
                  AND source_key = ANY(%s)""",
            (
                identity.tenant_id,
                identity.user_id,
                [source.value for source in definition.sources],
            ),
        ).fetchall()
        enabled = {row["source_key"] for row in rows}
        if enabled != {source.value for source in definition.sources}:
            raise GovernedDomainConflict(
                "Every routine source requires explicit personal AI source consent."
            )

    @staticmethod
    def _expected(row: Any, expected_revision: int) -> None:
        if int(row["revision"]) != expected_revision:
            raise GovernedDomainConflict("The personal routine revision has changed.")

    @staticmethod
    def _retention_days(connection: Any, tenant_id: int) -> int:
        row = connection.execute(
            """SELECT retention_days FROM ai_domain_retention_policies
                WHERE tenant_id = %s AND domain_key = 'ROUTINE'""",
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise GovernedDomainConflict(
                "An explicit routine retention policy is required."
            )
        return int(row["retention_days"])


_ROUTINE_SELECT = """SELECT routine_id, tenant_id, user_id, lifecycle_state,
       consent_state, source_access_consent_state, analysis_consent_state,
       proposal_delivery_consent_state, execution_mode, revision, definition_envelope,
       definition_fingerprint, next_run_at, created_at, updated_at
  FROM ai_personal_routines"""
