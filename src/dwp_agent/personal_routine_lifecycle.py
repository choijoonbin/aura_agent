from __future__ import annotations

from uuid import UUID

from psycopg import connect
from psycopg.rows import dict_row

from .governed_domain_core import GovernedDomainConflict
from .personal_domain_security import PersonalDomainIdentity
from .personal_routine_contracts import (
    ChangeRoutineLifecycleRequest,
    PersonalRoutine,
    RoutineLifecycle,
    RoutineLifecycleAction,
)


class PersonalRoutineLifecycleCommands:
    database_url: str

    def change_lifecycle(
        self,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        request: ChangeRoutineLifecycleRequest,
    ) -> PersonalRoutine:
        proof = self._proof(identity, "LIFECYCLE", routine_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(
                connection, identity, request.command_id, "LIFECYCLE", proof
            )
            if replay is not None:
                return PersonalRoutine.model_validate(replay)
            row = self._locked(connection, identity, routine_id)
            self._expected(row, request.expected_revision)
            if row["lifecycle_state"] == RoutineLifecycle.ARCHIVED.value:
                raise GovernedDomainConflict("An archived routine cannot change lifecycle.")
            target = self._target_lifecycle(row["lifecycle_state"], request.action)
            revision = int(row["revision"]) + 1
            connection.execute(
                """UPDATE ai_personal_routines
                      SET lifecycle_state = %s, revision = %s,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE routine_id = %s AND tenant_id = %s AND user_id = %s""",
                (target, revision, routine_id, identity.tenant_id, identity.user_id),
            )
            routine = self._routine(
                self._locked(connection, identity, routine_id, lock=False)
            )
            self._record_command(
                connection, identity, routine_id, "LIFECYCLE", request, proof, routine
            )
            self._event(
                connection,
                identity,
                routine_id,
                request.command_id,
                "LIFECYCLE_CHANGED",
                row["lifecycle_state"],
                target,
                revision,
                request.reason_code,
                proof.request_fingerprint,
                request.change_reason,
            )
            return routine

    @staticmethod
    def _target_lifecycle(current: str, action: RoutineLifecycleAction) -> str:
        if action == RoutineLifecycleAction.PAUSE:
            if current == RoutineLifecycle.PAUSED.value:
                raise GovernedDomainConflict("The routine is already paused.")
            return RoutineLifecycle.PAUSED.value
        if current != RoutineLifecycle.PAUSED.value:
            raise GovernedDomainConflict("Only a paused routine can resume preview access.")
        return RoutineLifecycle.DRAFT.value
