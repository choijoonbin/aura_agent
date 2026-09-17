from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg import connect
from psycopg.rows import dict_row

from .personal_routine_contracts import RoutineDefinition
from .personal_routine_capabilities import routine_runtime_capabilities
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
        runtime_controls: dict[str, object] | None = None,
    ) -> None:
        with connect(self.database_url, row_factory=dict_row) as connection:
            now = connection.execute(
                "SELECT CURRENT_TIMESTAMP AS now"
            ).fetchone()["now"]
            receipt_id = uuid4() if state in {"COMPLETED", "COMPENSATED"} else None
            provider_evidence: dict[str, object] = {
                "providerReceiptId": provider_receipt_id,
                "authorizationDecisionRevision": authorization_decision_revision,
                "authorizedSources": authorized_sources,
            }
            if lease.recovery_action is not None:
                provider_evidence["recoveryAction"] = lease.recovery_action
                provider_evidence["recoveryCommandId"] = str(lease.recovery_command_id)
            if runtime_controls is not None:
                provider_evidence["runtimeControls"] = runtime_controls
            envelope = self.codec.encrypt_json(
                provider_evidence,
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
                        **(
                            {"runtimeControls": runtime_controls}
                            if runtime_controls is not None
                            else {}
                        ),
                        "completedAt": now.isoformat(),
                        **(
                            {
                                "recoveryAction": lease.recovery_action,
                                "recoveryCommandId": str(lease.recovery_command_id),
                            }
                            if lease.recovery_action is not None
                            else {}
                        ),
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
            if exhausted:
                self._auto_quarantine(
                    connection,
                    routine_run_id=lease.routine_run_id,
                    routine_id=lease.routine_id,
                    tenant_id=lease.tenant_id,
                    user_id=lease.user_id,
                    routine_revision=lease.routine_revision,
                    correlation_id=lease.correlation_id,
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
                """WITH exhausted AS (
                       SELECT routine_run_id, run_state AS previous_state
                         FROM ai_personal_routine_executions
                        WHERE run_state IN ('CLAIMED', 'RUNNING', 'COMPENSATING')
                          AND lease_expires_at <= CURRENT_TIMESTAMP
                          AND attempt_count >= maximum_attempts
                        FOR UPDATE SKIP LOCKED
                   )
                   UPDATE ai_personal_routine_executions AS execution
                      SET run_state = 'FAILED', version = version + 1,
                          lease_token = NULL, lease_expires_at = NULL,
                          safe_error_code = 'ROUTINE_EXECUTION_LEASE_EXHAUSTED',
                          recovery_hint = 'Review the interrupted run and retry it explicitly.',
                          completed_at = CURRENT_TIMESTAMP,
                          updated_at = CURRENT_TIMESTAMP
                     FROM exhausted
                    WHERE execution.routine_run_id = exhausted.routine_run_id
                    RETURNING execution.routine_run_id, execution.routine_id,
                              execution.tenant_id, execution.user_id,
                              execution.routine_revision, execution.correlation_id,
                              execution.version, execution.attempt_count,
                              execution.lease_generation, exhausted.previous_state"""
            ).fetchall()
            for row in rows:
                self._event(
                    connection,
                    run_id=row["routine_run_id"],
                    routine_id=row["routine_id"],
                    tenant_id=row["tenant_id"],
                    user_id=row["user_id"],
                    event_type="FAILED",
                    previous_state=row["previous_state"],
                    current_state="FAILED",
                    version=row["version"],
                    attempt_count=row["attempt_count"],
                    generation=row["lease_generation"],
                    safe_error_code="ROUTINE_EXECUTION_LEASE_EXHAUSTED",
                )
                self._auto_quarantine(
                    connection,
                    routine_run_id=row["routine_run_id"],
                    routine_id=row["routine_id"],
                    tenant_id=int(row["tenant_id"]),
                    user_id=row["user_id"],
                    routine_revision=int(row["routine_revision"]),
                    correlation_id=row["correlation_id"],
                    safe_error_code="ROUTINE_EXECUTION_LEASE_EXHAUSTED",
                )

    def _auto_quarantine(
        self,
        connection,
        *,
        routine_run_id: UUID,
        routine_id: UUID,
        tenant_id: int,
        user_id: str,
        routine_revision: int,
        correlation_id: str,
        safe_error_code: str,
    ) -> None:
        """Pause the exact failing revision and seal one replay-safe audit snapshot."""
        row = connection.execute(
            """UPDATE ai_personal_routines
                  SET lifecycle_state = 'PAUSED', execution_mode = 'DRY_RUN_ONLY',
                      next_run_at = NULL, revision = revision + 1,
                      updated_at = CURRENT_TIMESTAMP
                WHERE routine_id = %s AND tenant_id = %s AND user_id = %s
                  AND revision = %s AND lifecycle_state = 'ACTIVE'
                RETURNING routine_id, tenant_id, user_id, lifecycle_state,
                          consent_state, source_access_consent_state,
                          analysis_consent_state, proposal_delivery_consent_state,
                          execution_mode, revision, definition_envelope,
                          next_run_at, created_at, updated_at""",
            (routine_id, tenant_id, user_id, routine_revision),
        ).fetchone()
        if row is None:
            return

        command_id = uuid5(
            NAMESPACE_URL,
            f"dwp:routine:auto-quarantine:{tenant_id}:{routine_run_id}:{routine_revision}",
        )
        request_fingerprint = self.fingerprints.value(
            tenant_id=tenant_id,
            purpose="personal-routine-auto-quarantine",
            payload={
                "routineRunId": str(routine_run_id),
                "routineId": str(routine_id),
                "failedRevision": routine_revision,
                "safeErrorCode": safe_error_code,
            },
        )
        session_fingerprint = self.fingerprints.value(
            tenant_id=tenant_id,
            purpose="auth-session",
            payload={"sessionId": "SYSTEM_ROUTINE_EXECUTION_WORKER"},
        )
        definition = self._definition(row)
        capabilities = routine_runtime_capabilities()
        snapshot = {
            "routineId": str(routine_id),
            "lifecycleState": row["lifecycle_state"],
            "consentState": row["consent_state"],
            "consents": {
                "sourceAccess": row["source_access_consent_state"],
                "analysis": row["analysis_consent_state"],
                "proposalDelivery": row["proposal_delivery_consent_state"],
            },
            "executionMode": row["execution_mode"],
            "revision": int(row["revision"]),
            "definition": definition.model_dump(mode="json", by_alias=True),
            "schedulingAvailable": False,
            "nextRunAt": None,
            "capabilities": capabilities.model_dump(mode="json", by_alias=True),
            "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat(),
        }
        result_envelope = self.codec.encrypt_json(
            snapshot,
            tenant_id=tenant_id,
            resource_type="personal-routine-command",
            resource_id=str(command_id),
            field="result",
        )
        connection.execute(
            """INSERT INTO ai_personal_routine_commands (
                   tenant_id, user_id, command_id, routine_id, command_type,
                   session_fingerprint, request_fingerprint, result_envelope)
               VALUES (%s, %s, %s, %s, 'AUTO_QUARANTINE', %s, %s, %s)
               ON CONFLICT (tenant_id, user_id, command_id) DO NOTHING""",
            (
                tenant_id,
                user_id,
                command_id,
                routine_id,
                session_fingerprint,
                request_fingerprint,
                result_envelope,
            ),
        )
        event_id = uuid4()
        change_reason_envelope = self.codec.encrypt_json(
            {
                "changeReason": (
                    "The governed routine worker quarantined the failing revision after "
                    "its bounded retry policy was exhausted."
                ),
                "routineRunId": str(routine_run_id),
                "safeErrorCode": safe_error_code,
            },
            tenant_id=tenant_id,
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
               VALUES (%s, %s, %s, %s, 'SYSTEM_ROUTINE_EXECUTION_WORKER',
                       %s, %s, 'AUTO_QUARANTINED', 'ACTIVE', 'PAUSED',
                       %s, %s, 'ROUTINE_RETRY_POLICY_EXHAUSTED', %s)""",
            (
                event_id,
                routine_id,
                tenant_id,
                user_id,
                correlation_id,
                command_id,
                row["revision"],
                request_fingerprint,
                change_reason_envelope,
            ),
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
