from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from psycopg import connect
from psycopg.errors import UniqueViolation

from .crypto import PayloadCipherKeyring, load_payload_keyring
from .evaluation_evidence_store import EvaluationEvidenceStoreMixin, EvaluationRunNotFound
from .governance_contracts import (
    CreateEvaluationCaseRequest,
    CreateEvaluationSetRequest,
    EvaluationCase,
    EvaluationLifecycle,
    EvaluationOutcome,
    EvaluationResult,
    EvaluationRun,
    EvaluationRunState,
    EvaluationSetDetail,
    EvaluationSetSummary,
    UpdateEvaluationLifecycleRequest,
)
from .governance_store import GovernancePolicyConflict, GovernanceStoreUnavailable


class EvaluationSetNotFound(RuntimeError):
    pass


class EvaluationSetNotRunnable(RuntimeError):
    pass


class EvaluationRunAlreadyActive(RuntimeError):
    pass


class EvaluationRunLeaseLost(RuntimeError):
    pass


MAX_EVALUATION_CASES = 20
EVALUATION_RUN_LEASE_MINUTES = 30


class PostgresEvaluationStore(EvaluationEvidenceStoreMixin):
    def __init__(self, database_url: str, keyring: PayloadCipherKeyring) -> None:
        self.database_url = database_url
        self.keyring = keyring

    def list_sets(self, *, tenant_id: str) -> list[EvaluationSetSummary]:
        with connect(self.database_url) as connection:
            rows = connection.execute(
                """SELECT evaluation_set_id, name, description, locale, lifecycle_state,
                          (SELECT COUNT(*) FROM ai_evaluation_cases c
                            WHERE c.evaluation_set_id = s.evaluation_set_id) AS case_count,
                          latest.run_state,
                          CASE WHEN latest.case_count > 0
                               THEN ROUND(latest.passed_count * 100.0 / latest.case_count)::INTEGER
                               ELSE NULL END AS pass_rate,
                          version, updated_at
                     FROM ai_evaluation_sets s
                     LEFT JOIN LATERAL (
                         SELECT run_state, case_count, passed_count
                           FROM ai_evaluation_runs r
                          WHERE r.tenant_id = s.tenant_id
                            AND r.evaluation_set_id = s.evaluation_set_id
                          ORDER BY r.created_at DESC LIMIT 1
                     ) latest ON TRUE
                    WHERE tenant_id = %s
                    ORDER BY updated_at DESC, evaluation_set_id DESC""",
                (int(tenant_id),),
            ).fetchall()
        return [self._summary(row) for row in rows]

    def detail(self, *, tenant_id: str, evaluation_set_id: UUID) -> EvaluationSetDetail:
        tenant = int(tenant_id)
        summaries = {
            item.evaluation_set_id: item for item in self.list_sets(tenant_id=tenant_id)
        }
        summary = summaries.get(evaluation_set_id)
        if summary is None:
            raise EvaluationSetNotFound("The evaluation set was not found.")
        with connect(self.database_url) as connection:
            rows = connection.execute(
                """SELECT evaluation_case_id, evaluation_set_id, name,
                          prompt_nonce, prompt_ciphertext,
                          expected_terms_nonce, expected_terms_ciphertext,
                          encryption_key_version, source_scopes, version, created_at
                     FROM ai_evaluation_cases
                    WHERE tenant_id = %s AND evaluation_set_id = %s
                    ORDER BY created_at, evaluation_case_id""",
                (tenant, evaluation_set_id),
            ).fetchall()
        return EvaluationSetDetail(
            summary=summary,
            cases=[self._case(tenant_id, row) for row in rows],
        )

    def create_set(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        request: CreateEvaluationSetRequest,
    ) -> EvaluationSetDetail:
        tenant = int(tenant_id)
        set_id = uuid4()
        with connect(self.database_url) as connection:
            connection.execute(
                """INSERT INTO ai_evaluation_sets (
                       evaluation_set_id, tenant_id, name, description, locale,
                       created_by, updated_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (set_id, tenant, request.name.strip(),
                 request.description.strip() if request.description else None,
                 request.locale, actor_user_id, actor_user_id),
            )
            self._event(connection, tenant, "evaluation-set.created", "EVALUATION_SET",
                        str(set_id), actor_user_id, correlation_id, None)
        return self.detail(tenant_id=tenant_id, evaluation_set_id=set_id)

    def add_case(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        evaluation_set_id: UUID, request: CreateEvaluationCaseRequest,
    ) -> EvaluationSetDetail:
        tenant = int(tenant_id)
        case_id = uuid4()
        with connect(self.database_url) as connection:
            state = connection.execute(
                """SELECT lifecycle_state,
                          (SELECT COUNT(*) FROM ai_evaluation_cases c
                            WHERE c.tenant_id = s.tenant_id
                              AND c.evaluation_set_id = s.evaluation_set_id)
                     FROM ai_evaluation_sets s
                    WHERE tenant_id = %s AND evaluation_set_id = %s FOR UPDATE""",
                (tenant, evaluation_set_id),
            ).fetchone()
            if state is None:
                raise EvaluationSetNotFound("The evaluation set was not found.")
            if state[0] != EvaluationLifecycle.DRAFT.value:
                raise EvaluationSetNotRunnable("Only draft evaluation sets can be edited.")
            if state[1] >= MAX_EVALUATION_CASES:
                raise EvaluationSetNotRunnable(
                    f"An evaluation set supports at most {MAX_EVALUATION_CASES} cases."
                )
            aad = self._aad(tenant_id, evaluation_set_id, case_id)
            key_version, prompt_nonce, prompt_ciphertext = self.keyring.encrypt_bytes(
                request.prompt.encode("utf-8"), aad + b":prompt")
            expected_version, expected_nonce, expected_ciphertext = self.keyring.encrypt_bytes(
                json.dumps(request.expected_terms, ensure_ascii=False).encode("utf-8"),
                aad + b":expected")
            if key_version != expected_version:
                raise GovernanceStoreUnavailable(
                    "Evaluation encryption key changed during the request."
                )
            connection.execute(
                """INSERT INTO ai_evaluation_cases (
                       evaluation_case_id, tenant_id, evaluation_set_id, name,
                       prompt_nonce, prompt_ciphertext,
                       expected_terms_nonce, expected_terms_ciphertext,
                       encryption_key_version, source_scopes, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)""",
                (case_id, tenant, evaluation_set_id, request.name, prompt_nonce,
                 prompt_ciphertext, expected_nonce, expected_ciphertext, key_version,
                 json.dumps([scope.value for scope in request.source_scopes]), actor_user_id),
            )
            connection.execute(
                """UPDATE ai_evaluation_sets
                      SET version = version + 1, updated_by = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND evaluation_set_id = %s""",
                (actor_user_id, tenant, evaluation_set_id),
            )
            self._event(connection, tenant, "evaluation-case.created", "EVALUATION_CASE",
                        str(case_id), actor_user_id, correlation_id, None)
        return self.detail(tenant_id=tenant_id, evaluation_set_id=evaluation_set_id)

    def transition(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        evaluation_set_id: UUID, request: UpdateEvaluationLifecycleRequest,
    ) -> EvaluationSetDetail:
        tenant = int(tenant_id)
        with connect(self.database_url) as connection:
            current = connection.execute(
                """SELECT lifecycle_state, version FROM ai_evaluation_sets
                    WHERE tenant_id = %s AND evaluation_set_id = %s FOR UPDATE""",
                (tenant, evaluation_set_id),
            ).fetchone()
            if current is None:
                raise EvaluationSetNotFound("The evaluation set was not found.")
            if current[1] != request.expected_version:
                raise GovernancePolicyConflict("The evaluation set changed. Reload and retry.")
            if current[0] == EvaluationLifecycle.RETIRED.value:
                raise EvaluationSetNotRunnable("Retired evaluation sets cannot transition.")
            if current[0] == request.lifecycle_state.value:
                raise EvaluationSetNotRunnable(
                    "The evaluation set is already in the requested lifecycle state."
                )
            if request.lifecycle_state == EvaluationLifecycle.ACTIVE:
                count = connection.execute(
                    "SELECT COUNT(*) FROM ai_evaluation_cases WHERE evaluation_set_id = %s",
                    (evaluation_set_id,),
                ).fetchone()[0]
                if count == 0:
                    raise EvaluationSetNotRunnable("Add at least one case before activation.")
            connection.execute(
                """UPDATE ai_evaluation_sets
                      SET lifecycle_state = %s, version = version + 1,
                          updated_by = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND evaluation_set_id = %s AND version = %s""",
                (request.lifecycle_state.value, actor_user_id, tenant,
                 evaluation_set_id, request.expected_version),
            )
            self._event(connection, tenant, "evaluation-set.lifecycle-changed", "EVALUATION_SET",
                        str(evaluation_set_id), actor_user_id, correlation_id,
                        request.change_reason)
        return self.detail(tenant_id=tenant_id, evaluation_set_id=evaluation_set_id)

    def begin_run(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        evaluation_set_id: UUID,
    ) -> tuple[UUID, EvaluationSetDetail]:
        detail = self.detail(tenant_id=tenant_id, evaluation_set_id=evaluation_set_id)
        if detail.summary.lifecycle_state != EvaluationLifecycle.ACTIVE:
            raise EvaluationSetNotRunnable("Only active evaluation sets can run.")
        if not detail.cases:
            raise EvaluationSetNotRunnable("The evaluation set has no cases.")
        run_id = uuid4()
        try:
            with connect(self.database_url) as connection:
                connection.execute(
                    """UPDATE ai_evaluation_runs
                          SET run_state = 'FAILED', completed_at = CURRENT_TIMESTAMP,
                              lease_expires_at = NULL
                        WHERE tenant_id = %s AND evaluation_set_id = %s
                          AND run_state = 'RUNNING'
                          AND lease_expires_at <= CURRENT_TIMESTAMP""",
                    (int(tenant_id), evaluation_set_id),
                )
                connection.execute(
                    """INSERT INTO ai_evaluation_runs (
                           evaluation_run_id, tenant_id, evaluation_set_id, run_state,
                           case_count, created_by, lease_expires_at)
                       VALUES (%s, %s, %s, 'RUNNING', %s, %s, %s)""",
                    (
                        run_id,
                        int(tenant_id),
                        evaluation_set_id,
                        len(detail.cases),
                        actor_user_id,
                        datetime.now(timezone.utc) + timedelta(
                            minutes=EVALUATION_RUN_LEASE_MINUTES),
                    ),
                )
                self._event(
                    connection, int(tenant_id), "evaluation-run.started", "EVALUATION_RUN",
                    str(run_id), actor_user_id, correlation_id, None)
        except UniqueViolation as error:
            raise EvaluationRunAlreadyActive(
                "An evaluation run is already active for this test set."
            ) from error
        return run_id, detail

    def complete_run(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        evaluation_set_id: UUID, evaluation_run_id: UUID,
        results: list[EvaluationResult], model_ref: str | None,
    ) -> EvaluationRun:
        passed = sum(item.outcome == EvaluationOutcome.PASS for item in results)
        configured = sum(
            item.outcome == EvaluationOutcome.CONFIGURATION_REQUIRED for item in results)
        failed = len(results) - passed - configured
        state = (EvaluationRunState.CONFIGURATION_REQUIRED
                 if configured == len(results) else EvaluationRunState.COMPLETED)
        with connect(self.database_url) as connection:
            for result in results:
                connection.execute(
                    """INSERT INTO ai_evaluation_results (
                           evaluation_result_id, tenant_id, evaluation_run_id,
                           evaluation_case_id, outcome, status_code, grounded,
                           expected_terms_matched, expected_terms_total, latency_ms)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (uuid4(), int(tenant_id), evaluation_run_id,
                     result.evaluation_case_id, result.outcome.value, result.status_code,
                     result.grounded, result.expected_terms_matched,
                     result.expected_terms_total, result.latency_ms),
                )
            row = connection.execute(
                """UPDATE ai_evaluation_runs
                      SET run_state = %s, passed_count = %s, failed_count = %s,
                          configuration_required_count = %s, model_ref = %s,
                          completed_at = CURRENT_TIMESTAMP, lease_expires_at = NULL
                    WHERE tenant_id = %s AND evaluation_run_id = %s
                      AND run_state = 'RUNNING'
                      AND lease_expires_at > CURRENT_TIMESTAMP
                RETURNING created_at, completed_at""",
                (state.value, passed, failed, configured, model_ref,
                 int(tenant_id), evaluation_run_id),
            ).fetchone()
            if row is None:
                raise EvaluationRunLeaseLost(
                    "The evaluation run lease expired or was already recovered."
                )
            self._event(connection, int(tenant_id), "evaluation-run.completed", "EVALUATION_RUN",
                        str(evaluation_run_id), actor_user_id, correlation_id, None)
        return EvaluationRun(
            evaluation_run_id=evaluation_run_id, evaluation_set_id=evaluation_set_id,
            run_state=state, case_count=len(results), passed_count=passed,
            failed_count=failed, configuration_required_count=configured,
            model_ref=model_ref, results=results, created_at=row[0], completed_at=row[1])

    def fail_run(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str,
        evaluation_run_id: UUID,
    ) -> None:
        with connect(self.database_url) as connection:
            connection.execute(
                """UPDATE ai_evaluation_runs
                      SET run_state = 'FAILED', completed_at = CURRENT_TIMESTAMP,
                          lease_expires_at = NULL
                    WHERE tenant_id = %s AND evaluation_run_id = %s
                      AND run_state = 'RUNNING'""",
                (int(tenant_id), evaluation_run_id),
            )
            self._event(
                connection,
                int(tenant_id),
                "evaluation-run.failed",
                "EVALUATION_RUN",
                str(evaluation_run_id),
                actor_user_id,
                correlation_id,
                None,
            )

    def _summary(self, row) -> EvaluationSetSummary:
        return EvaluationSetSummary(
            evaluation_set_id=row[0], name=row[1], description=row[2], locale=row[3],
            lifecycle_state=row[4], case_count=row[5], latest_run_state=row[6],
            latest_pass_rate=row[7], version=row[8], updated_at=row[9])

    def _case(self, tenant_id: str, row) -> EvaluationCase:
        aad = self._aad(tenant_id, row[1], row[0])
        prompt = self.keyring.decrypt_bytes(
            row[7], bytes(row[3]), bytes(row[4]), aad + b":prompt").decode("utf-8")
        expected = json.loads(self.keyring.decrypt_bytes(
            row[7], bytes(row[5]), bytes(row[6]), aad + b":expected").decode("utf-8"))
        return EvaluationCase(
            evaluation_case_id=row[0], evaluation_set_id=row[1], name=row[2], prompt=prompt,
            expected_terms=expected, source_scopes=row[8], version=row[9], created_at=row[10])

    def _event(self, connection, tenant: int, event_type: str, target_type: str,
               target_key: str, actor: str, correlation: str, reason: str | None) -> None:
        connection.execute(
            """INSERT INTO ai_governance_events (
                   event_id, tenant_id, category, event_type, target_type, target_key,
                   actor_user_id, correlation_id, change_reason)
               VALUES (%s, %s, 'EVALUATION', %s, %s, %s, %s, %s, %s)""",
            (uuid4(), tenant, event_type, target_type, target_key, actor, correlation, reason))

    def _aad(self, tenant_id: str, set_id: UUID, case_id: UUID) -> bytes:
        return f"dwaion:evaluation:{tenant_id}:{set_id}:{case_id}".encode("utf-8")


_STORE: PostgresEvaluationStore | None = None
_STORE_LOCK = threading.Lock()


def get_evaluation_store() -> PostgresEvaluationStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            raise GovernanceStoreUnavailable(
                "DWAI-ON evaluation requires the configured Agent database.")
        _STORE = PostgresEvaluationStore(database_url, load_payload_keyring())
        return _STORE


def reset_evaluation_store_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None
