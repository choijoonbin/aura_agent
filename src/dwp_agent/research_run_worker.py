from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg import connect
from psycopg.rows import dict_row

from .dwaion_workflow_contracts import ResearchProgress, ResearchRunState
from .personal_domain_security import PersonalDomainIdentity
from .research_executor import ResearchExecutionOutcome, ResearchExecutor
from .research_plan_store import ResearchPlanStore
from .research_run_runtime import (
    ResearchExecutionAuthorization,
    ResearchRunDeadlineExceeded,
    ResearchRunLease,
    ResearchRunLeaseLost,
    ResearchRuntimeControls,
)
from .research_run_store import ResearchRunStore


LOGGER = logging.getLogger(__name__)


class PostgresResearchRunWorker:
    def __init__(
        self,
        database_url: str,
        *,
        executor: ResearchExecutor | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 300,
    ) -> None:
        if not 30 <= lease_seconds <= 300:
            raise ValueError("Research worker lease must be between 30 and 300 seconds.")
        self.database_url = database_url
        self.plan_store = ResearchPlanStore(database_url)
        self.run_store = ResearchRunStore(database_url, plan_store=self.plan_store)
        self.executor = executor or ResearchExecutor()
        self.worker_id = worker_id or f"research-run:{uuid4()}"
        self.lease_seconds = lease_seconds

    def process_once(self) -> bool:
        if self._finalize_one_cancellation():
            return True
        lease = self.claim()
        if lease is None:
            return False
        try:
            plan = self.plan_store.get(lease.identity, lease.run.plan_id)
            if plan.revision != lease.run.plan_revision:
                outcome = self._partial(
                    lease, "RESEARCH_PLAN_REVISION_CONFLICT",
                    "Restore the pinned research plan revision before resuming.",
                )
            else:
                outcome = self.executor.perform(
                    lease.identity,
                    lease.run,
                    plan,
                    lease.controls,
                    lease.authorization.workspace_authorization(),
                    lambda phase: self.checkpoint(lease, phase),
                )
            self.finalize(lease, outcome)
        except ResearchRunDeadlineExceeded:
            try:
                self.finalize(
                    lease,
                    self._partial(
                        lease,
                        "RESEARCH_RUNTIME_DEADLINE_EXCEEDED",
                        "Extend the reviewed runtime budget before resuming the run.",
                    ),
                )
            except ResearchRunLeaseLost:
                pass
        except ResearchRunLeaseLost:
            return True
        except Exception as error:  # provider failures must never kill the worker loop
            LOGGER.warning(
                "Research execution failed safely; run=%s error=%s",
                lease.run.run_id,
                type(error).__name__,
            )
            try:
                self.finalize(
                    lease,
                    self._partial(
                        lease,
                        "RESEARCH_EXECUTION_FAILED",
                        "Restore research dependencies and resume the governed run.",
                    ),
                )
            except ResearchRunLeaseLost:
                pass
        return True

    def claim(self) -> ResearchRunLease | None:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT * FROM ai_research_runs
                    WHERE execution_authorization_envelope IS NOT NULL
                      AND execution_requested_at IS NOT NULL
                      AND runtime_controls_envelope IS NOT NULL
                      AND (
                           run_state = 'QUEUED'
                           OR (run_state = 'RUNNING'
                               AND lease_expires_at <= CURRENT_TIMESTAMP)
                      )
                    ORDER BY execution_requested_at, created_at, run_id
                    FOR UPDATE SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            authorization = self._authorization(row)
            identity = authorization.identity(int(row["tenant_id"]), row["user_id"])
            token = uuid4()
            generation = int(row["generation"]) + 1
            updated = connection.execute(
                """UPDATE ai_research_runs
                      SET run_state = 'RUNNING', revision = revision + 1,
                          generation = %s, lease_token = %s,
                          lease_expires_at = CURRENT_TIMESTAMP
                              + (%s * INTERVAL '1 second'),
                          lease_owner = %s,
                          execution_attempt_count = execution_attempt_count + 1,
                          checkpoint_sequence = checkpoint_sequence + 1,
                          last_checkpoint_at = CURRENT_TIMESTAMP,
                          started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                          safe_error_code = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = %s
                RETURNING *""",
                (
                    generation,
                    token,
                    self.lease_seconds,
                    self.worker_id,
                    row["run_id"],
                ),
            ).fetchone()
            command_id = self._command(updated["run_id"], generation, "claim")
            self._event(
                connection,
                identity,
                updated,
                command_id,
                "WORKER_CLAIMED",
                row["run_state"],
                {"generation": generation, "workerId": self.worker_id},
            )
            return ResearchRunLease(
                run=self.run_store._record(updated),
                identity=identity,
                authorization=authorization,
                controls=self.run_store._runtime_controls(updated),
                generation=generation,
                lease_token=token,
                lease_expires_at=updated["lease_expires_at"],
            )

    def checkpoint(
        self, lease: ResearchRunLease, phase: str
    ) -> ResearchRuntimeControls:
        if not phase or len(phase) > 64 or phase != phase.strip().upper():
            raise ValueError("Research checkpoint phase is invalid.")
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT * FROM ai_research_runs
                    WHERE run_id = %s AND tenant_id = %s AND user_id = %s
                    FOR UPDATE""",
                (lease.run.run_id, lease.identity.tenant_id, lease.identity.user_id),
            ).fetchone()
            if not self._owns(row, lease):
                raise ResearchRunLeaseLost("The research execution lease was revoked.")
            deadline = row["runtime_deadline_at"]
            if deadline is None or deadline.astimezone(UTC) <= datetime.now(UTC):
                raise ResearchRunDeadlineExceeded("The research runtime deadline elapsed.")
            updated = connection.execute(
                """UPDATE ai_research_runs
                      SET lease_expires_at = CURRENT_TIMESTAMP
                              + (%s * INTERVAL '1 second'),
                          checkpoint_sequence = checkpoint_sequence + 1,
                          last_checkpoint_at = CURRENT_TIMESTAMP,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = %s AND generation = %s AND lease_token = %s
                RETURNING *""",
                (
                    self.lease_seconds,
                    lease.run.run_id,
                    lease.generation,
                    lease.lease_token,
                ),
            ).fetchone()
            if updated is None:
                raise ResearchRunLeaseLost("The research execution lease was revoked.")
            return self.run_store._runtime_controls(updated)

    def finalize(
        self, lease: ResearchRunLease, outcome: ResearchExecutionOutcome
    ) -> str:
        if outcome.state not in {
            ResearchRunState.COMPLETED,
            ResearchRunState.PARTIAL,
            ResearchRunState.CONFLICT,
            ResearchRunState.FAILED,
        }:
            raise ValueError("The research execution outcome is not terminalizable.")
        if (outcome.state == ResearchRunState.COMPLETED) != (outcome.result is not None):
            raise ValueError("Only completed research can seal a result.")
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                "SELECT * FROM ai_research_runs WHERE run_id = %s FOR UPDATE",
                (lease.run.run_id,),
            ).fetchone()
            if not self._owns(row, lease):
                raise ResearchRunLeaseLost("The research execution lease was revoked.")
            deadline = row["runtime_deadline_at"]
            if outcome.state == ResearchRunState.COMPLETED and (
                deadline is None or deadline.astimezone(UTC) <= datetime.now(UTC)
            ):
                raise ResearchRunDeadlineExceeded(
                    "The research runtime deadline elapsed before completion."
                )
            receipt_id = uuid4() if outcome.result is not None else None
            result_envelope = (
                self.run_store._payload(
                    lease.identity,
                    lease.run.run_id,
                    "result",
                    outcome.result.model_dump(mode="json", by_alias=True),
                )
                if outcome.result is not None
                else None
            )
            terminal = outcome.state in {
                ResearchRunState.COMPLETED,
                ResearchRunState.FAILED,
            }
            updated = connection.execute(
                """UPDATE ai_research_runs
                      SET run_state = %s, revision = revision + 1,
                          progress_envelope = %s, result_envelope = %s,
                          receipt_id = %s, safe_error_code = %s,
                          lease_token = NULL, lease_expires_at = NULL,
                          lease_owner = NULL,
                          execution_authorization_envelope = CASE
                              WHEN %s THEN NULL
                              ELSE execution_authorization_envelope END,
                          completed_at = CASE WHEN %s = 'COMPLETED'
                              THEN CURRENT_TIMESTAMP ELSE NULL END,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = %s AND generation = %s AND lease_token = %s
                RETURNING *""",
                (
                    outcome.state.value,
                    self.run_store._payload(
                        lease.identity,
                        lease.run.run_id,
                        "progress",
                        outcome.progress.model_dump(mode="json", by_alias=True),
                    ),
                    result_envelope,
                    receipt_id,
                    outcome.safe_error_code,
                    terminal,
                    outcome.state.value,
                    lease.run.run_id,
                    lease.generation,
                    lease.lease_token,
                ),
            ).fetchone()
            if updated is None:
                raise ResearchRunLeaseLost("The research execution lease was revoked.")
            command_id = self._command(
                lease.run.run_id, lease.generation, outcome.state.value.lower()
            )
            self._event(
                connection,
                lease.identity,
                updated,
                command_id,
                "WORKER_FINALIZED",
                ResearchRunState.RUNNING.value,
                {
                    "generation": lease.generation,
                    "safeErrorCode": outcome.safe_error_code,
                },
            )
            return outcome.state.value

    def _finalize_one_cancellation(self) -> bool:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT * FROM ai_research_runs
                    WHERE run_state = 'CANCELLING'
                    ORDER BY updated_at, run_id
                    FOR UPDATE SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if row is None:
                return False
            identity = PersonalDomainIdentity(
                tenant_id=int(row["tenant_id"]),
                user_id=row["user_id"],
                correlation_id=f"research-cancel:{row['run_id']}",
                auth_session_id=f"worker:{row['run_id']}",
                roles=frozenset(),
                permissions=frozenset(),
            )
            updated = connection.execute(
                """UPDATE ai_research_runs
                      SET run_state = 'CANCELLED', revision = revision + 1,
                          generation = generation + 1,
                          lease_token = NULL, lease_expires_at = NULL,
                          lease_owner = NULL,
                          execution_authorization_envelope = NULL,
                          safe_error_code = 'RESEARCH_CANCELLED_BY_USER',
                          completed_at = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = %s
                RETURNING *""",
                (row["run_id"],),
            ).fetchone()
            command_id = self._command(
                row["run_id"], int(updated["generation"]), "cancelled"
            )
            self._event(
                connection,
                identity,
                updated,
                command_id,
                "WORKER_CANCELLED",
                ResearchRunState.CANCELLING.value,
                {"generation": int(updated["generation"])},
            )
            return True

    def _authorization(self, row: object) -> ResearchExecutionAuthorization:
        payload = self.run_store.codec.decrypt_json(
            row["execution_authorization_envelope"],  # type: ignore[index]
            tenant_id=row["tenant_id"],  # type: ignore[index]
            resource_type="research-run",
            resource_id=str(row["run_id"]),  # type: ignore[index]
            field="execution-authorization",
        )
        return ResearchExecutionAuthorization.model_validate(payload)

    @staticmethod
    def _owns(row: object, lease: ResearchRunLease) -> bool:
        return bool(
            row
            and row["run_state"] == ResearchRunState.RUNNING.value  # type: ignore[index]
            and int(row["generation"]) == lease.generation  # type: ignore[index]
            and row["lease_token"] == lease.lease_token  # type: ignore[index]
            and row["lease_expires_at"] is not None  # type: ignore[index]
            and row["lease_expires_at"].astimezone(UTC) > datetime.now(UTC)  # type: ignore[index]
        )

    @staticmethod
    def _command(run_id: UUID, generation: int, phase: str) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"urn:dwp:research-run-worker:{run_id}:{generation}:{phase}",
        )

    def _event(
        self,
        connection: object,
        identity: PersonalDomainIdentity,
        row: object,
        command_id: UUID,
        event_type: str,
        previous: str,
        detail: dict[str, object],
    ) -> None:
        fingerprint = self.run_store.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="research-run:worker-event",
            payload={
                "runId": str(row["run_id"]),  # type: ignore[index]
                "commandId": str(command_id),
                "eventType": event_type,
                "detail": detail,
            },
        )
        self.run_store._event(
            connection,
            identity,
            row,
            command_id,
            event_type,
            previous,
            fingerprint,
            detail,
        )

    @staticmethod
    def _partial(
        lease: ResearchRunLease, code: str, recovery_hint: str
    ) -> ResearchExecutionOutcome:
        return ResearchExecutionOutcome(
            state=ResearchRunState.PARTIAL,
            progress=lease.run.progress.model_copy(
                update={"recovery_hint": recovery_hint}
            ),
            safe_error_code=code,
        )
