from __future__ import annotations

from uuid import UUID, uuid4

from psycopg import connect
from psycopg.rows import dict_row

from .governed_domain_core import GovernedDomainConflict, GovernedDomainUnavailable
from .personal_domain_security import PersonalDomainIdentity
from .personal_routine_contracts import (
    ChangeRoutineActivationRequest,
    CommandRoutineRunRequest,
    PersonalRoutine,
    RoutineActivationAction,
    RoutineExecutionRun,
    RoutineLifecycle,
    RoutineRunCommand,
    RoutineTriggerType,
    TriggerRoutineRunRequest,
)
from .personal_routine_execution_queries import (
    EXECUTION_SELECT,
    PersonalRoutineExecutionQueries,
)
from .personal_routine_schedule import preview_next_run


class PersonalRoutineExecutionCommands(PersonalRoutineExecutionQueries):
    def change_activation(
        self,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        request: ChangeRoutineActivationRequest,
    ) -> PersonalRoutine:
        proof = self._proof(identity, "ACTIVATION", routine_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(
                connection, identity, request.command_id, "ACTIVATION", proof
            )
            if replay is not None:
                return PersonalRoutine.model_validate(replay)
            row = self._locked(connection, identity, routine_id)
            self._expected(row, request.expected_revision)
            if row["lifecycle_state"] == RoutineLifecycle.ARCHIVED.value:
                raise GovernedDomainConflict("An archived routine cannot be activated.")
            definition = self._definition(row)
            if request.action == RoutineActivationAction.ACTIVATE:
                if row["lifecycle_state"] == RoutineLifecycle.ACTIVE.value:
                    raise GovernedDomainConflict("The routine is already active.")
                if not self.execution_available():
                    raise GovernedDomainUnavailable(
                        "Governed routine execution is not configured or its worker is unavailable."
                    )
                consents = self._consent_values(row)
                if any(
                    state != "ENABLED"
                    for state in consents.values()
                ):
                    raise GovernedDomainConflict(
                        "All routine consent scopes must be enabled before activation."
                    )
                self._require_source_access(identity, definition)
                self._require_source_preferences(connection, identity, definition)
                now = connection.execute(
                    "SELECT CURRENT_TIMESTAMP AS now"
                ).fetchone()["now"]
                reference = max(now, request.start_at or now)
                if definition.trigger_type == RoutineTriggerType.SCHEDULED:
                    next_run_at = preview_next_run(definition, after=reference)
                    execution_mode = "SCHEDULED"
                else:
                    next_run_at = None
                    execution_mode = "WEBHOOK"
                target = RoutineLifecycle.ACTIVE.value
                event_type = "ACTIVATED"
            else:
                if row["lifecycle_state"] != RoutineLifecycle.ACTIVE.value:
                    raise GovernedDomainConflict("Only an active routine can be deactivated.")
                next_run_at = None
                target = RoutineLifecycle.DRAFT.value
                execution_mode = "DRY_RUN_ONLY"
                event_type = "DEACTIVATED"
            revision = int(row["revision"]) + 1
            connection.execute(
                """UPDATE ai_personal_routines
                      SET lifecycle_state = %s, execution_mode = %s,
                          next_run_at = %s, revision = %s,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE routine_id = %s AND tenant_id = %s AND user_id = %s""",
                (
                    target,
                    execution_mode,
                    next_run_at,
                    revision,
                    routine_id,
                    identity.tenant_id,
                    identity.user_id,
                ),
            )
            routine = self._routine(
                self._locked(connection, identity, routine_id, lock=False)
            )
            self._record_command(
                connection,
                identity,
                routine_id,
                "ACTIVATION",
                request,
                proof,
                routine,
            )
            self._event(
                connection,
                identity,
                routine_id,
                request.command_id,
                event_type,
                row["lifecycle_state"],
                target,
                revision,
                request.reason_code,
                proof.request_fingerprint,
                request.change_reason,
            )
            return routine

    def trigger_run(
        self,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        request: TriggerRoutineRunRequest,
    ) -> RoutineExecutionRun:
        proof = self._proof(identity, "TRIGGER_RUN", routine_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(
                connection, identity, request.command_id, "TRIGGER_RUN", proof
            )
            if replay is not None:
                return RoutineExecutionRun.model_validate(replay)
            routine = self._locked(connection, identity, routine_id)
            self._expected(routine, request.expected_revision)
            if routine["lifecycle_state"] != RoutineLifecycle.ACTIVE.value:
                raise GovernedDomainConflict("Only an active routine can run.")
            if not self.execution_available():
                raise GovernedDomainUnavailable(
                    "Governed routine execution is unavailable."
                )
            definition = self._definition(routine)
            self._require_source_access(identity, definition)
            self._require_source_preferences(connection, identity, definition)
            now = connection.execute(
                "SELECT CURRENT_TIMESTAMP AS now"
            ).fetchone()["now"]
            run_id = uuid4()
            connection.execute(
                """INSERT INTO ai_personal_routine_executions (
                       routine_run_id, routine_id, tenant_id, user_id,
                       routine_revision, trigger_type, scheduled_for,
                       maximum_attempts, correlation_id)
                   VALUES (%s, %s, %s, %s, %s, 'MANUAL', %s, %s, %s)""",
                (
                    run_id,
                    routine_id,
                    identity.tenant_id,
                    identity.user_id,
                    routine["revision"],
                    now,
                    definition.retry_policy.maximum_attempts,
                    identity.correlation_id,
                ),
            )
            self._require_monthly_run_budget(
                connection,
                run_id,
                identity.tenant_id,
                identity.user_id,
                routine_id,
                definition.budget.maximum_runs_per_month,
                now,
            )
            self._execution_event(
                connection,
                run_id=run_id,
                routine_id=routine_id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                event_type="QUEUED",
                previous_state=None,
                current_state="QUEUED",
                version=1,
                attempt_count=0,
                lease_generation=0,
            )
            run = self._execution(
                connection,
                self._locked_execution(
                    connection, identity, routine_id, run_id, lock=False
                ),
            )
            self._record_command(
                connection,
                identity,
                routine_id,
                "TRIGGER_RUN",
                request,
                proof,
                run,
            )
            self._event(
                connection,
                identity,
                routine_id,
                request.command_id,
                "RUN_TRIGGERED",
                routine["lifecycle_state"],
                routine["lifecycle_state"],
                routine["revision"],
                request.reason_code,
                proof.request_fingerprint,
                request.change_reason,
            )
            return run

    def list_runs(
        self,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        *,
        limit: int,
    ) -> list[RoutineExecutionRun]:
        with connect(self.database_url, row_factory=dict_row) as connection:
            self._locked(connection, identity, routine_id, lock=False)
            rows = connection.execute(
                EXECUTION_SELECT
                + " WHERE tenant_id = %s AND user_id = %s AND routine_id = %s"
                + " ORDER BY created_at DESC, routine_run_id DESC LIMIT %s",
                (identity.tenant_id, identity.user_id, routine_id, limit),
            ).fetchall()
            return [self._execution(connection, row) for row in rows]

    def get_run(
        self,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        run_id: UUID,
    ) -> RoutineExecutionRun:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = self._locked_execution(
                connection, identity, routine_id, run_id, lock=False
            )
            return self._execution(connection, row)

    def command_run(
        self,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        run_id: UUID,
        request: CommandRoutineRunRequest,
    ) -> RoutineExecutionRun:
        proof = self._proof(identity, "RUN_COMMAND", routine_id, request)
        with connect(self.database_url, row_factory=dict_row) as connection:
            replay = self._replay(
                connection, identity, request.command_id, "RUN_COMMAND", proof
            )
            if replay is not None:
                return RoutineExecutionRun.model_validate(replay)
            row = self._locked_execution(connection, identity, routine_id, run_id)
            if int(row["version"]) != request.expected_revision:
                raise GovernedDomainConflict("The routine run version has changed.")
            action = request.action
            current = row["run_state"]
            compensation_requested = False
            recovery_requested = False
            if action == RoutineRunCommand.RETRY:
                if current not in {"PARTIAL", "FAILED"}:
                    raise GovernedDomainConflict("Only a failed or partial run can retry.")
                target = "QUEUED"
                event_type = "RUN_RETRY_REQUESTED"
            elif action == RoutineRunCommand.SKIP_QUARANTINED_AND_CONTINUE:
                if current != "PARTIAL":
                    raise GovernedDomainConflict(
                        "Only a partial run can continue without quarantined records."
                    )
                if not row["provider_receipt_envelope"]:
                    raise GovernedDomainConflict(
                        "Continuing a partial run requires verified provider evidence."
                    )
                target = "QUEUED"
                event_type = "RUN_SKIP_QUARANTINED_REQUESTED"
                recovery_requested = True
            elif action == RoutineRunCommand.CANCEL:
                if current not in {
                    "QUEUED",
                    "CLAIMED",
                    "RUNNING",
                    "RETRY_SCHEDULED",
                }:
                    raise GovernedDomainConflict("This routine run cannot be cancelled.")
                target = "CANCELLED"
                event_type = "RUN_CANCELLED"
            else:
                if current not in {"PARTIAL", "COMPLETED", "FAILED"}:
                    raise GovernedDomainConflict(
                        "Only a terminal run can request compensation."
                    )
                if not row["provider_receipt_envelope"]:
                    raise GovernedDomainConflict(
                        "Compensation requires a verified provider receipt."
                    )
                routine = self._locked(connection, identity, routine_id, lock=False)
                if not self._definition(routine).compensation_policy.enabled:
                    raise GovernedDomainConflict(
                        "Compensation is disabled for this routine."
                    )
                target = "QUEUED"
                event_type = "RUN_COMPENSATION_REQUESTED"
                compensation_requested = True
            if recovery_requested:
                recovery_action = action.value
                recovery_command_id = request.command_id
                recovery_decision_envelope = self.codec.encrypt_json(
                    {
                        "commandId": str(request.command_id),
                        "reasonCode": request.reason_code,
                        "changeReason": request.change_reason,
                        "requestFingerprint": proof.request_fingerprint,
                    },
                    tenant_id=identity.tenant_id,
                    resource_type="personal-routine-execution",
                    resource_id=str(run_id),
                    field="recovery-decision",
                )
            elif action == RoutineRunCommand.RETRY:
                recovery_action = None
                recovery_command_id = None
                recovery_decision_envelope = None
            else:
                recovery_action = row["recovery_action"]
                recovery_command_id = row["recovery_command_id"]
                recovery_decision_envelope = row["recovery_decision_envelope"]
            version = int(row["version"]) + 1
            terminal = target == "CANCELLED"
            connection.execute(
                """UPDATE ai_personal_routine_executions
                      SET run_state = %s, version = %s,
                          next_attempt_at = CASE WHEN %s THEN NULL ELSE CURRENT_TIMESTAMP END,
                          attempt_count = CASE WHEN %s THEN 0 ELSE attempt_count END,
                          lease_token = NULL, lease_expires_at = NULL,
                          compensation_requested = %s,
                          recovery_action = %s, recovery_command_id = %s,
                          recovery_decision_envelope = %s,
                          safe_error_code = NULL, recovery_hint = NULL,
                          receipt_id = NULL, receipt_fingerprint = NULL,
                          provider_receipt_envelope = CASE
                              WHEN %s THEN provider_receipt_envelope ELSE NULL END,
                          result_sha256 = CASE WHEN %s THEN result_sha256 ELSE NULL END,
                          completed_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE routine_run_id = %s AND tenant_id = %s AND user_id = %s""",
                (
                    target,
                    version,
                    terminal,
                    target == "QUEUED",
                    compensation_requested,
                    recovery_action,
                    recovery_command_id,
                    recovery_decision_envelope,
                    compensation_requested or recovery_requested,
                    compensation_requested or recovery_requested,
                    terminal,
                    run_id,
                    identity.tenant_id,
                    identity.user_id,
                ),
            )
            self._execution_event(
                connection,
                run_id=run_id,
                routine_id=routine_id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                event_type=event_type,
                previous_state=current,
                current_state=target,
                version=version,
                attempt_count=row["attempt_count"],
                lease_generation=row["lease_generation"],
            )
            updated = self._execution(
                connection,
                self._locked_execution(
                    connection, identity, routine_id, run_id, lock=False
                ),
            )
            self._record_command(
                connection,
                identity,
                routine_id,
                "RUN_COMMAND",
                request,
                proof,
                updated,
            )
            self._event(
                connection,
                identity,
                routine_id,
                request.command_id,
                event_type,
                current,
                target,
                version,
                request.reason_code,
                proof.request_fingerprint,
                request.change_reason,
            )
            return updated
