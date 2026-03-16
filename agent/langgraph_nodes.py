from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

from langgraph.types import interrupt

from agent.event_schema import AgentEvent
from agent.hitl import build_hitl_request
import agent.langgraph_runtime as _runtime_module
from agent.langgraph_domain import (
    _build_prescreened_result as _domain_build_prescreened_result,
    _find_tool_result as _domain_find_tool_result,
    _format_occurred_at as _domain_format_occurred_at,
    _is_valid_screening_case_type as _domain_is_valid_screening_case_type,
    _tool_result_key as _domain_tool_result_key,
    _top_policy_refs as _domain_top_policy_refs,
    _voucher_summary_for_context as _domain_voucher_summary_for_context,
)
from agent.langgraph_nodes_execute import execute_node_impl as _execute_execute_node_impl
from agent.langgraph_nodes_ingest import (
    available_planner_tools_impl as _ingest_available_planner_tools_impl,
    intake_node_impl as _ingest_intake_node_impl,
    invoke_llm_planner_impl as _ingest_invoke_llm_planner_impl,
    planner_node_impl as _ingest_planner_node_impl,
    screener_node_impl as _ingest_screener_node_impl,
    start_router_node_impl as _ingest_start_router_node_impl,
)
from agent.langgraph_nodes_outcome import (
    finalizer_node_impl as _outcome_finalizer_node_impl,
    hitl_pause_node_impl as _outcome_hitl_pause_node_impl,
    hitl_validate_node_impl as _outcome_hitl_validate_node_impl,
    reporter_node_impl as _outcome_reporter_node_impl,
)
from agent.langgraph_nodes_review import (
    critic_node_impl as _review_critic_node_impl,
    verify_node_impl as _review_verify_node_impl,
)
from agent.langgraph_decisions import (
    _build_grounded_reason as _decisions_build_grounded_reason,
    _llm_decide_hitl_verdict as _decisions_llm_decide_hitl_verdict,
    _llm_summarize_completed_reason as _decisions_llm_summarize_completed_reason,
    _llm_summarize_hold_reason as _decisions_llm_summarize_hold_reason,
    _select_policy_refs_by_relevance as _decisions_select_policy_refs_by_relevance,
)
from agent.langgraph_hitl_helpers import (
    _assess_hitl_resolution_requirements as _hitl_assess_resolution_requirements,
    _build_system_auto_finalize_blockers as _hitl_build_system_auto_finalize_blockers,
    _format_hitl_reason_for_stream as _hitl_format_reason_for_stream,
    _is_verify_ready_without_hitl as _hitl_is_verify_ready_without_hitl,
    _pick_llm_review_reason as _hitl_pick_llm_review_reason,
)
from agent.langgraph_routes import (
    _get_hitl_response_value as _routes_get_hitl_response_value,
    _route_after_critic as _routes_after_critic,
    _route_after_hitl_validate as _routes_after_hitl_validate,
    _route_after_verify as _routes_after_verify,
)
from agent.langgraph_runtime import (
    _closure_initial_state as _runtime_closure_initial_state,
    _closure_verification as _runtime_closure_verification,
    _get_checkpointer as _runtime_get_checkpointer,
    build_agent_graph as _runtime_build_agent_graph,
    build_hitl_closure_graph as _runtime_build_hitl_closure_graph,
)
from agent.langgraph_reasoning import (
    ConsistencyCheckResult,
    _compact_reasoning_for_stream as _reasoning_compact_reasoning_for_stream,
    _extract_reasoning_token as _reasoning_extract_reasoning_token,
    _reasoning_stream_events as _reasoning_module_stream_events,
    _stream_reasoning_events_with_llm as _reasoning_stream_reasoning_events_with_llm,
    call_node_llm_with_consistency_check as _reasoning_call_node_llm_with_consistency_check,
    check_reasoning_consistency as _reasoning_check_reasoning_consistency,
)
from agent.langgraph_scoring import (
    _compute_plan_achievement as _scoring_compute_plan_achievement,
    _derive_flags as _scoring_derive_flags,
    _lookup_tiered as _scoring_lookup_tiered,
    _plan_from_flags as _scoring_plan_from_flags,
    _reason_prefix as _scoring_reason_prefix,
    _score as _scoring_score,
    _score_hybrid as _scoring_score_hybrid,
    _score_to_severity as _scoring_score_to_severity,
    _score_with_hitl_adjustment as _scoring_score_with_hitl_adjustment,
)
from agent.langgraph_verification_logic import (
    _build_verification_targets as _verification_build_targets,
    _derive_hitl_from_regulation as _verification_derive_hitl_from_regulation,
    _generate_claim_display_texts as _verification_generate_claim_display_texts,
    _generate_hitl_review_content as _verification_generate_hitl_review_content,
    _retry_fill_hitl_review_when_empty as _verification_retry_fill_hitl_review_when_empty,
)
from agent.output_models import (
    Citation,
    ClaimVerificationResult,
    CriticOutput,
    ExecuteOutput,
    PlanStep,
    PlannerOutput,
    ReporterOutput,
    ReporterSentence,
    VerifierGate,
    VerifierOutput,
)
from agent.screener import run_screening
from agent.agent_tools import get_langchain_tools
from agent.tool_schemas import ToolContextInput
from utils.config import settings
from utils.llm_azure import completion_kwargs_for_azure

# Phase C: plan 기반 도구 실행은 LangChain tool 호출로만 수행 (registry direct dispatch 제거)
_TOOLS_BY_NAME: dict[str, Any] = {}
# prior_tool_results를 활용하는 도구는 병렬 그룹 이후 순차 실행
_SEQUENTIAL_LAST_TOOLS = frozenset({"policy_rulebook_probe", "legacy_aura_deep_audit"})
# 병렬 실행 시 의존성이 있는 도구 정의 (의존 도구가 실행/스킵 처리된 뒤 실행)
_PARALLEL_TOOL_DEPENDENCIES: dict[str, frozenset[str]] = {
    "merchant_risk_probe": frozenset({"holiday_compliance_probe"}),
}
# 스트림/타임라인 표시용 짧은 문구 (반복적인 "— 도구 실행" 대신)
_TOOL_CALL_SHORT_MESSAGE: dict[str, str] = {
    "holiday_compliance_probe": "휴일·근태 적격성 확인 중",
    "merchant_risk_probe": "가맹점 업종 위험 점검 중",
    "policy_rulebook_probe": "규정 조항 조회 중",
    "document_evidence_probe": "전표·증빙 수집 중",
    "budget_risk_probe": "예산 초과 점검 중",
    "legacy_aura_deep_audit": "심층 감사 실행 중",
}
_MAX_CRITIC_LOOP = 2
_CHECKPOINTER: Any | None = None
_COMPILED_GRAPH: Any | None = None
_CLOSURE_GRAPH: Any | None = None
_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")


def _compact_reasoning_for_stream(text: str) -> str:
    return _reasoning_compact_reasoning_for_stream(text)


def _is_valid_screening_case_type(value: Any) -> bool:
    return _domain_is_valid_screening_case_type(value)


def check_reasoning_consistency(node_name: str, output: Any) -> ConsistencyCheckResult:
    return _reasoning_check_reasoning_consistency(node_name, output)


def call_node_llm_with_consistency_check(
    node_name: str,
    output: Any,
    reasoning_text: str,
    *,
    max_retries: int = 1,
) -> tuple[str, ConsistencyCheckResult, bool]:
    return _reasoning_call_node_llm_with_consistency_check(
        node_name,
        output,
        reasoning_text,
        max_retries=max_retries,
    )


def _get_tools_by_name() -> dict[str, Any]:
    if not _TOOLS_BY_NAME:
        for t in get_langchain_tools():
            _TOOLS_BY_NAME[t.name] = t
    return _TOOLS_BY_NAME


class AgentState(TypedDict, total=False):
    case_id: str
    body_evidence: dict[str, Any]
    intended_risk_type: str | None
    # Screening result populated by screener_node (before intake)
    screening_result: dict[str, Any] | None
    flags: dict[str, Any]
    plan: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    score_breakdown: dict[str, Any]
    critique: dict[str, Any]
    verification: dict[str, Any]
    hitl_request: dict[str, Any] | None
    final_result: dict[str, Any]
    pending_events: list[dict[str, Any]]
    # Phase B: structured output (planner/critic/verifier/reporter)
    planner_output: dict[str, Any]
    execute_output: dict[str, Any]
    critic_output: dict[str, Any]
    verifier_output: dict[str, Any]
    reporter_output: dict[str, Any]
    review_audit: dict[str, Any]
    critic_loop_count: int
    replan_context: dict[str, Any] | None
    plan_achievement: dict[str, Any]
    # Hybrid scoring / verification fields
    rule_score: int
    llm_score: int
    final_score: int
    verification_gate: str
    summary_reason: str
    diagnostic_log: str
    fidelity: int
    rule_fidelity: int
    llm_fidelity: int
    fallback_used: bool
    fallback_reason: str
    retry_count: int
    max_retries: int
    latency_ms: dict[str, float]
    version_meta: dict[str, str]
    evaluation_history: list[dict[str, Any]]
    # workspace.md: 이전 노드 결과 1줄 요약 (generate_working_note prev_result_summary용)
    last_node_summary: str
    # Sprint 2: 증빙 이미지 추출 엔티티 (intake에서 body_evidence.extracted_entities를 읽어 세팅)
    visual_audit_results: list[dict[str, Any]]


def _format_occurred_at(value: Any) -> str:
    return _domain_format_occurred_at(value)


def _reasoning_stream_events(node_name: str, reasoning_text: str) -> list[dict[str, Any]]:
    return _reasoning_module_stream_events(node_name, reasoning_text)


def _extract_reasoning_token(delta: str, full_so_far: str, emitted_len: int) -> tuple[str, int, bool]:
    return _reasoning_extract_reasoning_token(delta, full_so_far, emitted_len)


async def _stream_reasoning_events_with_llm(
    node_name: str,
    reasoning_text: str,
    *,
    context: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]], str]:
    return await _reasoning_stream_reasoning_events_with_llm(
        node_name,
        reasoning_text,
        context=context,
    )


def _voucher_summary_for_context(body_evidence: dict[str, Any]) -> str:
    return _domain_voucher_summary_for_context(body_evidence)


def _tool_result_key(r: dict[str, Any]) -> str:
    return _domain_tool_result_key(r)


def _find_tool_result(tool_results: list[dict[str, Any]], tool_name: str) -> dict[str, Any] | None:
    return _domain_find_tool_result(tool_results, tool_name)


def _top_policy_refs(tool_results: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    return _domain_top_policy_refs(tool_results, limit=limit)


async def _select_policy_refs_by_relevance(
    state: AgentState,
    refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return await _decisions_select_policy_refs_by_relevance(state, refs)


def _should_skip_tool(step: dict[str, Any], *, state: AgentState, tool_results: list[dict[str, Any]]) -> tuple[bool, str | None]:
    tool = step["tool"]
    if tool != "legacy_aura_deep_audit":
        return False, None

    policy_ref_count = (_find_tool_result(tool_results, "policy_rulebook_probe") or {}).get("facts", {}).get("ref_count", 0)
    line_item_count = (_find_tool_result(tool_results, "document_evidence_probe") or {}).get("facts", {}).get("lineItemCount", 0)
    has_missing_fields = bool(((state["body_evidence"].get("dataQuality") or {}).get("missingFields") or []))
    budget_exceeded = bool(state.get("flags", {}).get("budgetExceeded"))

    # 규정 근거와 전표 증거가 충분하고 입력 누락이 없으면 legacy specialist 호출을 생략한다.
    if policy_ref_count >= 2 and line_item_count > 0 and not has_missing_fields and not budget_exceeded:
        return True, "규정 근거와 전표 증거가 충분해 추가 legacy 심층 분석 없이 진행합니다."
    return False, None


def _score_with_hitl_adjustment(score: dict[str, Any], flags: dict[str, Any]) -> dict[str, Any]:
    return _scoring_score_with_hitl_adjustment(score, flags)


def _build_system_auto_finalize_blockers(
    verification_summary: dict[str, Any],
    *,
    quality_signals: list[str] | None = None,
    fallback_reason: str = "",
) -> list[str]:
    return _hitl_build_system_auto_finalize_blockers(
        verification_summary,
        quality_signals=quality_signals,
        fallback_reason=fallback_reason,
    )


def _assess_hitl_resolution_requirements(
    *,
    hitl_request: dict[str, Any],
    hitl_response: dict[str, Any],
    evidence_result: dict[str, Any] | None,
) -> tuple[bool, list[str], list[str]]:
    return _hitl_assess_resolution_requirements(
        hitl_request=hitl_request,
        hitl_response=hitl_response,
        evidence_result=evidence_result,
    )


async def _llm_decide_hitl_verdict(
    *,
    hitl_request: dict[str, Any],
    hitl_response: dict[str, Any],
    evidence_result: dict[str, Any] | None,
) -> tuple[str, str]:
    return await _decisions_llm_decide_hitl_verdict(
        hitl_request=hitl_request,
        hitl_response=hitl_response,
        evidence_result=evidence_result,
    )


async def _llm_summarize_hold_reason(hitl_response: dict[str, Any]) -> str:
    return await _decisions_llm_summarize_hold_reason(hitl_response)


async def _llm_summarize_completed_reason(state: AgentState) -> str:
    return await _decisions_llm_summarize_completed_reason(state)


def _pick_llm_review_reason(hitl_request: dict[str, Any]) -> str:
    return _hitl_pick_llm_review_reason(hitl_request)


def _is_verify_ready_without_hitl(state: AgentState) -> bool:
    return _hitl_is_verify_ready_without_hitl(state)


def _build_grounded_reason(state: AgentState, completed_tail: str | None = None) -> tuple[str, str]:
    return _decisions_build_grounded_reason(state, completed_tail=completed_tail)


def _derive_flags(body_evidence: dict[str, Any]) -> dict[str, Any]:
    return _scoring_derive_flags(body_evidence)


def _plan_from_flags(flags: dict[str, Any]) -> list[dict[str, Any]]:
    return _scoring_plan_from_flags(flags)


def _lookup_tiered(value: int, table: list[tuple[int, float]]) -> float:
    return _scoring_lookup_tiered(value, table)


def _score_to_severity(final_score: float) -> str:
    return _scoring_score_to_severity(final_score)


def _compute_plan_achievement(
    plan: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> dict[str, Any]:
    return _scoring_compute_plan_achievement(plan, tool_results)


def _reason_prefix(points: float) -> str:
    return _scoring_reason_prefix(points)


def _score(flags: dict[str, Any], tool_results: list[dict[str, Any]]) -> dict[str, Any]:
    return _scoring_score(flags, tool_results)


async def _score_hybrid(state: AgentState, flags: dict[str, Any], tool_results: list[dict[str, Any]]) -> dict[str, Any]:
    return await _scoring_score_hybrid(state, flags, tool_results)


def _build_prescreened_result(body: dict[str, Any]) -> dict[str, Any]:
    return _domain_build_prescreened_result(body)


async def start_router_node(state: AgentState) -> AgentState:
    return await _ingest_start_router_node_impl(
        state,
        is_valid_screening_case_type=_is_valid_screening_case_type,
        build_prescreened_result=_build_prescreened_result,
    )


async def screener_node(state: AgentState) -> AgentState:
    return await _ingest_screener_node_impl(
        state,
        is_valid_screening_case_type=_is_valid_screening_case_type,
        run_screening=run_screening,
    )


async def intake_node(state: AgentState) -> AgentState:
    return await _ingest_intake_node_impl(
        state,
        derive_flags=_derive_flags,
        stream_reasoning_events_with_llm=_stream_reasoning_events_with_llm,
    )


def _available_planner_tools() -> list[dict[str, str]]:
    return _ingest_available_planner_tools_impl(get_tools_by_name=_get_tools_by_name)


async def _invoke_llm_planner(
    flags: dict[str, Any],
    screening: dict[str, Any],
    replan_context: dict[str, Any] | None,
    available_tools: list[dict[str, str]],
) -> list[dict[str, Any]]:
    return await _ingest_invoke_llm_planner_impl(flags, screening, replan_context, available_tools)


async def planner_node(state: AgentState) -> AgentState:
    return await _ingest_planner_node_impl(
        state,
        plan_from_flags=_plan_from_flags,
        available_planner_tools=_available_planner_tools,
        invoke_llm_planner=_invoke_llm_planner,
        stream_reasoning_events_with_llm=_stream_reasoning_events_with_llm,
    )


async def execute_node(state: AgentState) -> AgentState:
    return await _execute_execute_node_impl(
        state,
        get_tools_by_name=_get_tools_by_name,
        should_skip_tool=lambda step, tool_results: _should_skip_tool(step, state=state, tool_results=tool_results),
        score=_score,
        score_hybrid=_score_hybrid,
        tool_result_key=_tool_result_key,
        compute_plan_achievement=_compute_plan_achievement,
        stream_reasoning_events_with_llm=_stream_reasoning_events_with_llm,
    )


def _build_verification_targets(state: AgentState) -> list[str]:
    return _verification_build_targets(state)


async def critic_node(state: AgentState) -> AgentState:
    return await _review_critic_node_impl(
        state,
        max_critic_loop=_MAX_CRITIC_LOOP,
        tool_result_key=_tool_result_key,
        build_verification_targets=_build_verification_targets,
        call_node_llm_with_consistency_check=call_node_llm_with_consistency_check,
        stream_reasoning_events_with_llm=_stream_reasoning_events_with_llm,
    )


async def _derive_hitl_from_regulation(state: AgentState) -> dict[str, Any]:
    return await _verification_derive_hitl_from_regulation(state)


async def _generate_hitl_review_content(
    hitl_request: dict[str, Any],
    verification_summary: dict[str, Any],
    claim_results: list[dict[str, Any]],
    reasoning_text: str,
) -> dict[str, Any]:
    return await _verification_generate_hitl_review_content(
        hitl_request,
        verification_summary,
        claim_results,
        reasoning_text,
    )


async def _generate_claim_display_texts(
    claim_results: list[dict[str, Any]],
    body_evidence: dict[str, Any],
) -> list[str]:
    return await _verification_generate_claim_display_texts(claim_results, body_evidence)


async def _retry_fill_hitl_review_when_empty(
    hitl_request: dict[str, Any],
    verification_summary: dict[str, Any],
    claim_results: list[dict[str, Any]],
    reasoning_text: str,
    *,
    empty_reasons: bool,
    empty_questions: bool,
) -> dict[str, Any]:
    return await _verification_retry_fill_hitl_review_when_empty(
        hitl_request,
        verification_summary,
        claim_results,
        reasoning_text,
        empty_reasons=empty_reasons,
        empty_questions=empty_questions,
    )


async def verify_node(state: AgentState) -> AgentState:
    return await _review_verify_node_impl(
        state,
        find_tool_result=_find_tool_result,
        derive_hitl_from_regulation=_derive_hitl_from_regulation,
        generate_claim_display_texts=_generate_claim_display_texts,
        build_hitl_request=build_hitl_request,
        generate_hitl_review_content=_generate_hitl_review_content,
        retry_fill_hitl_review_when_empty=_retry_fill_hitl_review_when_empty,
        call_node_llm_with_consistency_check=call_node_llm_with_consistency_check,
        stream_reasoning_events_with_llm=_stream_reasoning_events_with_llm,
    )


def _route_after_critic(state: AgentState) -> str:
    return _routes_after_critic(state, max_critic_loop=_MAX_CRITIC_LOOP)


def _route_after_verify(state: AgentState) -> str:
    return _routes_after_verify(state)


def _get_hitl_response_value(hitl_response: dict[str, Any], field: str) -> Any:
    return _routes_get_hitl_response_value(hitl_response, field)


async def hitl_validate_node(state: AgentState) -> AgentState:
    return await _outcome_hitl_validate_node_impl(
        state,
        get_hitl_response_value=_get_hitl_response_value,
    )


def _format_hitl_reason_for_stream(hitl_payload: dict[str, Any]) -> str:
    return _hitl_format_reason_for_stream(hitl_payload)


def _route_after_hitl_validate(state: AgentState) -> str:
    return _routes_after_hitl_validate(state)


async def hitl_pause_node(state: AgentState) -> AgentState:
    return await _outcome_hitl_pause_node_impl(
        state,
        interrupt_fn=interrupt,
    )


async def reporter_node(state: AgentState) -> AgentState:
    return await _outcome_reporter_node_impl(
        state,
        score_with_hitl_adjustment=_score_with_hitl_adjustment,
        format_occurred_at=_format_occurred_at,
        llm_decide_hitl_verdict=_llm_decide_hitl_verdict,
        llm_summarize_hold_reason=_llm_summarize_hold_reason,
        is_verify_ready_without_hitl=_is_verify_ready_without_hitl,
        top_policy_refs=_top_policy_refs,
        select_policy_refs_by_relevance=_select_policy_refs_by_relevance,
        call_node_llm_with_consistency_check=call_node_llm_with_consistency_check,
        stream_reasoning_events_with_llm=_stream_reasoning_events_with_llm,
    )


async def finalizer_node(state: AgentState) -> AgentState:
    return await _outcome_finalizer_node_impl(
        state,
        score_with_hitl_adjustment=_score_with_hitl_adjustment,
        is_verify_ready_without_hitl=_is_verify_ready_without_hitl,
        llm_summarize_completed_reason=_llm_summarize_completed_reason,
        build_grounded_reason=_build_grounded_reason,
        call_node_llm_with_consistency_check=call_node_llm_with_consistency_check,
        stream_reasoning_events_with_llm=_stream_reasoning_events_with_llm,
        build_system_auto_finalize_blockers=_build_system_auto_finalize_blockers,
        generate_hitl_review_content=_generate_hitl_review_content,
        retry_fill_hitl_review_when_empty=_retry_fill_hitl_review_when_empty,
        find_tool_result=_find_tool_result,
    )
