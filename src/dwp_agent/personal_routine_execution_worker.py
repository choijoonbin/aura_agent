from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from uuid import UUID, uuid4

from psycopg import connect
from psycopg.rows import dict_row

from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedFingerprints,
    GovernedPayloadCodec,
)
from .governed_worker_runtime import (
    register_governed_worker_heartbeat,
    remove_governed_worker_heartbeat,
)
from .personal_routine_contracts import RoutineDefinition
from .personal_routine_execution_transitions import PersonalRoutineExecutionTransitions
from .personal_routine_execution_provider import (
    RoutineExecutionProvider,
    RoutineExecutionProviderUnavailable,
)
from .personal_routine_schedule import preview_next_run


LOGGER = logging.getLogger(__name__)
WORKER_TYPE = "ROUTINE_EXECUTION"


@dataclass(frozen=True)
class RoutineExecutionLease:
    routine_run_id: UUID
    routine_id: UUID
    tenant_id: int
    user_id: str
    routine_revision: int
    generation: int
    lease_token: UUID
    attempt_count: int
    maximum_attempts: int
    correlation_id: str
    compensation_requested: bool


class PostgresPersonalRoutineExecutionWorker(PersonalRoutineExecutionTransitions):
    def __init__(
        self,
        database_url: str,
        *,
        provider: RoutineExecutionProvider | None = None,
        codec: GovernedPayloadCodec | None = None,
        fingerprints: GovernedFingerprints | None = None,
    ) -> None:
        self.database_url = database_url
        self.provider = provider or RoutineExecutionProvider()
        self.codec = codec or GovernedPayloadCodec()
        self.fingerprints = fingerprints or GovernedFingerprints.load()

    def process_once(self) -> bool:
        scheduled = self.schedule_due_once()
        self._fail_exhausted_leases()
        lease = self._claim()
        if lease is None:
            return scheduled
        self._process(lease)
        return True

    def schedule_due_once(self) -> bool:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT routine_id, tenant_id, user_id, revision,
                          definition_envelope, next_run_at
                     FROM ai_personal_routines
                    WHERE lifecycle_state = 'ACTIVE'
                      AND execution_mode = 'SCHEDULED'
                      AND next_run_at <= CURRENT_TIMESTAMP
                    ORDER BY next_run_at, routine_id
                    FOR UPDATE SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if row is None:
                return False
            definition = self._definition(row)
            scheduled_for = row["next_run_at"]
            count = connection.execute(
                """SELECT COUNT(*) AS count
                     FROM ai_personal_routine_executions
                    WHERE tenant_id = %s AND user_id = %s AND routine_id = %s
                      AND created_at >= date_trunc('month', CURRENT_TIMESTAMP)""",
                (row["tenant_id"], row["user_id"], row["routine_id"]),
            ).fetchone()["count"]
            run_id = uuid4()
            correlation_id = f"routine-scheduler:{run_id}"
            budget_exhausted = int(count) >= definition.budget.maximum_runs_per_month
            if budget_exhausted:
                connection.execute(
                    """INSERT INTO ai_personal_routine_executions (
                           routine_run_id, routine_id, tenant_id, user_id,
                           routine_revision, trigger_type, run_state, scheduled_for,
                           maximum_attempts, correlation_id, safe_error_code,
                           recovery_hint, completed_at)
                       VALUES (%s, %s, %s, %s, %s, 'SCHEDULED', 'FAILED', %s,
                               %s, %s, 'ROUTINE_MONTHLY_BUDGET_EXHAUSTED',
                               'Increase the approved monthly budget or wait for the next budget window.',
                               CURRENT_TIMESTAMP)
                       ON CONFLICT (routine_id, routine_revision, scheduled_for, trigger_type)
                       DO NOTHING""",
                    (
                        run_id,
                        row["routine_id"],
                        row["tenant_id"],
                        row["user_id"],
                        row["revision"],
                        scheduled_for,
                        definition.retry_policy.maximum_attempts,
                        correlation_id,
                    ),
                )
                current_state = "FAILED"
                safe_error = "ROUTINE_MONTHLY_BUDGET_EXHAUSTED"
            else:
                connection.execute(
                    """INSERT INTO ai_personal_routine_executions (
                           routine_run_id, routine_id, tenant_id, user_id,
                           routine_revision, trigger_type, scheduled_for,
                           maximum_attempts, correlation_id)
                       VALUES (%s, %s, %s, %s, %s, 'SCHEDULED', %s, %s, %s)
                       ON CONFLICT (routine_id, routine_revision, scheduled_for, trigger_type)
                       DO NOTHING""",
                    (
                        run_id,
                        row["routine_id"],
                        row["tenant_id"],
                        row["user_id"],
                        row["revision"],
                        scheduled_for,
                        definition.retry_policy.maximum_attempts,
                        correlation_id,
                    ),
                )
                current_state = "QUEUED"
                safe_error = None
            inserted = connection.execute(
                """SELECT routine_run_id, run_state, version, attempt_count,
                          lease_generation, safe_error_code
                     FROM ai_personal_routine_executions
                    WHERE routine_id = %s AND routine_revision = %s
                      AND scheduled_for = %s AND trigger_type = 'SCHEDULED'""",
                (row["routine_id"], row["revision"], scheduled_for),
            ).fetchone()
            if inserted is not None and inserted["routine_run_id"] == run_id:
                self._event(
                    connection,
                    run_id=run_id,
                    routine_id=row["routine_id"],
                    tenant_id=row["tenant_id"],
                    user_id=row["user_id"],
                    event_type="SCHEDULED",
                    previous_state=None,
                    current_state=current_state,
                    version=1,
                    attempt_count=0,
                    generation=0,
                    safe_error_code=safe_error,
                )
            try:
                next_run_at = preview_next_run(definition, after=scheduled_for)
                connection.execute(
                    """UPDATE ai_personal_routines
                          SET next_run_at = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE routine_id = %s AND tenant_id = %s AND user_id = %s""",
                    (
                        next_run_at,
                        row["routine_id"],
                        row["tenant_id"],
                        row["user_id"],
                    ),
                )
            except GovernedDomainConflict:
                connection.execute(
                    """UPDATE ai_personal_routines
                          SET lifecycle_state = 'PAUSED',
                              execution_mode = 'DRY_RUN_ONLY', next_run_at = NULL,
                              revision = revision + 1, updated_at = CURRENT_TIMESTAMP
                        WHERE routine_id = %s AND tenant_id = %s AND user_id = %s""",
                    (row["routine_id"], row["tenant_id"], row["user_id"]),
                )
            return True

    def _claim(self) -> RoutineExecutionLease | None:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT routine_run_id, routine_id, tenant_id, user_id,
                          routine_revision, run_state, version, attempt_count,
                          maximum_attempts, lease_generation, correlation_id,
                          compensation_requested
                     FROM ai_personal_routine_executions
                    WHERE (
                        (run_state IN ('QUEUED', 'RETRY_SCHEDULED')
                            AND COALESCE(next_attempt_at, scheduled_for) <= CURRENT_TIMESTAMP)
                        OR (run_state IN ('CLAIMED', 'RUNNING', 'COMPENSATING')
                            AND lease_expires_at <= CURRENT_TIMESTAMP)
                    )
                      AND attempt_count < maximum_attempts
                    ORDER BY COALESCE(next_attempt_at, scheduled_for), created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            token = uuid4()
            generation = int(row["lease_generation"]) + 1
            attempt = int(row["attempt_count"]) + 1
            version = int(row["version"]) + 1
            updated = connection.execute(
                """UPDATE ai_personal_routine_executions
                      SET run_state = 'CLAIMED', version = %s,
                          attempt_count = %s, lease_generation = %s,
                          lease_token = %s,
                          lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '5 minutes',
                          next_attempt_at = NULL,
                          started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                          completed_at = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE routine_run_id = %s
                    RETURNING routine_run_id""",
                (version, attempt, generation, token, row["routine_run_id"]),
            ).fetchone()
            if updated is None:
                return None
            self._event(
                connection,
                run_id=row["routine_run_id"],
                routine_id=row["routine_id"],
                tenant_id=row["tenant_id"],
                user_id=row["user_id"],
                event_type="CLAIMED",
                previous_state=row["run_state"],
                current_state="CLAIMED",
                version=version,
                attempt_count=attempt,
                generation=generation,
            )
            return RoutineExecutionLease(
                routine_run_id=row["routine_run_id"],
                routine_id=row["routine_id"],
                tenant_id=int(row["tenant_id"]),
                user_id=row["user_id"],
                routine_revision=int(row["routine_revision"]),
                generation=generation,
                lease_token=token,
                attempt_count=attempt,
                maximum_attempts=int(row["maximum_attempts"]),
                correlation_id=row["correlation_id"],
                compensation_requested=bool(row["compensation_requested"]),
            )

    def _process(self, lease: RoutineExecutionLease) -> None:
        target = "COMPENSATING" if lease.compensation_requested else "RUNNING"
        if not self._transition_claimed(lease, target):
            return
        try:
            if lease.compensation_requested:
                self._compensate(lease)
            else:
                self._execute(lease)
        except RoutineExecutionProviderUnavailable as error:
            self._retry_or_fail(lease, error.code)
        except Exception as error:
            LOGGER.warning(
                "Routine execution failed safely; run=%s error=%s",
                lease.routine_run_id,
                type(error).__name__,
            )
            self._retry_or_fail(lease, "ROUTINE_EXECUTION_INTERNAL_ERROR")

    def _execute(self, lease: RoutineExecutionLease) -> None:
        definition = self._load_definition(lease)
        result = self.provider.execute(
            routine_run_id=lease.routine_run_id,
            routine_id=lease.routine_id,
            routine_revision=lease.routine_revision,
            tenant_id=lease.tenant_id,
            user_id=lease.user_id,
            correlation_id=lease.correlation_id,
            definition=definition,
        )
        budget_exceeded = (
            result.tokensUsed > definition.budget.maximum_tokens_per_run
            or result.elapsedMs > definition.budget.maximum_minutes_per_run * 60_000
        )
        notification_incomplete = (
            (definition.notification_policy.notify_on_partial or definition.notification_policy.notify_on_failure)
            and result.notificationState
            in {"NOT_CONFIGURED", "FAILED"}
        )
        state = result.state
        safe_error = result.safeErrorCode
        recovery_hint = result.recoveryHint
        if budget_exceeded:
            state = "PARTIAL"
            safe_error = "ROUTINE_EXECUTION_BUDGET_EXCEEDED"
            recovery_hint = "Review the result and raise an approved runtime budget before retrying."
        elif notification_incomplete and state == "COMPLETED":
            state = "PARTIAL"
            safe_error = "ROUTINE_NOTIFICATION_INCOMPLETE"
            recovery_hint = "Configure notification delivery, then retry or acknowledge the partial run."
        self._finalize_provider_result(
            lease,
            state=state,
            provider_receipt_id=result.providerReceiptId,
            result_sha256=result.resultSha256,
            evidence_count=result.evidenceCount,
            proposals_created=result.proposalsCreated,
            actions_created=result.approvalGatedActionsCreated,
            tokens_used=result.tokensUsed,
            elapsed_ms=result.elapsedMs,
            notification_state=result.notificationState.value,
            compensation_required=(result.compensationRequired or budget_exceeded),
            safe_error_code=safe_error,
            recovery_hint=recovery_hint,
            authorization_decision_revision=result.authorizationDecisionRevision,
            authorized_sources=result.authorizedSources,
        )

    def _compensate(self, lease: RoutineExecutionLease) -> None:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT provider_receipt_envelope
                     FROM ai_personal_routine_executions
                    WHERE routine_run_id = %s AND tenant_id = %s""",
                (lease.routine_run_id, lease.tenant_id),
            ).fetchone()
            if row is None or row["provider_receipt_envelope"] is None:
                raise RoutineExecutionProviderUnavailable(
                    "ROUTINE_COMPENSATION_RECEIPT_UNAVAILABLE"
                )
            receipt = self.codec.decrypt_json(
                row["provider_receipt_envelope"],
                tenant_id=lease.tenant_id,
                resource_type="personal-routine-execution",
                resource_id=str(lease.routine_run_id),
                field="provider-receipt",
            )
        result = self.provider.compensate(
            routine_run_id=lease.routine_run_id,
            provider_receipt_id=str(receipt["providerReceiptId"]),
            tenant_id=lease.tenant_id,
            user_id=lease.user_id,
            correlation_id=lease.correlation_id,
        )
        self._finalize_provider_result(
            lease,
            state="COMPENSATED",
            provider_receipt_id=result.providerReceiptId,
            result_sha256=result.resultSha256,
            evidence_count=0,
            proposals_created=0,
            actions_created=0,
            tokens_used=0,
            elapsed_ms=0,
            notification_state="NOT_REQUIRED",
            compensation_required=False,
            safe_error_code=None,
            recovery_hint=None,
            authorization_decision_revision=int(
                receipt["authorizationDecisionRevision"]
            ),
            authorized_sources=list(receipt["authorizedSources"]),
        )






class PersonalRoutineExecutionMaintenance:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None or not _worker_enabled():
            return
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            return
        provider = RoutineExecutionProvider()
        if not provider.configured:
            return
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(database_url, provider),
            name="dwp-agent-routine-execution-worker",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=1)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        remove_governed_worker_heartbeat(WORKER_TYPE)
        self._ready.clear()

    def process_once(self, database_url: str) -> bool:
        return PostgresPersonalRoutineExecutionWorker(database_url).process_once()

    def _run(self, database_url: str, provider: RoutineExecutionProvider) -> None:
        try:
            self._require_database_schema(database_url)
            worker = PostgresPersonalRoutineExecutionWorker(
                database_url, provider=provider
            )
            register_governed_worker_heartbeat(WORKER_TYPE)
            self._ready.set()
            interval = _worker_interval_seconds()
            while not self._stop.is_set():
                register_governed_worker_heartbeat(WORKER_TYPE)
                if not worker.process_once():
                    self._stop.wait(interval)
        except Exception as error:
            LOGGER.error(
                "Routine execution worker stopped; error=%s", type(error).__name__
            )
        finally:
            self._ready.set()
            remove_governed_worker_heartbeat(WORKER_TYPE)

    @staticmethod
    def _require_database_schema(database_url: str) -> None:
        with connect(database_url) as connection:
            row = connection.execute(
                """SELECT to_regclass('ai_personal_routine_executions') IS NOT NULL
                          AND to_regclass('ai_personal_routine_execution_events') IS NOT NULL
                          AND EXISTS (
                              SELECT 1 FROM sys_schema_history WHERE version = 'V39'
                          )"""
            ).fetchone()
        if row != (True,):
            raise RuntimeError("Routine execution database schema is unavailable.")


def _worker_enabled() -> bool:
    return (
        os.getenv("DWP_ROUTINE_EXECUTION_WORKER_ENABLED", "false").strip().lower()
        == "true"
    )


def _worker_interval_seconds() -> float:
    try:
        value = float(os.getenv("DWP_ROUTINE_EXECUTION_WORKER_INTERVAL_SECONDS", "1"))
    except ValueError:
        value = 1.0
    return max(0.1, min(30.0, value))


MAINTENANCE = PersonalRoutineExecutionMaintenance()
