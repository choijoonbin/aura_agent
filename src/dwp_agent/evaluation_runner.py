from __future__ import annotations

from time import monotonic
from uuid import UUID

from .ask_runtime import AskRuntime
from .contracts import AskRequest, AskState
from .conversation_store import InMemoryConversationStore
from .evaluation_store import PostgresEvaluationStore
from .governance_contracts import (
    EvaluationOutcome,
    EvaluationResult,
    EvaluationRun,
    EvaluationSetDetail,
)
from .governance_store import GovernanceStoreUnavailable
from .policy import AskIdentity
from .run_store import InMemoryRunStore
from .runtime_policy import (
    SourcePolicyBlocked,
    SourceScopeLimitExceeded,
    resolve_runtime_safety_controls,
)


def run_evaluation(
    *, store: PostgresEvaluationStore, tenant_id: str, actor_user_id: str,
    correlation_id: str, evaluation_set_id: UUID, identity: AskIdentity,
) -> EvaluationRun:
    run_id, evaluation_set = store.begin_run(
        tenant_id=tenant_id, actor_user_id=actor_user_id,
        correlation_id=correlation_id, evaluation_set_id=evaluation_set_id)
    try:
        results, model_ref = _evaluate_cases(
            run_id=run_id,
            evaluation_set=evaluation_set,
            identity=identity,
        )
        return store.complete_run(
            tenant_id=tenant_id, actor_user_id=actor_user_id,
            correlation_id=correlation_id, evaluation_set_id=evaluation_set_id,
            evaluation_run_id=run_id, results=results, model_ref=model_ref)
    except Exception:
        store.fail_run(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
            evaluation_run_id=run_id,
        )
        raise


def _evaluate_cases(
    *, run_id: UUID, evaluation_set: EvaluationSetDetail, identity: AskIdentity
) -> tuple[list[EvaluationResult], str | None]:
    runtime = AskRuntime(
        run_store=InMemoryRunStore(),
        conversation_store=InMemoryConversationStore(),
    )
    results: list[EvaluationResult] = []
    model_ref: str | None = None
    for case in evaluation_set.cases:
        started = monotonic()
        request = AskRequest(
            request_id=f"eval-{run_id}-{case.evaluation_case_id}",
            query=case.prompt,
            locale=evaluation_set.summary.locale,
            source_scopes=case.source_scopes,
        )
        try:
            safety_controls = resolve_runtime_safety_controls(request, identity)
        except (GovernanceStoreUnavailable, SourcePolicyBlocked, SourceScopeLimitExceeded) as error:
            results.append(EvaluationResult(
                evaluation_case_id=case.evaluation_case_id,
                case_name=case.name,
                outcome=EvaluationOutcome.CONFIGURATION_REQUIRED,
                status_code=type(error).__name__.upper(),
                grounded=False,
                expected_terms_matched=0,
                expected_terms_total=len(case.expected_terms),
                latency_ms=max(0, round((monotonic() - started) * 1000)),
            ))
            continue
        response = runtime.answer(
            request,
            identity=identity,
            safety_controls=safety_controls,
        )
        elapsed_ms = max(0, round((monotonic() - started) * 1000))
        answer = (response.answer or "").casefold()
        matched = sum(term.casefold() in answer for term in case.expected_terms)
        grounded = response.state == AskState.COMPLETED and bool(response.citations)
        if response.state == AskState.CONFIGURATION_REQUIRED:
            outcome = EvaluationOutcome.CONFIGURATION_REQUIRED
        elif grounded and matched == len(case.expected_terms):
            outcome = EvaluationOutcome.PASS
        else:
            outcome = EvaluationOutcome.FAIL
        route = response.model_route
        if route.provider or route.model:
            model_ref = "/".join(value for value in (route.provider, route.model) if value)
        results.append(EvaluationResult(
            evaluation_case_id=case.evaluation_case_id,
            case_name=case.name,
            outcome=outcome,
            status_code=response.status_code,
            grounded=grounded,
            expected_terms_matched=matched,
            expected_terms_total=len(case.expected_terms),
            latency_ms=route.latency_ms or elapsed_ms,
        ))
    return results, model_ref
