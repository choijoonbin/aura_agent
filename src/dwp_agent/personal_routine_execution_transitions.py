from __future__ import annotations

from uuid import UUID, uuid4

from psycopg import connect
from psycopg.rows import dict_row

from .personal_routine_contracts import RoutineDefinition
from .personal_routine_execution_provider import RoutineExecutionProviderUnavailable


class PersonalRoutineExecutionTransitions:
    def _finalize_provider_result(
        self,
        lease: RoutineExecutionLease,
        *,
        state: str,
        provider_receipt_id: str,
        result_sha256: str,
        evidence_count: int,
        proposals_created: int,
        actions_created: int,
        tokens_used: int,
        elapsed_ms: int,
        notification_state: str,
        compensation_required: bool,
        safe_error_code: str | None,
        recovery_hint: str | None,
        authorization_decision_revision: int,
        authorized_sources: list[str],
    ) -> None:
        with connect(self.database_url, row_factory=dict_row) as connection:
            now = connection.execute(
                "SELECT CURRENT_TIMESTAMP AS now"
            ).fetchone()["now"]
            receipt_id = uuid4() if state in {"COMPLETED", "COMPENSATED"} else None
            envelope = self.codec.encrypt_json(
                {
                    "providerReceiptId": provider_receipt_id,
                    "authorizationDecisionRevision": authorization_decision_revision,
                    "authorizedSources": authorized_sources,
                },
                tenant_id=lease.tenant_id,
                resource_type="personal-routine-execution",
                resource_id=str(lease.routine_run_id),
                field="provider-receipt",
            )
            receipt_fingerprint = None
            if receipt_id is not None:
                receipt_fingerprint = self.fingerprints.value(
                    tenant_id=lease.tenant_id,
                    purpose="personal-routine-execution-receipt",
                    payload={
                        "receiptId": str(receipt_id),
                        "routineRunId": str(lease.routine_run_id),
                        "routineId": str(lease.routine_id),
                        "routineRevision": lease.routine_revision,
                        "terminalState": state,
                        "providerReceiptId": provider_receipt_id,
                        "resultSha256": result_sha256,
                        "evidenceCount": evidence_count,
                        "proposalsCreated": proposals_created,
                        "approvalGatedActionsCreated": actions_created,
                        "notificationState": notification_state,
                        "authorizationDecisionRevision": authorization_decision_revision,
                        "authorizedSources": authorized_sources,
                        "completedAt": now.isoformat(),
                    },
                )
            row = connection.execute(
                """UPDATE ai_personal_routine_executions
                      SET run_state = %s, version = version + 1,
                          evidence_count = %s, proposals_created = %s,
                          approval_gated_actions_created = %s,
                          external_writes_performed = 0, tokens_used = %s,
                          elapsed_ms = %s, notification_state = %s,
                          provider_receipt_envelope = %s, result_sha256 = %s,
                          receipt_id = %s, receipt_fingerprint = %s,
                          safe_error_code = %s, recovery_hint = %s,
                          compensation_required = %s,
                          compensation_requested = FALSE,
                          lease_token = NULL, lease_expires_at = NULL,
                          completed_at = %s, updated_at = %s
                    WHERE routine_run_id = %s AND tenant_id = %s
                      AND lease_generation = %s AND lease_token = %s
                      AND run_state IN ('RUNNING', 'COMPENSATING')
                    RETURNING version, attempt_count, lease_generation""",
                (
                    state,
                    evidence_count,
                    proposals_created,
                    actions_created,
                    tokens_used,
                    elapsed_ms,
                    notification_state,
                    envelope,
                    result_sha256,
                    receipt_id,
                    receipt_fingerprint,
                    safe_error_code,
                    recovery_hint,
                    compensation_required,
                    now,
                    now,
                    lease.routine_run_id,
                    lease.tenant_id,
                    lease.generation,
                    lease.lease_token,
                ),
            ).fetchone()
            if row is None:
                return
            self._event(
                connection,
                run_id=lease.routine_run_id,
                routine_id=lease.routine_id,
                tenant_id=lease.tenant_id,
                user_id=lease.user_id,
                event_type=state,
                previous_state="COMPENSATING" if lease.compensation_requested else "RUNNING",
                current_state=state,
                version=row["version"],
                attempt_count=row["attempt_count"],
                generation=row["lease_generation"],
                safe_error_code=safe_error_code,
                receipt_fingerprint=receipt_fingerprint,
            )

    def _retry_or_fail(self, lease: RoutineExecutionLease, safe_error_code: str) -> None:
        with connect(self.database_url, row_factory=dict_row) as connection:
            definition = self._definition_for_run(connection, lease)
            exhausted = lease.attempt_count >= lease.maximum_attempts
            state = "FAILED" if exhausted else "RETRY_SCHEDULED"
            backoff = min(
                3_600,
                int(
                    definition.retry_policy.initial_backoff_seconds
                    * definition.retry_policy.backoff_multiplier
                    ** max(0, lease.attempt_count - 1)
                ),
            )
            row = connection.execute(
                """UPDATE ai_personal_routine_executions
                      SET run_state = %s, version = version + 1,
                          next_attempt_at = CASE WHEN %s THEN NULL
                              ELSE CURRENT_TIMESTAMP + (%s * INTERVAL '1 second') END,
                          lease_token = NULL, lease_expires_at = NULL,
                          safe_error_code = %s,
                          recovery_hint = %s,
                          completed_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE routine_run_id = %s AND tenant_id = %s
                      AND lease_generation = %s AND lease_token = %s
                      AND run_state IN ('RUNNING', 'COMPENSATING')
                    RETURNING version, attempt_count, lease_generation""",
                (
                    state,
                    exhausted,
                    backoff,
                    safe_error_code,
                    "Retry is scheduled." if not exhausted else "Review configuration and retry the failed run.",
                    exhausted,
                    lease.routine_run_id,
                    lease.tenant_id,
                    lease.generation,
                    lease.lease_token,
                ),
            ).fetchone()
            if row is None:
                return
            self._event(
                connection,
                run_id=lease.routine_run_id,
                routine_id=lease.routine_id,
                tenant_id=lease.tenant_id,
                user_id=lease.user_id,
                event_type=state,
                previous_state="COMPENSATING" if lease.compensation_requested else "RUNNING",
                current_state=state,
                version=row["version"],
                attempt_count=row["attempt_count"],
                generation=row["lease_generation"],
                safe_error_code=safe_error_code,
            )

    def _transition_claimed(self, lease: RoutineExecutionLease, target: str) -> bool:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """UPDATE ai_personal_routine_executions
                      SET run_state = %s, version = version + 1,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE routine_run_id = %s AND tenant_id = %s
                      AND run_state = 'CLAIMED' AND lease_generation = %s
                      AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP
                    RETURNING version, attempt_count, lease_generation""",
                (
                    target,
                    lease.routine_run_id,
                    lease.tenant_id,
                    lease.generation,
                    lease.lease_token,
                ),
            ).fetchone()
            if row is None:
                return False
            self._event(
                connection,
                run_id=lease.routine_run_id,
                routine_id=lease.routine_id,
                tenant_id=lease.tenant_id,
                user_id=lease.user_id,
                event_type=target,
                previous_state="CLAIMED",
                current_state=target,
                version=row["version"],
                attempt_count=row["attempt_count"],
                generation=row["lease_generation"],
            )
            return True

    def _fail_exhausted_leases(self) -> None:
        with connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                """UPDATE ai_personal_routine_executions
                      SET run_state = 'FAILED', version = version + 1,
                          lease_token = NULL, lease_expires_at = NULL,
                          safe_error_code = 'ROUTINE_EXECUTION_LEASE_EXHAUSTED',
                          recovery_hint = 'Review the interrupted run and retry it explicitly.',
                          completed_at = CURRENT_TIMESTAMP,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE run_state IN ('CLAIMED', 'RUNNING', 'COMPENSATING')
                      AND lease_expires_at <= CURRENT_TIMESTAMP
                      AND attempt_count >= maximum_attempts
                    RETURNING routine_run_id, routine_id, tenant_id, user_id,
                              version, attempt_count, lease_generation"""
            ).fetchall()
            for row in rows:
                self._event(
                    connection,
                    run_id=row["routine_run_id"],
                    routine_id=row["routine_id"],
                    tenant_id=row["tenant_id"],
                    user_id=row["user_id"],
                    event_type="FAILED",
                    previous_state="RUNNING",
                    current_state="FAILED",
                    version=row["version"],
                    attempt_count=row["attempt_count"],
                    generation=row["lease_generation"],
                    safe_error_code="ROUTINE_EXECUTION_LEASE_EXHAUSTED",
                )

    def _load_definition(self, lease: RoutineExecutionLease) -> RoutineDefinition:
        with connect(self.database_url, row_factory=dict_row) as connection:
            return self._definition_for_run(connection, lease)

    def _definition_for_run(self, connection, lease: RoutineExecutionLease) -> RoutineDefinition:
        row = connection.execute(
            """SELECT tenant_id, routine_id, definition_envelope
                 FROM ai_personal_routines
                WHERE routine_id = %s AND tenant_id = %s AND user_id = %s""",
            (lease.routine_id, lease.tenant_id, lease.user_id),
        ).fetchone()
        if row is None:
            raise RoutineExecutionProviderUnavailable("ROUTINE_DEFINITION_UNAVAILABLE")
        return self._definition(row)

    def _definition(self, row) -> RoutineDefinition:
        return RoutineDefinition.model_validate(
            self.codec.decrypt_json(
                row["definition_envelope"],
                tenant_id=int(row["tenant_id"]),
                resource_type="personal-routine",
                resource_id=str(row["routine_id"]),
                field="definition",
            )
        )

    @staticmethod
    def _event(
        connection,
        *,
        run_id: UUID,
        routine_id: UUID,
        tenant_id: int,
        user_id: str,
        event_type: str,
        previous_state: str | None,
        current_state: str,
        version: int,
        attempt_count: int,
        generation: int,
        safe_error_code: str | None = None,
        receipt_fingerprint: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO ai_personal_routine_execution_events (
                   event_id, routine_run_id, routine_id, tenant_id, user_id,
                   event_type, previous_state, current_state, version,
                   attempt_count, lease_generation, safe_error_code,
                   receipt_fingerprint)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(),
                run_id,
                routine_id,
                tenant_id,
                user_id,
                event_type,
                previous_state,
                current_state,
                version,
                attempt_count,
                generation,
                safe_error_code,
                receipt_fingerprint,
            ),
        )
