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
from agent.langgraph_hitl_helpers import (
    _assess_hitl_resolution_requirements as _hitl_assess_resolution_requirements,
    _build_system_auto_finalize_blockers as _hitl_build_system_auto_finalize_blockers,
    _format_hitl_reason_for_stream as _hitl_format_reason_for_stream,
    _is_verify_ready_without_hitl as _hitl_is_verify_ready_without_hitl,
    _pick_llm_review_reason as _hitl_pick_llm_review_reason,
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
    _score_to_severity as _scoring_score_to_severity,
    _score_with_hitl_adjustment as _scoring_score_with_hitl_adjustment,
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
_CLAIM_PRIORITY: dict[str, int] = {
    "night_violation": 10,
    "holiday_hr_conflict": 9,
    "merchant_high_risk": 8,
    "budget_exceeded": 7,
    "amount_approval_tier": 6,
    "policy_ref_direct": 5,
}


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
    critic_loop_count: int
    replan_context: dict[str, Any] | None
    plan_achievement: dict[str, Any]
    # workspace.md: 이전 노드 결과 1줄 요약 (generate_working_note prev_result_summary용)
    last_node_summary: str


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
    """
    LLM에게 케이스 유형·업종에 맞는 규정만 골라 인용하도록 요청한다.
    로직 강제가 아니라 LLM 판단으로, retrieval 결과 중 이 케이스에 적용 가능한 조문만 남긴다.
    """
    if not refs or not settings.openai_api_key:
        return refs
    body = state.get("body_evidence") or {}
    case_type = str(body.get("case_type") or state.get("intended_risk_type") or "").strip()
    merchant = str(body.get("merchantName") or "").strip() or "거래처 미상"
    mcc = str(body.get("mccCode") or body.get("mccName") or "").strip()
    ref_lines = []
    for i, r in enumerate(refs):
        art = str(r.get("article") or r.get("regulation_article") or "").strip()
        title = str(r.get("parent_title") or "").strip()
        ref_lines.append(f"[{i}] {art} {title}")
    ref_block = "\n".join(ref_lines)
    system = (
        "당신은 감사 보고서에 인용할 규정을 선택하는 역할이다. "
        "주어진 케이스 유형·업종(가맹점, MCC)에 실제로 적용 가능한 규정 조문만 골라야 한다. "
        "예: 휴일·식대 건이면 휴일/식대/심야 관련 조문만 채택하고, 차량유지비·차량 관련 조문은 이 케이스에 해당하지 않으면 제외한다. "
        "JSON만 출력: {\"applicable_indices\": [0, 1, ...]} (해당하는 인덱스만 나열). 하나도 해당 없으면 빈 배열."
    )
    user = (
        f"케이스 유형: {case_type or '미분류'}\n가맹점: {merchant}\nMCC/업종: {mcc or '-'}\n\n"
        f"규정 후보:\n{ref_block}\n\n"
        "이 케이스에 적용 가능한 규정의 인덱스만 applicable_indices 배열로 출력하라."
    )
    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI

        base_url = (getattr(settings, "openai_base_url", None) or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client = AsyncAzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_ep,
                api_version=getattr(settings, "openai_api_version", "2024-02-15-preview"),
            )
        else:
            client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base_url or None)
        tok_kw = {"max_completion_tokens": 1200} if is_azure else {"max_tokens": 1200}
        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **tok_kw,
            ),
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        indices = parsed.get("applicable_indices")
        if isinstance(indices, list) and indices:
            return [refs[i] for i in indices if 0 <= i < len(refs)]
        return refs
    except Exception:
        return refs


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
    """
    담당자 검토(HITL) 응답에 대해 확정 vs 재검토를 룰이 아닌 LLM 판단으로 결정한다.
    검토 필요로 올라온 건에 대해 필요한 답변/근거 없이 승인만 온 경우 재검토가 나와야 정상이지만,
    그 판단은 LLM이 맥락을 보고 하도록 함. 하드코딩/룰베이스 금지.
    반환: (verdict, reason) — verdict는 COMPLETED_AFTER_HITL 또는 REVIEW_REQUIRED.
    """
    logger.info("[VERDICT_LLM] _llm_decide_hitl_verdict 진입 (확정 vs 재검토 LLM 판단)")
    why = str(hitl_request.get("why_hitl") or hitl_request.get("blocking_reason") or "").strip()
    reasons = hitl_request.get("reasons") or hitl_request.get("auto_finalize_blockers") or []
    questions = hitl_request.get("review_questions") or hitl_request.get("questions") or []
    required_inputs = hitl_request.get("required_inputs") or []
    comment = str(hitl_response.get("comment") or "").strip()
    approved = hitl_response.get("approved") is True
    extra = hitl_response.get("extra_facts") or {}
    evidence_passed = None
    if isinstance(evidence_result, dict) and evidence_result:
        evidence_passed = evidence_result.get("passed") is True

    context = {
        "why_hitl": why,
        "reasons": reasons[:10],
        "review_questions": questions[:3],
        "required_inputs": [r.get("field") for r in required_inputs[:5] if r.get("field")],
        "reviewer_approved": approved,
        "reviewer_comment_length": len(comment),
        "reviewer_comment_preview": (comment[:200] + "…") if len(comment) > 200 else comment,
        "reviewer_extra_facts_keys": list(extra.keys())[:10] if isinstance(extra, dict) else [],
        "evidence_upload_passed": evidence_passed,
    }
    system = (
        "당신은 엔터프라이즈 감사 에이전트의 판단자다. "
        "담당자 검토(HITL)가 필요했던 전표에 대해 담당자가 응답을 제출했다. "
        "이 응답만 보고 '최종 확정(COMPLETED_AFTER_HITL)' vs '재검토 필요(REVIEW_REQUIRED)' 중 하나를 판단하라. "
        "필요한 답변·근거 없이 승인만 한 경우(의견 없음 또는 극히 짧은 무의미한 문구) 정상적으로는 재검토가 나와야 한다. "
        "단, 담당자가 승인하고 검토 의견에 자신의 판단을 명시한 경우(예: 문제 없음, 특이 사항, 승인 가능, 이번 건 괜찮음 등)에는 "
        "질문 문장과 완전히 같은 표현이 없어도 '질문에 대한 답변으로 해석 가능한 판단'으로 보아 COMPLETED_AFTER_HITL을 우선 고려하라. "
        "규정·업무 맥락상 담당자 판단을 존중하는 방향으로 맥락을 보고 판단하라. "
        "룰이나 키워드로만 치지 말고, 검토 요청 사유·질문과 담당자 응답 내용을 비교해 판단하라. "
        "JSON만 출력한다. 키: verdict(반드시 COMPLETED_AFTER_HITL 또는 REVIEW_REQUIRED), reason(한 문장 이유, 한국어)."
    )
    user = (
        "[검토 요청 맥락]\n"
        f"검토 필요 사유: {why or '(없음)'}\n"
        f"요청 사유 목록: {json.dumps(reasons, ensure_ascii=False)}\n"
        f"검토 시 확인할 질문: {json.dumps(questions, ensure_ascii=False)}\n"
        f"필수 입력 항목: {context['required_inputs']}\n\n"
        "[담당자 응답]\n"
        f"승인 여부: {approved}\n"
        f"검토 의견 길이: {len(comment)}자\n"
        f"의견 미리보기: {context['reviewer_comment_preview'] or '(없음)'}\n"
        f"추가 사실 키: {context['reviewer_extra_facts_keys']}\n"
        f"증빙 업로드 검증 통과: {evidence_passed}\n\n"
        "위 맥락을 바탕으로 verdict와 reason을 JSON으로 출력하라."
    )
    logger.info(
        "[VERDICT_LLM] 호출 직전 approved=%s comment_len=%s comment_preview=%s",
        approved,
        len(comment),
        (comment[:80] + "…") if len(comment) > 80 else (comment or "(없음)"),
    )
    raw = ""
    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI

        base_url = (getattr(settings, "openai_base_url") or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client = AsyncAzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_ep,
                api_version=getattr(settings, "openai_api_version", "2024-02-15-preview"),
            )
        else:
            client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base_url or None)
        # Azure(gpt-4o-mini 등)는 max_tokens 미지원, max_completion_tokens 사용. finish_reason=length 시 800으로도 JSON 전에 잘림 → 1200으로 여유.
        completion_tokens_kw = {"max_completion_tokens": 5000} if is_azure else {"max_tokens": 5000}
        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **completion_tokens_kw,
            ),
        )
        choice = response.choices[0] if response.choices else None
        raw = (choice.message.content if choice and choice.message else None) or ""
        raw = raw.strip()
        usage = getattr(response, "usage", None)
        logger.info(
            "[VERDICT_LLM] 응답 수신 content_len=%s finish_reason=%s usage=%s",
            len(raw),
            getattr(choice, "finish_reason", None) if choice else None,
            (f"prompt={getattr(usage, 'prompt_tokens', None)} completion={getattr(usage, 'completion_tokens', None)}") if usage else None,
        )
        if not raw and choice:
            finish = getattr(choice, "finish_reason", None)
            refusal = getattr(choice.message, "refusal", None) if choice.message else None
            finish_details = getattr(choice, "finish_details", None)
            logger.warning(
                "[VERDICT_LLM] 응답 본문 없음 — finish_reason=%s refusal=%s finish_details=%s (content_filter/length 등 원인 확인용)",
                finish,
                refusal,
                finish_details,
            )
            # LLM 실패 시: 담당자 승인 시 답변 길이 무관하게 완료 fallback
            if approved:
                logger.info(
                    "[VERDICT_LLM] fallback: 응답 비어 있음, approved=True → COMPLETED_AFTER_HITL (답변 길이 제한 없음)",
                )
                return ("COMPLETED_AFTER_HITL", "담당자 승인 및 검토 의견 반영으로 확정(LLM 판단 보조 실패 시 적용).")
            logger.warning(
                "[VERDICT_LLM] fallback: 응답 비어 있음, approved=False → REVIEW_REQUIRED",
            )
            return ("REVIEW_REQUIRED", "판단 LLM 응답이 비어 있어 재검토 필요 처리.")
        parsed = json.loads(raw)
        v = str(parsed.get("verdict") or "").upper()
        if v not in ("COMPLETED_AFTER_HITL", "REVIEW_REQUIRED"):
            v = "REVIEW_REQUIRED"
        reason = str(parsed.get("reason") or "").strip() or "담당자 검토 응답을 기준으로 판단함."
        logger.info(
            "[VERDICT_LLM] LLM 정상 응답 (검토 필요 여부는 LLM 판단) verdict=%s reason=%s raw_preview=%s",
            v,
            (reason[:80] + "…") if len(reason) > 80 else reason,
            (raw[:200] + "…") if len(raw) > 200 else raw,
        )
        return (v, reason)
    except Exception as e:
        raw_preview = (raw[:300] + "…") if len(raw) > 300 else (raw or "(empty or not available)")
        # LLM 예외 시에도 담당자 승인 시 답변 길이 무관하게 완료 fallback
        if approved:
            logger.info(
                "[VERDICT_LLM] fallback: LLM 호출 실패 (%s) approved=True → COMPLETED_AFTER_HITL (답변 길이 제한 없음)",
                type(e).__name__,
            )
            return ("COMPLETED_AFTER_HITL", "담당자 승인 및 검토 의견 반영으로 확정(LLM 판단 실패 시 적용).")
        logger.warning(
            "[VERDICT_LLM] fallback: LLM 호출 실패 (%s) approved=False → REVIEW_REQUIRED. raw_preview=%s",
            type(e).__name__,
            raw_preview,
        )
        return ("REVIEW_REQUIRED", "판단 LLM 호출 실패로 재검토 필요 처리.")


async def _llm_summarize_hold_reason(hitl_response: dict[str, Any]) -> str:
    """
    담당자가 보류를 선택했을 때, HITL 의견(comment 등)을 LLM이 한 문장으로 요약해 판단요약에 쓴다.
    하드코딩 '담당자 사유: {comment}' 대신 LLM 해석 문장을 적용.
    """
    comment = str(hitl_response.get("comment") or "").strip()
    business_purpose = str(hitl_response.get("business_purpose") or "").strip()
    attendees = hitl_response.get("attendees")
    if isinstance(attendees, list):
        attendees_str = ", ".join(str(x) for x in attendees[:5] if x)
    else:
        attendees_str = str(attendees or "").strip()
    parts = [p for p in [comment, business_purpose, attendees_str] if p]
    if not parts:
        return ""
    text = "\n".join(f"- {p[:300]}" for p in parts)
    system = (
        "당신은 감사 판단요약 문구를 작성하는 보조자다. "
        "담당자가 검토 보류를 선택했고, 아래와 같은 의견/사유를 제출했다. "
        "이 내용을 '담당자 검토 결과 보류합니다.' 뒤에 붙일 한 문장(한국어, 80자 내외)으로 요약하라. "
        "원문을 그대로 나열하지 말고, 요지만 정리한 한 문장으로 출력하라. 다른 설명 없이 그 한 문장만 출력하라."
    )
    user = f"[담당자 제출 내용]\n{text}"
    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI

        base_url = (getattr(settings, "openai_base_url") or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client = AsyncAzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_ep,
                api_version=getattr(settings, "openai_api_version", "2024-02-15-preview"),
            )
        else:
            client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base_url or None)
        completion_tokens_kw = {"max_completion_tokens": 256} if is_azure else {"max_tokens": 256}
        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=settings.reasoning_llm_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **completion_tokens_kw,
            )
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw:
            # 한 문장만 취함(첫 줄)
            first = raw.split("\n")[0].strip()
            if len(first) > 300:
                first = first[:297] + "…"
            logger.info("[VERDICT_LLM] HOLD 사유 LLM 요약: %s", first[:100] + "…" if len(first) > 100 else first)
            return first
    except Exception as e:
        logger.warning("[VERDICT_LLM] HOLD 사유 LLM 요약 실패: %s", type(e).__name__)
    return ""


async def _llm_summarize_completed_reason(state: AgentState) -> str:
    """
    자동 확정(COMPLETED)인 경우, 수집된 증거·규정·검증 결과를 바탕으로
    '왜 확정인지' 한 문장을 LLM이 생성해 판단요약 꼬리에 쓴다.
    하드코딩 '현재 수집된 증거 기준으로 자동 확정되었습니다.' 대신 증거 기반 사유를 노출.
    """
    body = state.get("body_evidence") or {}
    tool_results = state.get("tool_results") or []
    verification = state.get("verification") or {}
    reporter_output = state.get("reporter_output") or {}
    score = _score_with_hitl_adjustment(state.get("score_breakdown") or {}, state.get("flags") or {})

    voucher_line = _voucher_summary_for_context(body)
    case_type = str(body.get("case_type") or state.get("intended_risk_type") or "").strip() or "미분류"

    # 규정·증거 요약
    refs = _top_policy_refs(tool_results, limit=5)
    ref_labels = []
    for r in refs:
        art = (r.get("article") or r.get("regulation_article") or "").strip()
        title = (r.get("parent_title") or "").strip()
        ref_labels.append(f"{art} ({title})" if title else art)
    policy_line = "규정: " + ", ".join(ref_labels) if ref_labels else "규정: (없음)"

    doc_result = _find_tool_result(tool_results, "document_evidence_probe")
    line_count = int(((doc_result or {}).get("facts") or {}).get("lineItemCount") or 0)
    policy_result = _find_tool_result(tool_results, "policy_rulebook_probe")
    ref_count = int(((policy_result or {}).get("facts") or {}).get("ref_count") or 0)
    evidence_line = f"규정 조항 {ref_count}건, 전표 라인 {line_count}건 확보."

    holiday_result = _find_tool_result(tool_results, "holiday_compliance_probe")
    merchant_result = _find_tool_result(tool_results, "merchant_risk_probe")
    risk_parts = []
    if holiday_result:
        h_risk = (holiday_result.get("facts") or {}).get("holidayRisk")
        if h_risk is not True:
            risk_parts.append("휴일 위험 없음")
    if merchant_result:
        m_risk = str((merchant_result.get("facts") or {}).get("merchantRisk") or "UNKNOWN").upper()
        if m_risk in ("LOW", "UNKNOWN"):
            risk_parts.append(f"가맹점 위험도 {m_risk}")
    risk_line = " ".join(risk_parts) if risk_parts else ""

    verifier_output = state.get("verifier_output") or {}
    gate = str(getattr(verifier_output.get("gate"), "value", verifier_output.get("gate")) or "").upper()
    if gate.startswith("VERIFIERGATE."):
        gate = gate.split(".", 1)[1]
    quality = verification.get("quality_signals") or []
    quality_line = f"검증 게이트: {gate}, 품질: {quality}" if gate or quality else ""

    summary = (reporter_output.get("summary") or "").strip()
    context_parts = [
        f"[전표] {voucher_line}",
        f"[케이스유형] {case_type}",
        policy_line,
        evidence_line,
        f"[점수] 정책 {score['policy_score']}점, 근거 {score['evidence_score']}점",
    ]
    if risk_line:
        context_parts.append(f"[위험점검] {risk_line}")
    if quality_line:
        context_parts.append(quality_line)
    if summary:
        context_parts.append(f"[보고요약] {summary[:300]}")
    text = "\n".join(context_parts)

    system = (
        "당신은 감사 판단요약 문구를 작성하는 보조자다. "
        "아래 분석 결과는 이미 '자동 확정(COMPLETED)'으로 판단된 전표이다. "
        "수집된 증거·규정 근거·검증 결과를 바탕으로, 사용자가 '왜 확정인지' 이해할 수 있게 한 문장(한국어, 100자 내외)으로 요약하라. "
        "예: 평일 업무 시간대 일반 식대 사용, 규정 위험 없음, 증빙·규정 조항 충족 등 수집된 증거상 위반 소지가 없어 자동 확정하였습니다. "
        "다른 설명 없이 그 한 문장만 출력하라."
    )
    user = f"[분석 결과]\n{text}"
    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI

        base_url = (getattr(settings, "openai_base_url") or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client = AsyncAzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_ep,
                api_version=getattr(settings, "openai_api_version", "2024-02-15-preview"),
            )
        else:
            client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base_url or None)
        completion_tokens_kw = {"max_completion_tokens": 256} if is_azure else {"max_tokens": 256}
        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=settings.reasoning_llm_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **completion_tokens_kw,
            )
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw:
            first = raw.split("\n")[0].strip()
            if len(first) > 350:
                first = first[:347] + "…"
            logger.info("[VERDICT_LLM] COMPLETED 확정 사유 LLM 요약: %s", first[:100] + "…" if len(first) > 100 else first)
            return first
    except Exception as e:
        logger.warning("[VERDICT_LLM] COMPLETED 확정 사유 LLM 요약 실패: %s", type(e).__name__)
    return ""


def _pick_llm_review_reason(hitl_request: dict[str, Any]) -> str:
    return _hitl_pick_llm_review_reason(hitl_request)


def _is_verify_ready_without_hitl(state: AgentState) -> bool:
    return _hitl_is_verify_ready_without_hitl(state)


def _build_grounded_reason(state: AgentState, completed_tail: str | None = None) -> tuple[str, str]:
    score = _score_with_hitl_adjustment(state["score_breakdown"], state["flags"])
    hitl_request = state.get("hitl_request")
    body = state["body_evidence"]
    prior_hitl_request = (body.get("hitlRequest") or {}) if isinstance(body.get("hitlRequest"), dict) else {}
    occurred_at = _format_occurred_at(body.get("occurredAt"))
    merchant = body.get("merchantName") or "거래처 미상"
    refs = _top_policy_refs(state.get("tool_results", []), limit=5)
    _article_in_merged_re = re.compile(r"제\s*\d+\s*조")
    # 병합 조문(parent_title에 " ~ ")에 등장하는 모든 조번: 단일 ref로 중복 표시하지 않음
    articles_in_merged: set[str] = set()
    for ref in refs:
        pt = (ref.get("parent_title") or "").strip()
        if " ~ " in pt:
            for m in _article_in_merged_re.finditer(pt):
                articles_in_merged.add(m.group(0).strip())
    ref_labels: list[str] = []
    for ref in refs:
        article = ref.get("article") or ref.get("regulation_article") or "조항 미상"
        parent_title = (ref.get("parent_title") or "").strip()
        art_stripped = (article or "").strip()
        # 병합 라벨에 이미 포함된 조문의 단일 ref는 생략 (제39조가 "제38조 ~ 제39조"에 있으면 단일 "제39조" 제외)
        if art_stripped and art_stripped in articles_in_merged and " ~ " not in parent_title:
            continue
        if not parent_title:
            label = article
        else:
            if art_stripped and (
                parent_title == art_stripped
                or parent_title.startswith(art_stripped + " ")
                or parent_title.startswith(art_stripped + "(")
            ):
                label = parent_title
            else:
                label = f"{article} ({parent_title})"
        ref_labels.append(label)

    intro = f"전표는 {occurred_at} 시점 {merchant} 사용 건으로 분석되었습니다. "
    score_text = f"정책점수 {score['policy_score']}점, 근거점수 {score['evidence_score']}점이 반영되었습니다. "
    if ref_labels:
        grounding = f"관련 규정 근거는 {', '.join(ref_labels)}입니다. "
    else:
        grounding = "현재 직접 연결된 규정 근거는 제한적입니다. "

    if hitl_request:
        status = "HITL_REQUIRED"
        tail = "현재는 담당자 검토가 필요한 상태로 분류되었으며, 추가 소명 확인 후 최종 확정이 필요합니다."
    else:
        if state["flags"].get("hasHitlResponse"):
            if state["flags"].get("hitlApproved") is True:
                # reporter가 LLM으로 판단한 verdict를 그대로 사용 (룰/하드코딩 대신 일관된 LLM 판단)
                reporter_verdict = (state.get("reporter_output") or {}).get("verdict")
                hitl_reason = (state.get("reporter_output") or {}).get("hitl_verdict_reason") or ""
                if reporter_verdict in ("COMPLETED_AFTER_HITL", "REVIEW_REQUIRED"):
                    status = reporter_verdict
                    tail = hitl_reason if hitl_reason else (
                        "담당자 검토 결과 승인 가능으로 확인되어 최종 판단을 확정했습니다." if status == "COMPLETED_AFTER_HITL" else "담당자 검토 기준 미충족으로 재검토가 필요합니다."
                    )
                    if status == "REVIEW_REQUIRED":
                        logger.info("[FINAL_STATUS] status=REVIEW_REQUIRED (출처: verdict LLM 반환 — reporter_verdict=REVIEW_REQUIRED)")
                else:
                    status = "REVIEW_REQUIRED"
                    tail = hitl_reason if hitl_reason else "담당자 검토 기준 미충족으로 재검토가 필요합니다."
                    logger.info("[FINAL_STATUS] status=REVIEW_REQUIRED (출처: hasHitlResponse=True이나 reporter_verdict 미사용, finalizer fallback)")
            else:
                status = "HOLD_AFTER_HITL"
                hitl_reason = (state.get("reporter_output") or {}).get("hitl_verdict_reason") or ""
                if hitl_reason:
                    tail = f"담당자 검토 결과 보류합니다. {hitl_reason}"
                else:
                    hitl_response = (body.get("hitlResponse") or {}) if isinstance(body.get("hitlResponse"), dict) else {}
                    hold_reason = (hitl_response.get("comment") or hitl_response.get("business_purpose") or "").strip()
                    if hold_reason:
                        tail = f"담당자 검토 결과 보류합니다. 담당자 사유: {hold_reason[:400]}{'…' if len(hold_reason) > 400 else ''}"
                    else:
                        tail = "담당자 검토 결과 보류/추가 검토가 필요해 자동 확정을 중단합니다."
        else:
            reporter_verdict = str((state.get("reporter_output") or {}).get("verdict") or "").upper()
            verify_ready = _is_verify_ready_without_hitl(state)
            if verify_ready and reporter_verdict in {"", "READY", "COMPLETED", "AUTO_APPROVED"}:
                status = "COMPLETED"
                tail = (completed_tail or "현재 수집된 증거 기준으로 자동 확정되었습니다.").strip() or "현재 수집된 증거 기준으로 자동 확정되었습니다."
            else:
                status = "REVIEW_REQUIRED"
                tail = "현재 수집된 증거 기준으로 우선 검토 대상입니다."
                logger.info(
                    "[FINAL_STATUS] status=REVIEW_REQUIRED (출처: finalizer 룰 — verify_ready=%s reporter_verdict=%s, verdict LLM 미호출)",
                    verify_ready,
                    reporter_verdict,
                )
    return intro + grounding + score_text + tail, status


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


def _build_prescreened_result(body: dict[str, Any]) -> dict[str, Any]:
    return _domain_build_prescreened_result(body)


async def start_router_node(state: AgentState) -> AgentState:
    """
    사전 스크리닝된 전표(case_type 있음)면 screening_result를 주입하고 다음에 intake로 직행하고,
    아니면 빈 업데이트만 반환해 다음에 screener로 보낸다.
    """
    body = state.get("body_evidence") or {}
    pre_classified = body.get("case_type") or body.get("intended_risk_type")
    if not _is_valid_screening_case_type(pre_classified):
        return {}
    screening = _build_prescreened_result(body)
    updated_body = {**body, "case_type": screening["case_type"], "intended_risk_type": screening["case_type"]}
    return {
        "screening_result": screening,
        "intended_risk_type": screening["case_type"],
        "body_evidence": updated_body,
    }


async def screener_node(state: AgentState) -> AgentState:
    """
    Phase 0 — Screening.
    Runs deterministic signal analysis on raw body_evidence to classify
    the case_type BEFORE the agent begins deep analysis.
    This mirrors the original aura-platform /detect/screen flow.
    (사전 스크리닝된 경우 start_router에서 intake로 가므로 이 노드는 미스크리닝 건만 처리)
    """
    body = state["body_evidence"]

    # If case_type was already screened (AgentCase exists), skip re-screening
    # but still emit the SCREENING_RESULT event so it shows in the stream.
    pre_classified = body.get("case_type") or body.get("intended_risk_type")
    if _is_valid_screening_case_type(pre_classified) and str(pre_classified).upper() != "NORMAL_BASELINE":
        screening = {
            "case_type": pre_classified,
            "severity": body.get("severity") or "MEDIUM",
            "score": body.get("screening_score") or 0,
            "reasons": ["기존 스크리닝 결과를 사용합니다."],
            "reason_text": f"기존 분류: {pre_classified}",
        }
    else:
        screening = await asyncio.to_thread(run_screening, body)

    # Propagate screened case_type into body_evidence so downstream nodes use it
    updated_body = {**body, "case_type": screening["case_type"], "intended_risk_type": screening["case_type"]}

    label_map = {
        "HOLIDAY_USAGE": "휴일/휴무 중 사용 의심",
        "LIMIT_EXCEED": "한도 초과 의심",
        "PRIVATE_USE_RISK": "사적 사용 위험",
        "UNUSUAL_PATTERN": "비정상 패턴",
        "NORMAL_BASELINE": "정상 범위",
    }
    label = label_map.get(screening["case_type"], screening["case_type"])
    severity = screening.get("severity", "MEDIUM")
    score = screening.get("score", 0)
    reason_text = screening.get("reason_text", "")

    return {
        "body_evidence": updated_body,
        "intended_risk_type": screening["case_type"],
        "screening_result": screening,
        "pending_events": [
            AgentEvent(
                event_type="NODE_START",
                node="screener",
                phase="screen",
                message="전표 데이터를 분석해 케이스(위반 유형)를 분류합니다.",
                thought="원시 전표 데이터에서 위반 유형을 식별합니다.",
                action="전표 데이터 추출 및 점수 산정",
                observation="근태 상태, 업종 코드, 전표 기준일, 입력 시각 등 핵심 필드를 분석합니다.",
                metadata={"role": "screener"},
            ).to_payload(),
            AgentEvent(
                event_type="SCREENING_RESULT",
                node="screener",
                phase="screen",
                message=f"스크리닝 완료: [{label}] — 중요도 {severity} / 점수 {score}",
                thought=f"전표 데이터를 바탕으로 '{screening['case_type']}' 유형으로 분류되었습니다.",
                action="케이스 유형 분류 완료",
                observation=reason_text,
                metadata={
                    "case_type": screening["case_type"],
                    "severity": severity,
                    "score": score,
                    "reasons": screening.get("reasons", []),
                },
            ).to_payload(),
            AgentEvent(
                event_type="NODE_END",
                node="screener",
                phase="screen",
                message="스크리닝 단계가 완료되었습니다.",
                metadata={
                    "case_type": screening["case_type"],
                    "severity": severity,
                    "score": score,
                },
            ).to_payload(),
        ],
    }


async def intake_node(state: AgentState) -> AgentState:
    flags = _derive_flags(state["body_evidence"])
    pending: list[dict[str, Any]] = []

    # 사전 스크리닝된 경우(screener 건너뜀): 스트림/타임라인용 SCREENING_RESULT 이벤트를 먼저 발행
    screening_result = state.get("screening_result")
    if screening_result:
        label_map = {
            "HOLIDAY_USAGE": "휴일/휴무 중 사용 의심",
            "LIMIT_EXCEED": "한도 초과 의심",
            "PRIVATE_USE_RISK": "사적 사용 위험",
            "UNUSUAL_PATTERN": "비정상 패턴",
            "NORMAL_BASELINE": "정상 범위",
        }
        label = label_map.get(screening_result.get("case_type", ""), screening_result.get("case_type", ""))
        severity = screening_result.get("severity", "MEDIUM")
        score = screening_result.get("score", 0)
        reason_text = screening_result.get("reason_text", "")
        pending.append(
            AgentEvent(
                event_type="SCREENING_RESULT",
                node="screener",
                phase="screen",
                message=f"스크리닝 완료(사전 적용): [{label}] — 중요도 {severity} / 점수 {score}",
                thought="테스트 데이터 생성 시점에 적용된 스크리닝 결과를 사용합니다.",
                action="사전 스크리닝 결과 반영",
                observation=reason_text,
                metadata={
                    "case_type": screening_result.get("case_type"),
                    "severity": severity,
                    "score": score,
                    "reasons": screening_result.get("reasons", []),
                    "reasonText": reason_text,
                },
            ).to_payload(),
        )

    reasoning_parts = ["전표 입력값에서 핵심 위험 지표를 추출했다."]
    if flags.get("isHoliday"):
        reasoning_parts.append("휴일 사용 정황이 감지되었다.")
    if flags.get("budgetExceeded"):
        reasoning_parts.append("예산 초과 플래그가 있다.")
    if flags.get("mccCode"):
        reasoning_parts.append("MCC 업종 코드가 있어 업종 위험 검증이 필요하다.")
    reasoning_text = " ".join(reasoning_parts)
    last_node_summary = f"intake 완료: {reasoning_text[:60]}…" if len(reasoning_text) > 60 else f"intake 완료: {reasoning_text}"
    pending.append(
        AgentEvent(event_type="NODE_START", node="intake", phase="analyze", message="입력 데이터를 정규화합니다.", metadata=dict(flags)).to_payload(),
    )
    intake_context = {
        "flags": flags,
        "last_node_summary": state.get("last_node_summary", "없음"),
    }
    reasoning_text, reasoning_events, note_source = await _stream_reasoning_events_with_llm("intake", reasoning_text, context=intake_context)
    pending.extend(reasoning_events)
    pending.append(
        AgentEvent(
            event_type="NODE_END",
            node="intake",
            phase="analyze",
            message="입력 정규화가 완료되었습니다.",
            metadata={"reasoning": reasoning_text, "note_source": note_source, **flags},
        ).to_payload(),
    )
    return {
        "flags": flags,
        "last_node_summary": last_node_summary,
        "pending_events": pending,
    }


def _available_planner_tools() -> list[dict[str, str]]:
    """LLM Planner에 제공할 도구 목록. 레지스트리와 동기화."""
    tools_by_name = _get_tools_by_name()
    templates = [
        ("holiday_compliance_probe", "isHoliday=True 또는 hrStatus가 LEAVE/OFF/VACATION일 때"),
        ("budget_risk_probe", "budgetExceeded=True일 때"),
        ("merchant_risk_probe", "mccCode가 있을 때"),
        ("document_evidence_probe", "항상 실행 (전표 증거 수집)"),
        ("policy_rulebook_probe", "항상 실행 (규정 조항 조회)"),
        ("legacy_aura_deep_audit", "enable_legacy_aura_specialist=True이고 증거가 부족할 때"),
    ]
    return [{"name": name, "when": when} for name, when in templates if name in tools_by_name]


async def _invoke_llm_planner(
    flags: dict[str, Any],
    screening: dict[str, Any],
    replan_context: dict[str, Any] | None,
    available_tools: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """LLM으로 계획 JSON 생성. 실패 시 빈 리스트 반환."""
    system_prompt = (
        "당신은 기업 경비 감사 에이전트의 Planner다.\n"
        "아래 케이스 정보를 분석하여 최적의 도구 실행 순서를 결정하라.\n"
        "규칙:\n"
        "1. 공통 사항: 규정상 모든 전표는 증빙이 필요하므로 document_evidence_probe(전표 증거 수집)와 policy_rulebook_probe(규정 조항 조회)는 반드시 계획에 포함하라.\n"
        "2. 케이스 유형(휴일/한도/업종 등)에 따라 holiday_compliance_probe, budget_risk_probe, merchant_risk_probe 등을 추가로 포함하라.\n"
        "3. 앞 도구 결과가 뒷 도구에 영향을 준다면 순서를 고려하라.\n"
        "4. 반드시 JSON 배열로만 응답하라. 각 항목: {\"tool\": string, \"reason\": string}\n"
        "5. 배열 외 텍스트, 마크다운 금지.\n"
    )
    user_prompt = (
        f"케이스 유형: {screening.get('case_type', 'UNKNOWN')}\n"
        f"심각도: {screening.get('severity', 'MEDIUM')}\n"
        f"플래그: isHoliday={flags.get('isHoliday')}, "
        f"hrStatus={flags.get('hrStatus')}, "
        f"budgetExceeded={flags.get('budgetExceeded')}, "
        f"mccCode={flags.get('mccCode')}, "
        f"isNight={flags.get('isNight')}, "
        f"amount={flags.get('amount')}\n"
    )
    if replan_context:
        user_prompt += (
            "\n[재계획 모드]\n"
            f"이전 실행 도구: {replan_context.get('previous_tool_results', [])}\n"
            f"Critic 피드백: {replan_context.get('critic_feedback', '')}\n"
            f"누락 필드: {replan_context.get('missing_fields', [])}\n"
            "이미 실행된 도구는 꼭 필요한 경우에만 재포함하라."
        )
    user_prompt += f"\n\n사용 가능한 도구:\n{json.dumps(available_tools, ensure_ascii=False)}"

    valid_names = {t["name"] for t in available_tools}
    if not getattr(settings, "openai_api_key", None):
        return []
    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI

        base_url = (getattr(settings, "openai_base_url") or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client = AsyncAzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_ep,
                api_version=getattr(settings, "openai_api_version", "2024-02-15-preview"),
            )
        else:
            kw: dict[str, Any] = {"api_key": settings.openai_api_key}
            if base_url:
                kw["base_url"] = base_url
            client = AsyncOpenAI(**kw)

        tok_kw = {"max_completion_tokens": 1200} if is_azure else {"max_tokens": 1200}
        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                **tok_kw,
            ),
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        raw_plan = parsed if isinstance(parsed, list) else (parsed.get("plan") or [])
        plan = [
            {"tool": step["tool"], "reason": step.get("reason", ""), "owner": "llm_planner"}
            for step in raw_plan
            if isinstance(step, dict) and step.get("tool") in valid_names
        ]
        return plan
    except Exception:
        return []


async def planner_node(state: AgentState) -> AgentState:
    replan_context = state.get("replan_context")
    flags = state["flags"]
    available_tools = _available_planner_tools()
    valid_tool_names = {t["name"] for t in available_tools}

    plan: list[dict[str, Any]] = []
    plan_source = "rule"
    if getattr(settings, "enable_llm_planner", True) and valid_tool_names:
        plan = await _invoke_llm_planner(flags, state.get("screening_result") or {}, replan_context, available_tools)
        if plan:
            plan_source = "llm"

    if not plan:
        base_plan = _plan_from_flags(flags)
        if replan_context:
            already_run = set(replan_context.get("previous_tool_results") or [])
            always_rerun = {"policy_rulebook_probe", "document_evidence_probe"}
            plan = [step for step in base_plan if step["tool"] not in already_run or step["tool"] in always_rerun]
            if not plan:
                plan = base_plan
        else:
            plan = base_plan
        if getattr(settings, "enable_llm_planner", True):
            plan_source = "fallback_rule"

    # 규정집 공통: 모든 전표는 증빙 필요(제14조). case_type 유무와 무관하게 항상 증빙 수집·규정 조회 포함.
    plan_tools = {step.get("tool") for step in plan}
    if "document_evidence_probe" not in plan_tools:
        plan.append({"tool": "document_evidence_probe", "reason": "공통: 전표 증빙(라인/항목) 수집", "owner": "common"})
    if "policy_rulebook_probe" not in plan_tools:
        plan.append({"tool": "policy_rulebook_probe", "reason": "공통: 규정 조항(증빙 의무 포함) 조회", "owner": "common"})

    def _build_planner_reasoning() -> str:
        lines: list[str] = []
        if plan_source == "llm":
            lines.append("LLM이 현재 신호와 재계획 문맥을 바탕으로 도구 실행 순서를 구성했습니다.")
        elif plan_source == "fallback_rule":
            lines.append("LLM 계획을 사용할 수 없어 규칙 기반 기본 경로로 계획을 구성했습니다.")
        else:
            lines.append("규칙 기반 기본 경로로 계획을 구성했습니다.")
        for idx, step in enumerate(plan, start=1):
            tool_name = step.get("tool", "unknown_tool")
            reason = str(step.get("reason") or "핵심 위험 신호 확인을 위해 포함")
            lines.append(f"{idx}) {tool_name}: {reason}")
        if replan_context:
            lines.append("비판 단계 피드백과 이전 실행 결과를 반영해 재계획했습니다.")
        return " ".join(lines)

    steps = [
        PlanStep(
            tool_name=step["tool"],
            purpose=step.get("reason", ""),
            required=True,
            skip_condition=None,
            owner=step.get("owner"),
        )
        for step in plan
    ]
    tool_sequence = [s["tool"] for s in plan]
    rationale = (
        "LLM이 도구 실행 순서를 결정했다."
        if plan_source == "llm"
        else "위험 신호 기반 규칙으로 도구 실행 순서를 결정했다."
    )
    reasoning_text = _build_planner_reasoning()
    planner_output = PlannerOutput(
        objective="위험 유형별 조사 순서에 따라 증거를 수집하고 규정 근거를 확보한다.",
        steps=steps,
        stop_after_sufficient_evidence=True,
        tool_budget=len(plan),
        rationale=rationale,
        reasoning=reasoning_text,
    )
    last_node_summary = f"planner 완료: {reasoning_text[:80]}…" if len(reasoning_text) > 80 else f"planner 완료: {reasoning_text}"
    pending: list[dict[str, Any]] = [
        AgentEvent(
            event_type="NODE_START",
            node="planner",
            phase="plan",
            message="조사 계획을 수립합니다.",
            metadata={"plan": plan, "plan_source": plan_source},
        ).to_payload(),
    ]
    planner_context = {
        "selected_tools": tool_sequence,
        "flags": state.get("flags") or {},
        "plan_source": plan_source,
        "last_node_summary": state.get("last_node_summary", "없음"),
    }
    reasoning_text, reasoning_events, note_source = await _stream_reasoning_events_with_llm("planner", reasoning_text, context=planner_context)
    pending.extend(reasoning_events)
    pending.append(
        AgentEvent(
            event_type="PLAN_READY",
            node="planner",
            phase="plan",
            message=reasoning_text or "조사 계획이 확정되었습니다.",
            metadata={"plan": plan, "reasoning": reasoning_text, "note_source": note_source, "plan_source": plan_source},
        ).to_payload(),
    )
    pending.append(
        AgentEvent(
            event_type="NODE_END",
            node="planner",
            phase="plan",
            message="조사 계획 수립이 완료되었습니다.",
            metadata={"plan_size": len(plan)},
        ).to_payload()
    )
    return {
        "plan": plan,
        "planner_output": planner_output.model_dump(),
        "replan_context": None,
        "last_node_summary": last_node_summary,
        "pending_events": pending,
    }


async def execute_node(state: AgentState) -> AgentState:
    tools_by_name = _get_tools_by_name()
    tool_results: list[dict[str, Any]] = []
    skipped_tools: list[str] = []
    failed_tools: list[str] = []
    pending_events: list[dict[str, Any]] = [
        AgentEvent(
            event_type="NODE_START",
            node="execute",
            phase="execute",
            message="계획된 도구를 순차 실행합니다.",
            metadata={"planned_tools": [step.get("tool") for step in state.get("plan") or []]},
        ).to_payload()
    ]
    plan = state.get("plan") or []
    use_parallel = getattr(settings, "enable_parallel_tool_execution", False)

    async def _invoke_one(
        step: dict[str, Any],
        prior: list[dict[str, Any]],
    ) -> dict[str, Any]:
        tool_name = step.get("tool", "")
        tool = tools_by_name.get(tool_name)
        if not tool:
            return {"tool": tool_name, "ok": False, "facts": {}, "summary": f"알 수 없는 도구: {tool_name}"}
        inp = ToolContextInput(
            case_id=state["case_id"],
            body_evidence=state["body_evidence"],
            intended_risk_type=state.get("intended_risk_type"),
            prior_tool_results=list(prior),
        )
        result = await tool.ainvoke(inp.model_dump())
        return result if isinstance(result, dict) else {"tool": tool_name, "ok": False, "facts": {}, "summary": str(result)}

    if use_parallel:
        parallel_steps = [s for s in plan if s.get("tool", "") not in _SEQUENTIAL_LAST_TOOLS]
        sequential_steps = [s for s in plan if s.get("tool", "") in _SEQUENTIAL_LAST_TOOLS]
        planned_tool_names = {s.get("tool", "") for s in plan if s.get("tool", "")}
        finished_parallel_tools: set[str] = set()
        remaining_parallel_steps = list(parallel_steps)

        while remaining_parallel_steps:
            ready_steps: list[dict[str, Any]] = []
            blocked_steps: list[dict[str, Any]] = []

            for step in remaining_parallel_steps:
                tool_name = step.get("tool", "")
                deps = {
                    dep for dep in _PARALLEL_TOOL_DEPENDENCIES.get(tool_name, frozenset())
                    if dep in planned_tool_names
                }
                if deps.issubset(finished_parallel_tools):
                    ready_steps.append(step)
                else:
                    blocked_steps.append(step)

            if not ready_steps and blocked_steps:
                # 방어 로직: 순환 의존/잘못된 의존 정의가 있어도 진행이 멈추지 않도록 1개는 강행
                ready_steps = [blocked_steps.pop(0)]

            to_run: list[tuple[dict[str, Any], str]] = []
            for step in ready_steps:
                tool_name = step.get("tool", "")
                skip, reason = _should_skip_tool(step, state=state, tool_results=tool_results)
                if skip:
                    tool_obj = tools_by_name.get(tool_name)
                    tool_description = getattr(tool_obj, "description", None) if tool_obj else None
                    msg = reason or "기존 증거가 충분해 생략한다."
                    skipped_tools.append(tool_name)
                    finished_parallel_tools.add(tool_name)
                    pending_events.append(
                        AgentEvent(
                            event_type="TOOL_SKIPPED",
                            node="execute",
                            phase="execute",
                            tool=tool_name,
                            message=msg,
                            thought=msg,
                            action="추가 specialist 호출을 생략한다.",
                            observation=msg,
                            metadata={"reason": reason, "owner": step.get("owner"), "tool_description": tool_description},
                        ).to_payload()
                    )
                    continue
                tool = tools_by_name.get(tool_name)
                if not tool:
                    failed_tools.append(tool_name)
                    finished_parallel_tools.add(tool_name)
                    tool_results.append({"tool": tool_name, "ok": False, "facts": {}, "summary": f"알 수 없는 도구: {tool_name}"})
                    continue
                tool_description = getattr(tool, "description", None) or ""
                step_reason = step.get("reason", "")
                short_msg = _TOOL_CALL_SHORT_MESSAGE.get(tool_name) or f"{step_reason} — {tool_name} 실행."
                pending_events.append(
                    AgentEvent(
                        event_type="TOOL_CALL",
                        node="execute",
                        phase="execute",
                        tool=tool_name,
                        message=short_msg,
                        thought=step_reason,
                        action=f"{tool_name} 실행",
                        observation="도구 실행 중.",
                        metadata={"reason": step_reason, "owner": step.get("owner"), "tool_description": tool_description},
                    ).to_payload()
                )
                to_run.append((step, tool_description))

            if to_run:
                parallel_results = await asyncio.gather(
                    *[_invoke_one(step, tool_results) for step, _ in to_run],
                    return_exceptions=False,
                )
                for (step, tool_description), result in zip(to_run, parallel_results):
                    tool_name = step.get("tool", "")
                    if not result.get("ok"):
                        failed_tools.append(tool_name)
                    tool_results.append(result)
                    finished_parallel_tools.add(tool_name)
                    pending_events.append(
                        AgentEvent(
                            event_type="TOOL_RESULT",
                            node="execute",
                            phase="execute",
                            tool=tool_name,
                            message=result.get("summary") or "도구 결과 수집 완료",
                            thought="수집한 사실을 다음 판단 단계에 반영한다.",
                            action=f"{tool_name} 결과 반영",
                            observation=result.get("summary") or "",
                            metadata={**result, "tool_description": tool_description},
                        ).to_payload()
                    )

            remaining_parallel_steps = blocked_steps
        for step in sequential_steps:
            tool_name = step.get("tool", "")
            skip, reason = _should_skip_tool(step, state=state, tool_results=tool_results)
            if skip:
                tool_obj = tools_by_name.get(tool_name)
                tool_description = getattr(tool_obj, "description", None) if tool_obj else None
                msg = reason or "기존 증거가 충분해 생략한다."
                skipped_tools.append(tool_name)
                pending_events.append(
                    AgentEvent(
                        event_type="TOOL_SKIPPED",
                        node="execute",
                        phase="execute",
                        tool=tool_name,
                        message=msg,
                        thought=msg,
                        action="추가 specialist 호출을 생략한다.",
                        observation=msg,
                        metadata={"reason": reason, "owner": step.get("owner"), "tool_description": tool_description},
                    ).to_payload()
                )
                continue
            tool = tools_by_name.get(tool_name)
            if not tool:
                failed_tools.append(tool_name)
                tool_results.append({"tool": tool_name, "ok": False, "facts": {}, "summary": f"알 수 없는 도구: {tool_name}"})
                continue
            tool_description = getattr(tool, "description", None) or ""
            step_reason = step.get("reason", "")
            short_msg = _TOOL_CALL_SHORT_MESSAGE.get(tool_name) or f"{step_reason} — {tool_name} 실행."
            pending_events.append(
                AgentEvent(
                    event_type="TOOL_CALL",
                    node="execute",
                    phase="execute",
                    tool=tool_name,
                    message=short_msg,
                    thought=step_reason,
                    action=f"{tool_name} 실행",
                    observation="도구 실행 중.",
                    metadata={"reason": step_reason, "owner": step.get("owner"), "tool_description": tool_description},
                ).to_payload()
            )
            result = await _invoke_one(step, tool_results)
            if not result.get("ok"):
                failed_tools.append(tool_name)
            tool_results.append(result)
            pending_events.append(
                AgentEvent(
                    event_type="TOOL_RESULT",
                    node="execute",
                    phase="execute",
                    tool=tool_name,
                    message=result.get("summary") or "도구 결과 수집 완료",
                    thought="수집한 사실을 다음 판단 단계에 반영한다.",
                    action=f"{tool_name} 결과 반영",
                    observation=result.get("summary") or "",
                    metadata={**result, "tool_description": tool_description},
                ).to_payload()
            )
    else:
        for step in plan:
            tool_name = step.get("tool", "")
            skip, reason = _should_skip_tool(step, state=state, tool_results=tool_results)
            if skip:
                tool_obj = tools_by_name.get(tool_name)
                tool_description = getattr(tool_obj, "description", None) if tool_obj else None
                msg = reason or "기존 증거가 충분해 생략한다."
                skipped_tools.append(tool_name)
                pending_events.append(
                    AgentEvent(
                        event_type="TOOL_SKIPPED",
                        node="execute",
                        phase="execute",
                        tool=tool_name,
                        message=msg,
                        thought=msg,
                        action="추가 specialist 호출을 생략한다.",
                        observation=msg,
                        metadata={"reason": reason, "owner": step.get("owner"), "tool_description": tool_description},
                    ).to_payload()
                )
                continue
            tool = tools_by_name.get(tool_name)
            if not tool:
                failed_tools.append(tool_name)
                tool_results.append({"tool": tool_name, "ok": False, "facts": {}, "summary": f"알 수 없는 도구: {tool_name}"})
                continue
            tool_description = getattr(tool, "description", None) or ""
            step_reason = step.get("reason", "")
            short_msg = _TOOL_CALL_SHORT_MESSAGE.get(tool_name) or f"{step_reason} — {tool_name} 실행."
            pending_events.append(
                AgentEvent(
                    event_type="TOOL_CALL",
                    node="execute",
                    phase="execute",
                    tool=tool_name,
                    message=short_msg,
                    thought=step_reason,
                    action=f"{tool_name} 실행",
                    observation="도구 실행 중.",
                    metadata={"reason": step_reason, "owner": step.get("owner"), "tool_description": tool_description},
                ).to_payload()
            )
            inp = ToolContextInput(
                case_id=state["case_id"],
                body_evidence=state["body_evidence"],
                intended_risk_type=state.get("intended_risk_type"),
                prior_tool_results=list(tool_results),
            )
            result = await tool.ainvoke(inp.model_dump())
            if not isinstance(result, dict):
                result = {"tool": tool_name, "ok": False, "facts": {}, "summary": str(result)}
            if not bool(result.get("ok")):
                failed_tools.append(tool_name)
            tool_results.append(result)
            result_summary = result.get("summary") or "도구 결과 수집 완료"
            pending_events.append(
                AgentEvent(
                    event_type="TOOL_RESULT",
                    node="execute",
                    phase="execute",
                    tool=tool_name,
                    message=result_summary,
                    thought="수집한 사실을 다음 판단 단계에 반영한다.",
                    action=f"{tool_name} 결과 반영",
                    observation=result_summary,
                    metadata={**result, "tool_description": tool_description},
                ).to_payload()
            )
    score = _score(state["flags"], tool_results)
    trace = score.get("calculation_trace", "")
    pending_events.append({
        "event_type": "SCORE_BREAKDOWN",
        "message": (
            f"정책점수 {score['policy_score']}점 / 근거점수 {score['evidence_score']}점 / "
            f"최종 {score['final_score']}점 [{score.get('severity', '-')}] — {trace}"
        ),
        "node": "execute",
        "phase": "execute",
        "metadata": score,
    })
    executed_tools = [_tool_result_key(r) for r in tool_results if _tool_result_key(r)]
    reasoning_parts = [
        f"{len(executed_tools)}개 도구를 실행해 정책점수 {score['policy_score']}점, 근거점수 {score['evidence_score']}점을 산출했다.",
        "수집된 도구 결과를 critic 단계의 반박 가능성 검토 입력으로 전달한다.",
    ]
    if skipped_tools:
        reasoning_parts.append(f"생략 도구: {', '.join(skipped_tools)}.")
    if failed_tools:
        reasoning_parts.append(f"실패 도구: {', '.join(sorted(set(failed_tools)))}.")
    execute_reasoning = " ".join(reasoning_parts).strip()
    execute_context = {
        "executed_tools": executed_tools,
        "skipped_tools": skipped_tools,
        "failed_tools": sorted(set(failed_tools)),
        "score": score,
        "last_node_summary": state.get("last_node_summary", "없음"),
    }
    execute_reasoning, execute_reasoning_events, note_source = await _stream_reasoning_events_with_llm(
        "execute",
        execute_reasoning,
        context=execute_context,
    )
    pending_events.extend(execute_reasoning_events)
    execute_output = ExecuteOutput(
        executed_tools=executed_tools,
        skipped_tools=skipped_tools,
        failed_tools=sorted(set(failed_tools)),
        policy_score=int(score.get("policy_score") or 0),
        evidence_score=int(score.get("evidence_score") or 0),
        final_score=int(score.get("final_score") or 0),
        reasoning=execute_reasoning,
    )
    pending_events.append(
        AgentEvent(
            event_type="NODE_END",
            node="execute",
            phase="execute",
            message="도구 실행과 점수 산출이 완료되었습니다.",
            metadata={"executed_tools": len(tool_results), "reasoning": execute_reasoning, "note_source": note_source},
        ).to_payload()
    )
    plan_achievement = _compute_plan_achievement(state.get("plan") or [], tool_results)
    return {
        "tool_results": tool_results,
        "score_breakdown": score,
        "execute_output": execute_output.model_dump(),
        "pending_events": pending_events,
        "plan_achievement": plan_achievement,
        "last_node_summary": f"execute 완료: {execute_reasoning[:60]}…" if len(execute_reasoning) > 60 else f"execute 완료: {execute_reasoning}",
    }


def _build_verification_targets(state: AgentState) -> list[str]:
    """
    Verifier가 검증할 구체적·반박 가능한 주장 문장 최대 4개 생성.

    설계 원칙:
    1) 전표 사실(시간·금액·근태·MCC)을 주장에 직접 삽입
    2) 특정 조항 번호(제XX조 ③항)까지 명시
    3) "적용될 수 있음" 대신 "해당한다 / 위반 가능성" 수준의 주장
    4) _chunk_supports_claim()이 단순 단어 중복만으로 통과하지 못하도록 충분히 구체화
    5) tool_results의 실제 facts 값을 반드시 참조
    """
    body = state["body_evidence"]
    flags = state.get("flags") or {}
    tool_results = state.get("tool_results") or []

    occurred_at = str(body.get("occurredAt") or "")
    date_part = occurred_at[:10] if len(occurred_at) >= 10 else "날짜 미상"
    time_part = occurred_at[11:16] if len(occurred_at) >= 16 else ""
    amount = body.get("amount")
    amount_str = f"{int(amount):,}원" if amount else "금액 미상"
    merchant = body.get("merchantName") or "거래처 미상"
    mcc_code = body.get("mccCode") or flags.get("mccCode") or ""
    mcc_name = body.get("mccName") or ""
    hr_status = str(flags.get("hrStatus") or body.get("hrStatus") or "").upper()
    is_holiday = bool(flags.get("isHoliday") or body.get("isHoliday"))
    is_night = bool(flags.get("isNight"))
    budget_exceeded = bool(flags.get("budgetExceeded"))

    holiday_facts = (_find_tool_result(tool_results, "holiday_compliance_probe") or {}).get("facts") or {}
    merchant_facts = (_find_tool_result(tool_results, "merchant_risk_probe") or {}).get("facts") or {}
    policy_facts = (_find_tool_result(tool_results, "policy_rulebook_probe") or {}).get("facts") or {}

    merchant_risk = str(merchant_facts.get("merchantRisk") or "").upper()
    holiday_risk = bool(holiday_facts.get("holidayRisk"))
    policy_refs = policy_facts.get("policy_refs") or []

    claims: list[tuple[int, str]] = []

    if is_night and time_part:
        claims.append((
            _CLAIM_PRIORITY["night_violation"],
            f"{date_part} {time_part} 심야 시간대에 {merchant}에서 {amount_str} 결제가 발생하여 "
            f"제23조 ③-1항 '23:00~06:00 심야 식대 경고 대상' 및 "
            f"제38조 ②항 '심야 시간대 지출 검토 대상'에 해당한다.",
        ))

    if is_holiday and hr_status in {"LEAVE", "OFF", "VACATION"}:
        hr_label = {"LEAVE": "휴가·결근", "OFF": "휴무", "VACATION": "휴가"}.get(hr_status, hr_status)
        claims.append((
            _CLAIM_PRIORITY["holiday_hr_conflict"],
            f"결제일({date_part}) 근태 상태 {hr_status}({hr_label}) 및 휴일 결제가 동시에 확인되어 "
            f"제39조 ①항 주말·공휴일 지출 제한과 "
            f"제23조 ③-2항 '주말/공휴일 식대(예외 승인 없는 경우)' 경고 조건 모두에 해당한다.",
        ))
    elif (is_holiday or holiday_risk) and not hr_status:
        claims.append((
            _CLAIM_PRIORITY["holiday_hr_conflict"] - 1,
            f"결제일({date_part})이 휴일로 확인되나 근태 상태 데이터가 누락되어 "
            f"제39조 주말·공휴일 지출 제한 적용 여부 완전 판단이 불가하다. 근태 보완 후 재검토 필요.",
        ))

    if merchant_risk in {"HIGH", "CRITICAL"} and mcc_code:
        mcc_display = f"MCC {mcc_code}({mcc_name})" if mcc_name else f"MCC {mcc_code}"
        compound = "복합 위험" if merchant_risk == "CRITICAL" else "고위험"
        claims.append((
            _CLAIM_PRIORITY["merchant_high_risk"],
            f"{merchant}({mcc_display})은 제42조 {compound} 업종으로 분류되어 "
            f"금액과 무관하게 강화 승인 대상이며, 제11조 ③항 고위험 업종 거래 강화 승인 조건을 충족한다.",
        ))
    elif merchant_risk == "MEDIUM" and mcc_code:
        claims.append((
            _CLAIM_PRIORITY["merchant_high_risk"] - 2,
            f"{merchant}(MCC {mcc_code}) 업종 위험도 MEDIUM으로 제42조 업종 제한 기준 검토 대상이다.",
        ))

    if budget_exceeded:
        claims.append((
            _CLAIM_PRIORITY["budget_exceeded"],
            f"{amount_str} 결제가 예산 한도를 초과하여 제40조 ①항 금액·누적한도 제약 및 "
            f"제19조 ①항 예산 초과 처리 기준에 따른 상위 승인이 필요하다.",
        ))

    if amount and not budget_exceeded:
        if amount >= 2_000_000:
            claims.append((
                _CLAIM_PRIORITY["amount_approval_tier"],
                f"{amount_str}은 제11조 ②-4항 임원·CFO 승인 구간(200만원 초과)에 해당하며 "
                f"증빙 완결성과 결재권자 확인이 필수이다.",
            ))
        elif amount >= 500_000:
            claims.append((
                _CLAIM_PRIORITY["amount_approval_tier"],
                f"{amount_str}은 제11조 ②-3항 본부장 승인 구간(50만~200만원)에 해당한다.",
            ))
        elif amount >= 100_000:
            claims.append((
                _CLAIM_PRIORITY["amount_approval_tier"] - 1,
                f"{amount_str}은 제11조 ②-2항 부서장 승인 구간(10만~50만원)에 해당한다.",
            ))

    for ref in policy_refs[:2]:
        article = ref.get("article") or ""
        parent_title = (ref.get("parent_title") or "")[:35]
        reason = ref.get("adoption_reason") or ""
        if article:
            reason_part = f" ({reason})" if reason else ""
            claims.append((
                _CLAIM_PRIORITY["policy_ref_direct"],
                f"policy_rulebook_probe 채택 조항 {article}({parent_title}){reason_part}이 "
                f"{merchant} {amount_str} 전표에 직접 적용 가능한 위반 근거를 갖는다.",
            ))

    claims.sort(key=lambda item: item[0], reverse=True)
    if claims:
        return [text for _, text in claims[:4]]

    return [
        f"{merchant} {amount_str} 전표({date_part})가 사내 경비 지출 관리 규정 위반 여부 "
        f"검토 대상으로 판정되었으며 세부 조항 적용 근거 확인이 필요하다."
    ]


async def critic_node(state: AgentState) -> AgentState:
    legacy = next((r for r in state.get("tool_results", []) if _tool_result_key(r) == "legacy_aura_deep_audit"), None)
    missing = ((state["body_evidence"].get("dataQuality") or {}).get("missingFields") or [])
    score = state.get("score_breakdown") or {}
    execute_out = state.get("execute_output") or {}
    tool_results = state.get("tool_results") or []
    failed_tools = execute_out.get("failed_tools") or []
    evidence_score = int(score.get("evidence_score") or 0)
    final_score = int(score.get("final_score") or 0)
    high_risk_compound = bool(state["flags"].get("isHoliday")) and bool(state["flags"].get("mccCode"))
    critical_tool_failed = "policy_rulebook_probe" in failed_tools or "document_evidence_probe" in failed_tools
    tool_failure_rate = len(failed_tools) / max(len(tool_results), 1)
    borderline_score = 48 <= final_score <= 62
    weak_evidence_with_risk = high_risk_compound and evidence_score < 30

    replan_reasons: list[str] = []
    if missing:
        replan_reasons.append(f"누락 필드 {missing} — 과잉 주장 위험")
    if critical_tool_failed:
        replan_reasons.append(
            f"핵심 도구 실패: {[t for t in failed_tools if t in {'policy_rulebook_probe', 'document_evidence_probe'}]}"
        )
    if tool_failure_rate >= 0.5 and len(tool_results) >= 2:
        replan_reasons.append(f"도구 실패율 {tool_failure_rate:.0%} — 증거 신뢰성 저하")
    if borderline_score:
        replan_reasons.append(f"최종점수 {final_score}점이 MEDIUM/HIGH 경계 ±7점 이내 — 추가 증거 필요")
    if weak_evidence_with_risk:
        replan_reasons.append(f"복합 위험(휴일+MCC) 케이스인데 evidence_score={evidence_score} (30점 미만)")

    loop_count = state.get("critic_loop_count") or 0
    replan_required = bool(
        replan_reasons
        and not state["flags"].get("hasHitlResponse")
        and loop_count < _MAX_CRITIC_LOOP
    )
    replan_reason = " | ".join(replan_reasons) if replan_reasons else ""
    critique = {
        "has_legacy_result": bool(legacy and legacy.get("facts")),
        "missing_fields": missing,
        "risk_of_overclaim": bool(missing) or bool(replan_reasons),
        "recommend_hold": bool(replan_required or (missing and not state["flags"].get("hasHitlResponse"))),
    }
    replan_context: dict[str, Any] | None = None
    if replan_required:
        replan_context = {
            "critic_feedback": replan_reason,
            "missing_fields": missing,
            "loop_count": loop_count + 1,
            "previous_tool_results": [_tool_result_key(r) for r in state.get("tool_results", [])],
        }
    verification_targets = _build_verification_targets(state)
    critic_output = CriticOutput(
        overclaim_risk=critique["risk_of_overclaim"],
        contradictions=[],
        missing_counter_evidence=missing,
        recommend_hold=critique["recommend_hold"],
        rationale=replan_reason[:300] if replan_reason else ("입력 누락 필드가 있으면 과잉 주장 위험이 있어 보류를 권고한다." if missing else "추가 보류 조건 없이 진행 가능하다."),
        has_legacy_result=critique["has_legacy_result"],
        verification_targets=verification_targets,
        replan_required=replan_required,
        replan_reason=replan_reason,
    )
    rationale = critic_output.rationale
    reasoning_parts = [rationale]
    if missing:
        reasoning_parts.append(f"누락 필드: {', '.join(missing[:5])}. 과잉 주장 위험이 있어 보류를 권고한다.")
    if replan_required:
        reasoning_parts.append(replan_reason or "")
    reasoning_text = " ".join(reasoning_parts).strip()
    # v3 정합성 검증 + 1회 보정
    reasoning_text, check, retried = call_node_llm_with_consistency_check("critic", critic_output.model_dump(), reasoning_text, max_retries=1)
    critic_context = {
        "missing_fields": missing,
        "recommend_hold": critique.get("recommend_hold"),
        "replan_required": replan_required,
        "last_node_summary": state.get("last_node_summary", "없음"),
    }
    reasoning_text, reasoning_events, note_source = await _stream_reasoning_events_with_llm("critic", reasoning_text, context=critic_context)
    critic_output_dict = critic_output.model_dump()
    critic_output_dict["reasoning"] = reasoning_text
    last_node_summary = f"critic 완료: {reasoning_text[:60]}…" if len(reasoning_text) > 60 else f"critic 완료: {reasoning_text}"
    pending: list[dict[str, Any]] = [
        AgentEvent(event_type="NODE_START", node="critic", phase="reflect", message="전문 도구 결과와 입력 품질을 교차 검토합니다.", metadata={}).to_payload(),
    ]
    if retried:
        pending.append(
            AgentEvent(
                event_type="THINKING_RETRY",
                node="critic",
                phase="reflect",
                message="추론 정합성 불일치 감지 — 재검토 후 문구를 보정합니다.",
                metadata={"conflict": check.conflict_description},
            ).to_payload()
        )
    pending.extend(reasoning_events)
    pending.append(
        AgentEvent(
            event_type="NODE_END",
            node="critic",
            phase="reflect",
            message="비판적 재검토가 완료되었습니다.",
            metadata={"reasoning": reasoning_text, "note_source": note_source, **critique},
        ).to_payload(),
    )
    return {
        "critique": critique,
        "critic_output": critic_output_dict,
        "critic_loop_count": loop_count + 1 if replan_required else loop_count,
        "replan_context": replan_context,
        "last_node_summary": last_node_summary,
        "pending_events": pending,
    }


async def _derive_hitl_from_regulation(state: AgentState) -> dict[str, Any]:
    """
    규정 본문(chunk_text)을 바탕으로 에이전트가 필수 입력/증빙과 검토 질문을 추출한다.
    하드코딩된 케이스별 규칙이 아니라, 적용 규정의 '필수 입력/증빙' 등 문구를 읽어 HITL 요청 내용을 만든다.
    """
    refs = (_find_tool_result(state.get("tool_results", []), "policy_rulebook_probe") or {}).get("facts", {}).get("policy_refs") or []
    body = state.get("body_evidence") or {}
    regulation_texts: list[str] = []
    for ref in refs[:5]:
        chunk_text = (ref.get("chunk_text") or "").strip()
        article = ref.get("article") or ref.get("regulation_article") or ""
        parent_title = ref.get("parent_title") or ""
        if chunk_text:
            regulation_texts.append(f"[{article} {parent_title}]\n{chunk_text}")
    if not regulation_texts:
        return {}

    case_summary = (
        f"발생시각: {body.get('occurredAt')} / 가맹점: {body.get('merchantName')} / "
        f"휴일여부: {body.get('isHoliday')} / 근태: {body.get('hrStatus')} / 예산초과: {body.get('budgetExceeded')}"
    )
    system_prompt = (
        "당신은 경비 규정을 적용하는 감사 에이전트다. 아래 '적용 규정 조문'에 적힌 내용만을 근거로, "
        "담당자 검토(HITL) 시 요구할 **필수 입력/증빙** 항목과 **검토 시 확인할 질문**을 추출하라.\n"
        "규칙:\n"
        "1. 규정에 '필수 입력', '필수 증빙', '② 필수' 등으로 열거된 항목을 required_inputs로 나열하라. "
        "각 항목은 {\"field\": \"영문식별자\", \"reason\": \"규정에서 요구하는 이유 한 줄\", \"guide\": \"사용자에게 보여줄 가이드 문구\"} 형태로.\n"
        "2. 규정에서 예외·승인·검토 시 확인하라고 한 내용을 review_questions로 짧은 질문 문장으로 나열하라. "
        "질문은 최대 3개만. 유사·중복 질문은 제외하고, 판단에 가장 중요한 핵심만 선별하라.\n"
        "3. 현재 케이스(휴일/심야/접대 등)에 실제로 해당하는 조문만 사용하라. 해당 없으면 빈 배열을 반환하라.\n"
        "4. 반드시 JSON만 응답하라: {\"required_inputs\": [...], \"review_questions\": [...]}\n"
    )
    user_prompt = f"현재 케이스 요약: {case_summary}\n\n적용 규정 조문:\n\n" + "\n\n---\n\n".join(regulation_texts)

    if not getattr(settings, "openai_api_key", None):
        return {}
    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI

        base_url = (getattr(settings, "openai_base_url") or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client = AsyncAzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_ep,
                api_version=getattr(settings, "openai_api_version", "2024-02-15-preview"),
            )
        else:
            client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base_url or None)

        tok_kw = {"max_completion_tokens": 1500} if is_azure else {"max_tokens": 1500}
        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                **tok_kw,
            ),
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        required_inputs = parsed.get("required_inputs") or []
        review_questions = parsed.get("review_questions") or []
        if not isinstance(required_inputs, list):
            required_inputs = []
        if not isinstance(review_questions, list):
            review_questions = []
        required_inputs = [
            {"field": str(x.get("field", "")), "reason": str(x.get("reason", "")), "guide": str(x.get("guide", ""))}
            for x in required_inputs if isinstance(x, dict)
        ]
        review_questions = [str(q).strip() for q in review_questions if str(q).strip()][:3]
        return {"required_inputs": required_inputs, "review_questions": review_questions}
    except Exception:
        return {}


async def _generate_hitl_review_content(
    hitl_request: dict[str, Any],
    verification_summary: dict[str, Any],
    claim_results: list[dict[str, Any]],
    reasoning_text: str,
) -> dict[str, Any]:
    """
    담당자 검토가 필요하다고 판단된 맥락을 바탕으로, LLM이 검토 필요 사유와 검토자가 답해야 할 질문을 생성한다.
    반환: {"review_reasons": list[str], "review_questions": list[str]} (각 1개 이상 보장)
    """
    why = (hitl_request.get("why_hitl") or "").strip()
    blockers = hitl_request.get("reasons") or hitl_request.get("auto_finalize_blockers") or []
    missing = hitl_request.get("missing_citations") or []
    covered = verification_summary.get("covered")
    total = verification_summary.get("total")
    coverage_note = f"검증 대상 {total}개 중 {covered}개만 규정 근거와 연결됨." if (total and total > 0) else ""

    claim_lines: list[str] = []
    for r in (claim_results or [])[:6]:
        c = r.get("claim") or ""
        cov = r.get("covered")
        gap = (r.get("gap") or "").strip()
        if c:
            claim_lines.append(f"- {c[:120]}{'…' if len(c) > 120 else ''} | 연결: {'예' if cov else '아니오'}{f' | 부족: {gap}' if gap else ''}")

    system_prompt = (
        "당신은 경비 감사 에이전트다. 이 전표는 담당자 검토(HITL)가 필요한 것으로 판정되었다. "
        "아래 맥락(분석 과정에서 나온 근거)만을 사용하여 다음 두 가지를 반드시 생성하라.\n"
        "1. review_reasons: 검토가 필요한 이유를 담당자가 이해할 수 있는 문장 1~5개. 분석 결과 기반으로 명확히.\n"
        "2. review_questions: 검토자가 검토의견에 답해야 할 질문을 2~3개만 작성하라. 중복·유사 질문 없이, 판단에 가장 중요한 핵심만 선별. 분석 결과와 연결된 구체적 질문으로. 예: '휴일 사용 사전 승인 여부를 확인했는가?'\n"
        "반드시 JSON만 응답: {\"review_reasons\": [\"...\", ...], \"review_questions\": [\"...\", ...]}\n"
        "review_reasons는 최소 1개, review_questions는 2~3개(최대 3개) 필수."
    )
    user_parts = [f"검증 판단 요약: {reasoning_text[:500]}" if reasoning_text else ""]
    if why:
        user_parts.append(f"자동 확정 중단 이유: {why}")
    if blockers:
        user_parts.append("자동 확정 차단 사유: " + "; ".join(str(b) for b in blockers[:5]))
    if coverage_note:
        user_parts.append(coverage_note)
    if missing:
        user_parts.append("근거 미연결 주장: " + " | ".join((m or "")[:80] for m in missing[:3]))
    if claim_lines:
        user_parts.append("주장별 검증 결과:\n" + "\n".join(claim_lines))
    user_prompt = "\n\n".join(p for p in user_parts if p).strip() or "검토 필요로 판정됨. 사유와 질문을 생성하라."

    # 1) 분석 결과 기반 기본안(항상 생성)
    base_reasons: list[str] = []
    base_questions: list[str] = []
    for s in (hitl_request.get("unresolved_claims") or []):
        t = str(s or "").strip()
        if t:
            base_reasons.append(t)
    if why:
        base_reasons.append(why)
    for s in (hitl_request.get("review_questions") or hitl_request.get("questions") or []):
        t = str(s or "").strip()
        if t:
            base_questions.append(t)
    for req in (hitl_request.get("required_inputs") or []):
        q = str(req.get("guide") or req.get("reason") or "").strip()
        if q:
            base_questions.append(q)
    for r in (claim_results or [])[:4]:
        claim = str(r.get("claim") or "").strip()
        covered_flag = bool(r.get("covered"))
        gap = str(r.get("gap") or "").strip()
        if not covered_flag and claim:
            if gap:
                base_reasons.append(f"미검증 주장: {claim[:120]}{'…' if len(claim) > 120 else ''} ({gap[:100]})")
            else:
                base_reasons.append(f"미검증 주장: {claim[:120]}{'…' if len(claim) > 120 else ''}")
            base_questions.append(f"다음 주장에 대한 근거를 확인할 수 있는가: {claim[:110]}{'…' if len(claim) > 110 else ''}")
    # 중복 제거
    dedup_reasons: list[str] = []
    seen_r: set[str] = set()
    for s in base_reasons:
        k = s.strip()
        if not k or k in seen_r:
            continue
        seen_r.add(k)
        dedup_reasons.append(k)
    dedup_questions: list[str] = []
    seen_q: set[str] = set()
    for s in base_questions:
        k = s.strip()
        if not k or k in seen_q:
            continue
        seen_q.add(k)
        dedup_questions.append(k)
    if not dedup_reasons:
        dedup_reasons = ["자동 판정을 보류한 근거를 담당자 확인이 필요합니다."]
    if not dedup_questions:
        if why:
            dedup_questions = [f"자동 판정 보류 사유를 해소할 근거를 확인할 수 있는가: {why[:180]}{'…' if len(why) > 180 else ''}"]
        else:
            dedup_questions = ["검토 보류 사유를 해소할 추가 근거를 제출할 수 있는가?"]

    baseline = {"review_reasons": dedup_reasons[:5], "review_questions": dedup_questions[:3]}

    if not getattr(settings, "openai_api_key", None):
        return baseline
    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI

        base_url = (getattr(settings, "openai_base_url") or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client = AsyncAzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_ep,
                api_version=getattr(settings, "openai_api_version", "2024-02-15-preview"),
            )
        else:
            client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base_url or None)

        tok_kw = {"max_completion_tokens": 1200} if is_azure else {"max_tokens": 1200}
        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                **tok_kw,
            ),
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        reasons = parsed.get("review_reasons") or []
        questions = parsed.get("review_questions") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)] if reasons else []
        if not isinstance(questions, list):
            questions = [str(questions)] if questions else []
        reasons = [str(s).strip() for s in reasons if str(s).strip()]
        questions = [str(q).strip() for q in questions if str(q).strip()]
        # 2) LLM 결과가 비거나 일부만 있으면 baseline으로 보강(실패/빈값 방지)
        merged_reasons = reasons + [s for s in baseline["review_reasons"] if s not in set(reasons)]
        merged_questions = questions + [s for s in baseline["review_questions"] if s not in set(questions)]
        return {"review_reasons": merged_reasons[:5], "review_questions": merged_questions[:3]}
    except Exception:
        return baseline


async def _retry_fill_hitl_review_when_empty(
    hitl_request: dict[str, Any],
    verification_summary: dict[str, Any],
    claim_results: list[dict[str, Any]],
    reasoning_text: str,
    *,
    empty_reasons: bool,
    empty_questions: bool,
) -> dict[str, Any]:
    """
    검토 필요로 판정했는데 검토 필요 사유 또는 검토 시 답해야 할 질문이 비어 있을 때,
    LLM에게 분석 결과를 바탕으로 반드시 두 항목을 채우라고 재지시한다.
    """
    why = (hitl_request.get("why_hitl") or "").strip()
    blockers = hitl_request.get("reasons") or hitl_request.get("auto_finalize_blockers") or []
    covered = verification_summary.get("covered")
    total = verification_summary.get("total")
    coverage_note = f"검증 대상 {total}개 중 {covered}개만 규정 근거와 연결됨." if (total and total > 0) else ""

    claim_lines: list[str] = []
    for r in (claim_results or [])[:6]:
        c = r.get("claim") or ""
        cov = r.get("covered")
        gap = (r.get("gap") or "").strip()
        if c:
            claim_lines.append(f"- {c[:120]}{'…' if len(c) > 120 else ''} | 연결: {'예' if cov else '아니오'}{f' | 부족: {gap}' if gap else ''}")

    missing_what = []
    if empty_reasons:
        missing_what.append("검토 필요 사유")
    if empty_questions:
        missing_what.append("검토 시 답해야 할 질문")
    missing_str = ", ".join(missing_what)

    system_prompt = (
        "당신은 경비 감사 에이전트다. 이 전표는 '검토 필요'로 이미 판정된 건이다. "
        f"그런데 현재 {missing_str} 항목이 비어 있다. "
        "아래 분석 결과(검증 판단 요약, 자동 확정 차단 사유, 주장별 검증 결과 등)를 **근거**로 다음을 반드시 수행하라.\n"
        "1. review_reasons: 검토가 필요한 이유를 담당자가 이해할 수 있는 문장 1~5개. 분석 과정에서 나온 근거 기반으로 작성.\n"
        "2. review_questions: 검토자가 검토의견에 답해야 할 질문을 2~3개만 작성하라. 중복·유사 없이 핵심만. 분석 결과와 연결된 구체적 질문으로.\n"
        "3. empty_explanation: (선택) 위 두 항목이 비어 있었을 수 있는 이유를 한 줄로.\n"
        "반드시 JSON만 응답: {\"review_reasons\": [\"...\"], \"review_questions\": [\"...\"], \"empty_explanation\": \"...\"}\n"
        "review_reasons는 최소 1개, review_questions는 2~3개(최대 3개) 필수."
    )
    user_parts = [f"검증 판단 요약: {reasoning_text[:600]}" if reasoning_text else ""]
    if why:
        user_parts.append(f"자동 확정 중단 이유: {why}")
    if blockers:
        user_parts.append("자동 확정 차단 사유: " + "; ".join(str(b) for b in blockers[:5]))
    if coverage_note:
        user_parts.append(coverage_note)
    if claim_lines:
        user_parts.append("주장별 검증 결과:\n" + "\n".join(claim_lines))
    user_prompt = "\n\n".join(p for p in user_parts if p).strip() or "분석 결과를 바탕으로 검토 필요 사유와 검토 시 답해야 할 질문을 생성하라."

    if not getattr(settings, "openai_api_key", None):
        return {"review_reasons": [], "review_questions": []}
    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI

        base_url = (getattr(settings, "openai_base_url") or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client = AsyncAzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_ep,
                api_version=getattr(settings, "openai_api_version", "2024-02-15-preview"),
            )
        else:
            client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base_url or None)

        tok_kw = {"max_completion_tokens": 1200} if is_azure else {"max_tokens": 1200}
        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                **tok_kw,
            ),
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        reasons = parsed.get("review_reasons") or []
        questions = parsed.get("review_questions") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)] if reasons else []
        if not isinstance(questions, list):
            questions = [str(questions)] if questions else []
        reasons = [str(s).strip() for s in reasons if str(s).strip()][:5]
        questions = [str(q).strip() for q in questions if str(q).strip()][:3]
        return {"review_reasons": reasons, "review_questions": questions}
    except Exception:
        return {"review_reasons": [], "review_questions": []}


async def verify_node(state: AgentState) -> AgentState:
    from services.evidence_verification import (
        EVIDENCE_GATE_HOLD,
        EVIDENCE_GATE_REGENERATE,
        get_dynamic_coverage_thresholds,
        verify_evidence_coverage_claims,
    )

    verification = {"needs_hitl": False, "quality_signals": ["OK"]}
    verification_targets = (state.get("critic_output") or {}).get("verification_targets") or []
    probe_facts = (_find_tool_result(state["tool_results"], "policy_rulebook_probe") or {}).get("facts", {}) or {}
    retrieved_chunks = probe_facts.get("retrieval_candidates") or probe_facts.get("policy_refs") or []
    verification_summary: dict[str, Any] = {}
    score_bd = state.get("score_breakdown") or {}
    severity = score_bd.get("severity", "MEDIUM")
    final_score = float(score_bd.get("final_score") or 0)
    compound_multiplier = float(score_bd.get("compound_multiplier") or 1.0)
    hold_threshold, caution_threshold = get_dynamic_coverage_thresholds(
        severity=severity,
        final_score=final_score,
        compound_multiplier=compound_multiplier,
    )
    if verification_targets and retrieved_chunks:
        verification_summary = verify_evidence_coverage_claims(
            verification_targets,
            retrieved_chunks,
            threshold_hold=hold_threshold,
            threshold_caution=caution_threshold,
        )
    elif verification_targets:
        verification_summary = {"covered": 0, "total": len(verification_targets), "coverage_ratio": 0.0, "details": [], "gate_policy": EVIDENCE_GATE_HOLD, "missing_citations": verification_targets}
    verification["verification_summary"] = verification_summary
    regulation_driven = await _derive_hitl_from_regulation(state)
    hitl_request = build_hitl_request(
        state["body_evidence"],
        state["tool_results"],
        critique=state.get("critique"),
        verification_summary=verification_summary,
        screening_result=state.get("screening_result"),
        score_breakdown=state.get("score_breakdown"),
        regulation_driven=regulation_driven,
    )
    if state["flags"].get("hasHitlResponse"):
        hitl_request = None
    # 분석 결과가 위험/규정 위배이면 항상 hitl_request를 세우고, 라우팅에서 담당자 검토로 보냄.
    # _enable_hitl(체크박스)은 라우팅에서만 '건너뛰기' 시 사용하며, verify 단계에서는 무시함.
    needs_hitl = bool(hitl_request)
    verification["needs_hitl"] = needs_hitl
    verification["quality_signals"] = ["HITL_REQUIRED"] if needs_hitl else ["OK"]
    gate = VerifierGate.HITL_REQUIRED if needs_hitl else VerifierGate.READY

    stop_words = {
        "이", "가", "을", "를", "의", "에", "으로", "로", "와", "과",
        "이다", "있음", "있다", "수", "하며", "하여", "해당", "필요",
        "대상", "조항", "기준", "한다", "되어", "위반", "가능성",
    }
    claim_results: list[ClaimVerificationResult] = []
    for detail in (verification_summary.get("details") or []):
        idx = detail.get("index", 0)
        claim_text = verification_targets[idx] if idx < len(verification_targets) else ""
        is_covered = bool(detail.get("covered"))

        supporting: list[str] = []
        if is_covered and retrieved_chunks:
            claim_words = {word for word in _WORD_RE.findall(claim_text.lower()) if word not in stop_words}
            for chunk in retrieved_chunks:
                chunk_combined = " ".join([
                    str(chunk.get("chunk_text") or ""),
                    str(chunk.get("parent_title") or ""),
                    str(chunk.get("article") or chunk.get("regulation_article") or ""),
                ])
                chunk_words = {word for word in _WORD_RE.findall(chunk_combined.lower()) if word not in stop_words}
                if len(claim_words & chunk_words) >= 3:
                    article = chunk.get("article") or chunk.get("regulation_article")
                    if article and article not in supporting:
                        supporting.append(article)

        gap_text = ""
        if not is_covered:
            if "심야" in claim_text or "23:00" in claim_text:
                gap_text = "심야 시간대 규정 조항(제23조/제38조)이 retrieval 결과에 포함되지 않음"
            elif "LEAVE" in claim_text or "휴일" in claim_text or "근태" in claim_text:
                gap_text = "근태·휴일 지출 연계 규정 청크 부족"
            elif "MCC" in claim_text or "업종" in claim_text:
                gap_text = "고위험 업종 관련 조항(제42조)이 retrieval 결과에 부재"
            elif "예산" in claim_text:
                gap_text = "예산 초과 관련 조항(제40조/제19조) 청크 미확보"
            else:
                gap_text = "해당 주장을 뒷받침할 규정 청크를 retrieval에서 찾지 못함"

        claim_results.append(ClaimVerificationResult(
            claim=claim_text,
            covered=is_covered,
            supporting_articles=supporting[:3],
            gap=gap_text,
        ))

    rationale = hitl_request.get("why_hitl") if hitl_request else "자동 확정 가능한 상태로 검증이 완료되었습니다."
    verifier_output = VerifierOutput(
        grounded=not needs_hitl,
        needs_hitl=needs_hitl,
        missing_evidence=(hitl_request.get("missing_evidence") or hitl_request.get("reasons") or []) if hitl_request else [],
        gate=gate,
        rationale=rationale,
        quality_signals=verification["quality_signals"],
        claim_results=claim_results,
    )
    reasoning_parts = [rationale]
    reasoning_parts.append("담당자 검토 필요" if needs_hitl else "자동 진행 가능")
    reasoning_text = " ".join(reasoning_parts).strip()
    # v3 정합성 검증 + 1회 보정
    reasoning_text, check, retried = call_node_llm_with_consistency_check("verify", verifier_output.model_dump(), reasoning_text, max_retries=1)
    verify_context = {
        "gate_result": gate.value,
        "needs_hitl": needs_hitl,
        "verification_targets": verification_targets,
        "last_node_summary": state.get("last_node_summary", "없음"),
    }
    reasoning_text, reasoning_events, note_source = await _stream_reasoning_events_with_llm("verify", reasoning_text, context=verify_context)
    verifier_output_dict = verifier_output.model_dump()
    verifier_output_dict["reasoning"] = reasoning_text

    # HITL 필요 시 LLM이 검토 필요 사유와 검토자가 답해야 할 질문을 생성해 hitl_request를 보강
    # 정책: fallback 템플릿/하드코딩 질문 금지. 두 항목이 비면 run 실패 처리.
    if hitl_request:
        claim_results_dicts = [c.model_dump() if hasattr(c, "model_dump") else c for c in claim_results]
        llm_review = await _generate_hitl_review_content(
            hitl_request,
            verification_summary,
            claim_results_dicts,
            reasoning_text,
        )
        if llm_review.get("review_reasons"):
            hitl_request["unresolved_claims"] = llm_review["review_reasons"]
        if llm_review.get("review_questions"):
            hitl_request["review_questions"] = llm_review["review_questions"]
            hitl_request["questions"] = llm_review["review_questions"]
        # 검토 필요로 판정 시 두 항목을 채운다. (생성 함수가 baseline+LLM 결합으로 비지 않게 보장)
        need_reasons = not (hitl_request.get("unresolved_claims"))
        need_questions = not (hitl_request.get("review_questions") or hitl_request.get("questions"))
        if need_reasons or need_questions:
            retry_result = await _retry_fill_hitl_review_when_empty(
                hitl_request,
                verification_summary,
                claim_results_dicts,
                reasoning_text,
                empty_reasons=need_reasons,
                empty_questions=need_questions,
            )
            if retry_result.get("review_reasons"):
                hitl_request["unresolved_claims"] = retry_result["review_reasons"]
            if retry_result.get("review_questions"):
                hitl_request["review_questions"] = retry_result["review_questions"]
                hitl_request["questions"] = retry_result["review_questions"]
        final_reasons = [str(x).strip() for x in (hitl_request.get("unresolved_claims") or []) if str(x).strip()]
        final_questions = [str(x).strip() for x in (hitl_request.get("review_questions") or hitl_request.get("questions") or []) if str(x).strip()]
        hitl_request["unresolved_claims"] = final_reasons[:5]
        hitl_request["review_questions"] = final_questions[:3]
        hitl_request["questions"] = final_questions[:3]

    events: list[dict[str, Any]] = [
        AgentEvent(event_type="NODE_START", node="verify", phase="verify", message="근거 정합성과 추가 검토 필요 여부를 확인합니다.", metadata={}).to_payload(),
    ]
    if retried:
        events.append(
            AgentEvent(
                event_type="THINKING_RETRY",
                node="verify",
                phase="verify",
                message="추론 정합성 불일치 감지 — 재검토 후 문구를 보정합니다.",
                metadata={"conflict": check.conflict_description},
            ).to_payload()
        )
    events.extend(reasoning_events)
    events.append(
        AgentEvent(
            event_type="GATE_APPLIED",
            node="verify",
            phase="verify",
            message="검증 게이트 적용이 완료되었습니다.",
            decision_code="HITL_REQUIRED" if needs_hitl else "READY",
            observation="담당자 검토 필요" if needs_hitl else "자동 진행 가능",
            metadata={**verification},
        ).to_payload(),
    )
    events.append(
        AgentEvent(
            event_type="NODE_END",
            node="verify",
            phase="verify",
            message="검증 단계가 완료되었습니다.",
            observation="담당자 검토 필요" if needs_hitl else "자동 진행 가능",
            metadata={"needs_hitl": needs_hitl, "reasoning": reasoning_text, "note_source": note_source},
        ).to_payload()
    )
    if hitl_request:
        events.append(
            AgentEvent(
                event_type="HITL_REQUESTED",
                node="verify",
                phase="verify",
                message="담당자 검토가 필요한 케이스로 분류되었습니다.",
                decision_code="HITL_REQUIRED",
                metadata=dict(hitl_request),
            ).to_payload(),
        )
    last_node_summary = f"verify 완료: {reasoning_text[:60]}…" if len(reasoning_text) > 60 else f"verify 완료: {reasoning_text}"
    return {"verification": verification, "verifier_output": verifier_output_dict, "hitl_request": hitl_request, "last_node_summary": last_node_summary, "pending_events": events}


def _route_after_critic(state: AgentState) -> str:
    critic_out = state.get("critic_output") or {}
    loop_count = state.get("critic_loop_count") or 0
    has_hitl_response = (state.get("flags") or {}).get("hasHitlResponse", False)
    replan_required = bool(critic_out.get("replan_required"))
    under_limit = loop_count < _MAX_CRITIC_LOOP
    if replan_required and under_limit and not has_hitl_response:
        return "planner"
    return "verify"


def _route_after_verify(state: AgentState) -> str:
    """Phase D: 위험/규정 위배로 hitl_request가 있으면 항상 담당자 검토(hitl_pause). HITL 체크박스와 무관."""
    if state.get("hitl_request"):
        return "hitl_pause"
    return "reporter"


# 규정/LLM에서 나온 required_inputs 필드명 → UI 제출 키 매핑 (필드명 불일치로 재인터럽트 방지)
# business_purpose: 업무 목적란을 비우고 검토 의견에만 적은 경우 comment로 충족
_HITL_FIELD_TO_UI_ALIASES: dict[str, list[str]] = {
    "reason_for_expense": ["comment", "business_purpose"],
    "participants_list": ["attendees"],
    "purpose_of_expense": ["business_purpose", "comment"],
    "comment": ["comment"],
    "attendees": ["attendees"],
    "business_purpose": ["business_purpose", "comment"],
}


def _get_hitl_response_value(hitl_response: dict[str, Any], field: str) -> Any:
    """HITL 응답에서 필드값 추출. 상위 키, extra_facts[field], 규정필드→UI 키 별칭 순으로 확인."""
    def _valid(v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, str):
            return bool(v.strip())
        if isinstance(v, list):
            return len(v) > 0
        return True

    v = hitl_response.get(field)
    if _valid(v):
        return v
    extra = hitl_response.get("extra_facts") or {}
    v = extra.get(field) if isinstance(extra, dict) else None
    if _valid(v):
        return v
    # 규정/LLM 필드명과 UI 제출 키가 다를 수 있음 → 별칭으로 재시도
    aliases = _HITL_FIELD_TO_UI_ALIASES.get(field) or [field]
    for alias in aliases:
        if alias == field:
            continue
        v = hitl_response.get(alias)
        if _valid(v):
            return v
        v = extra.get(alias) if isinstance(extra, dict) else None
        if _valid(v):
            return v
    return None


async def hitl_validate_node(state: AgentState) -> AgentState:
    """
    재분석 시: 사용자가 입력한 HITL 응답이 규정에서 요구한 필수 항목을 채웠는지 에이전트가 판단.
    누락이 있으면 해당 항목만 담은 새 HITL 요청을 만들어 hitl_pause로 돌려 추가 입력을 받는다.
    """
    hitl_request = state.get("hitl_request") or {}
    hitl_response = (state.get("body_evidence") or {}).get("hitlResponse") or {}
    required_inputs = hitl_request.get("required_inputs") or []
    logger.info(
        "[RESUME_TRACE] hitl_validate_node: required_inputs=%s hitl_response_keys=%s",
        [r.get("field") for r in required_inputs[:10]],
        list(hitl_response.keys()) if isinstance(hitl_response, dict) else type(hitl_response).__name__,
    )
    if not required_inputs:
        logger.info("[RESUME_TRACE] hitl_validate_node: required_inputs 비어 있음 → reporter")
        return {"hitl_request": None}

    missing: list[dict[str, str]] = []
    for req in required_inputs:
        field = (req.get("field") or "").strip()
        if not field:
            continue
        val = _get_hitl_response_value(hitl_response, field)
        if val is None:
            missing.append(req)
            logger.info("[RESUME_TRACE] hitl_validate_node: field=%r → 값 없음 (missing)", field)
            continue
        if isinstance(val, list):
            if not val:
                missing.append(req)
                logger.info("[RESUME_TRACE] hitl_validate_node: field=%r → 빈 리스트 (missing)", field)
            continue
        if isinstance(val, str) and not val.strip():
            missing.append(req)
            logger.info("[RESUME_TRACE] hitl_validate_node: field=%r → 빈 문자열 (missing)", field)

    if not missing:
        flags_has = (state.get("flags") or {}).get("hasHitlResponse")
        logger.info(
            "[RESUME_TRACE] hitl_validate_node: 모든 필수 항목 충족 → reporter (flags.hasHitlResponse=%s, True여야 사용자 답변 반영 후 verdict LLM 호출됨)",
            flags_has,
        )
        return {"hitl_request": None}

    logger.info(
        "[RESUME_TRACE] hitl_validate_node: 누락 필드=%s → hitl_pause 재요청",
        [m.get("field") for m in missing[:5]],
    )
    # 누락 항목만으로 재요청 (가이드 문구로 사용자에게 안내)
    new_request = dict(hitl_request)
    new_request["required_inputs"] = missing
    new_request["why_hitl"] = "규정에서 요구한 필수 입력/증빙 항목 중 아래 항목이 비어 있어 추가 입력이 필요합니다."
    new_request["reasons"] = [f"필수 항목 미기입: {m.get('field', '')} — {m.get('reason', '')}" for m in missing[:5]]
    new_request["review_questions"] = [m.get("guide", m.get("reason", "")) for m in missing if m.get("guide") or m.get("reason")]
    if not new_request.get("review_questions"):
        new_request["review_questions"] = [f"{m.get('field')}: {m.get('reason')}" for m in missing]
    new_request["questions"] = new_request["review_questions"]
    return {"hitl_request": new_request}


def _format_hitl_reason_for_stream(hitl_payload: dict[str, Any]) -> str:
    return _hitl_format_reason_for_stream(hitl_payload)


def _route_after_hitl_validate(state: AgentState) -> str:
    """hitl_validate 후: 재요청이 있으면 hitl_pause, 없으면 reporter."""
    if state.get("hitl_request"):
        return "hitl_pause"
    return "reporter"


async def hitl_pause_node(state: AgentState) -> AgentState:
    """Phase D: HITL 필요 시 interrupt()로 중단. resume 시 hitl_response를 body_evidence에 반영하고, hitl_validate를 거쳐 reporter 또는 재요청으로 간다."""
    hitl_request = state.get("hitl_request") or {}
    # interrupt()로 일시정지; 재개 시 호출자가 Command(resume=payload)로 넘긴 값이 여기로 반환됨
    hitl_response = interrupt(hitl_request)
    hitl_response = hitl_response if isinstance(hitl_response, dict) else {}
    # 재개 시: resume_value가 넘어오면 approved/comment 등 사용자 입력 키가 있음. 첫 정지 시: hitl_request와 동일/빈 dict일 수 있음
    looks_like_resume = isinstance(hitl_response, dict) and (
        "approved" in hitl_response or "comment" in hitl_response or "reviewer" in hitl_response
    )
    logger.info(
        "[RESUME_TRACE] hitl_pause_node: interrupt 반환 keys=%s looks_like_resume=%s",
        list(hitl_response.keys()) if hitl_response else [],
        looks_like_resume,
    )
    body = dict(state.get("body_evidence") or {})
    body["hitlResponse"] = hitl_response
    out: dict[str, Any] = {"body_evidence": body}
    # 재개 시: reporter/finalizer가 사용자 답변을 인식하도록 flags 반영 (미반영 시 hasHitlResponse=False로 verdict LLM 미호출 → 응답 없음 현상)
    if looks_like_resume and hitl_response:
        flags = dict(state.get("flags") or {})
        flags["hasHitlResponse"] = True
        flags["hitlApproved"] = hitl_response.get("approved") is True
        out["flags"] = flags
        logger.info(
            "[HITL_RESPONSE_TRACE] hitl_pause_node: 재개 감지 → flags.hasHitlResponse=True hitlApproved=%s 반영 (verdict LLM 호출되도록 함)",
            flags["hitlApproved"],
        )
    return out


async def reporter_node(state: AgentState) -> AgentState:
    score = _score_with_hitl_adjustment(state["score_breakdown"], state["flags"])
    hitl_request = state.get("hitl_request")
    body = state["body_evidence"]
    hitl_response = body.get("hitlResponse") or {}
    hitl_approved = hitl_response.get("approved")
    hitl_verdict_reason = ""
    occurred_at = _format_occurred_at(body.get("occurredAt"))
    merchant = body.get("merchantName") or "거래처 미상"
    summary = (
        f"전표는 {occurred_at} 시점 {merchant} 사용 건으로 분석되었습니다. "
        f"정책점수 {score['policy_score']}점, 근거점수 {score['evidence_score']}점, 최종점수 {score['final_score']}점입니다."
    )
    has_hitl_response = state["flags"].get("hasHitlResponse")
    body_has_hitl = bool(hitl_response)
    if body_has_hitl and not has_hitl_response:
        logger.warning(
            "[HITL_RESPONSE_TRACE] reporter_node 의심: body_evidence.hitlResponse 있음(keys=%s) but flags.hasHitlResponse=False → 사용자 답변 작성했는데 응답 없으면 flags 미반영 가능성",
            list(hitl_response.keys())[:12] if isinstance(hitl_response, dict) else None,
        )
    logger.info(
        "[VERDICT_LLM] reporter_node verdict 분기: hitl_request=%s hasHitlResponse=%s hitl_approved=%s (body_has_hitl=%s)",
        bool(hitl_request),
        has_hitl_response,
        hitl_approved,
        body_has_hitl,
    )
    if hitl_request:
        summary += " 담당자 검토가 필요한 상태입니다."
        verdict = "HITL_REQUIRED"
        logger.info("[VERDICT_LLM] reporter_node → 분기: hitl_request 있음, verdict=HITL_REQUIRED (verdict LLM 미호출)")
    elif has_hitl_response and hitl_approved is True:
        prior_hitl_request = (body.get("hitlRequest") or {}) if isinstance(body.get("hitlRequest"), dict) else {}
        check_req = hitl_request or prior_hitl_request or {}
        evidence_result = body.get("evidenceDocumentResult") if isinstance(body.get("evidenceDocumentResult"), dict) else None
        logger.info(
            "[VERDICT_LLM] reporter_node → verdict LLM 호출 (hasHitlResponse=True approved=True) check_req_keys=%s",
            list(check_req.keys())[:12] if isinstance(check_req, dict) else None,
        )
        # 확정 vs 재검토는 룰/하드코딩이 아닌 LLM 판단으로 결정 (필요 답변 없이 승인만 온 경우 재검토가 나오는지 LLM이 맥락으로 판단)
        verdict, hitl_verdict_reason = await _llm_decide_hitl_verdict(
            hitl_request=check_req,
            hitl_response=hitl_response,
            evidence_result=evidence_result,
        )
        logger.info(
            "[VERDICT_LLM] reporter_node ← verdict LLM 반환 verdict=%s reason_preview=%s",
            verdict,
            (hitl_verdict_reason[:100] + "…") if hitl_verdict_reason and len(hitl_verdict_reason) > 100 else (hitl_verdict_reason or "(없음)"),
        )
        if verdict == "COMPLETED_AFTER_HITL":
            summary += f" {hitl_verdict_reason}" if hitl_verdict_reason else " 담당자 검토 결과 승인 가능으로 판단되어 최종 확정 후보로 전환되었습니다."
        else:
            summary += f" {hitl_verdict_reason}" if hitl_verdict_reason else " 담당자 검토 기준 미충족으로 재검토가 필요합니다."
    elif has_hitl_response and hitl_approved is False:
        verdict = "HOLD_AFTER_HITL"
        hold_summary = await _llm_summarize_hold_reason(hitl_response)
        if hold_summary:
            hitl_verdict_reason = hold_summary
            summary += f" 담당자 검토 결과 보류합니다. {hold_summary}"
        else:
            hitl_verdict_reason = ""
            hold_reason = (hitl_response.get("comment") or hitl_response.get("business_purpose") or "").strip()
            if hold_reason:
                summary += f" 담당자 검토 결과 보류합니다. 담당자 사유: {hold_reason[:400]}{'…' if len(hold_reason) > 400 else ''}"
            else:
                summary += " 담당자 검토 결과 보류/추가 검토 의견이 있어 자동 확정을 중단합니다."
        logger.info("[VERDICT_LLM] reporter_node → 분기: approved=False, verdict=HOLD_AFTER_HITL (verdict LLM 미호출)")
    else:
        if _is_verify_ready_without_hitl(state):
            summary += " 검증 게이트를 통과해 자동 확정 후보로 분류되었습니다."
        else:
            summary += " 현재 수집된 증거 기준으로 추가 검토 우선순위가 높습니다."
        verdict = "READY"
        logger.info("[VERDICT_LLM] reporter_node → 분기: hasHitlResponse=False 또는 기타, verdict=READY (verdict LLM 미호출)")
    refs = _top_policy_refs(state.get("tool_results", []), limit=5)
    refs = await _select_policy_refs_by_relevance(state, refs)
    citations_list = []
    for ref in refs:
        cids = ref.get("chunk_ids") or []
        chunk_id = str(cids[0]) if cids else None
        citations_list.append(Citation(chunk_id=chunk_id, article=ref.get("article") or "조항 미상", title=ref.get("parent_title")))
    sentences_list: list[ReporterSentence] = [
        ReporterSentence(sentence=summary, citations=citations_list),
    ]
    reasoning_text = f"{summary} {verdict}".strip()
    # v3 정합성 검증 + 1회 보정
    reasoning_text, check, retried = call_node_llm_with_consistency_check("reporter", {"verdict": verdict}, reasoning_text, max_retries=1)
    reporter_context = {
        "verdict": verdict,
        "summary": summary,
        "score_breakdown": score,
        "last_node_summary": state.get("last_node_summary", "없음"),
    }
    reasoning_text, reasoning_events, note_source = await _stream_reasoning_events_with_llm("reporter", reasoning_text, context=reporter_context)
    reporter_output = ReporterOutput(summary=summary, verdict=verdict, sentences=sentences_list, reasoning=reasoning_text)
    reporter_output_dict = reporter_output.model_dump()
    if verdict in ("COMPLETED_AFTER_HITL", "REVIEW_REQUIRED", "HOLD_AFTER_HITL") and hitl_verdict_reason:
        reporter_output_dict["hitl_verdict_reason"] = hitl_verdict_reason
    last_node_summary = f"reporter 완료: {reasoning_text[:60]}…" if len(reasoning_text) > 60 else f"reporter 완료: {reasoning_text}"
    pending: list[dict[str, Any]] = [
        AgentEvent(event_type="NODE_START", node="reporter", phase="report", message="사용자에게 제시할 보고 문안을 구성합니다.", metadata={}).to_payload(),
    ]
    if retried:
        pending.append(
            AgentEvent(
                event_type="THINKING_RETRY",
                node="reporter",
                phase="report",
                message="추론 정합성 불일치 감지 — 재검토 후 문구를 보정합니다.",
                metadata={"conflict": check.conflict_description},
            ).to_payload()
        )
    pending.extend(reasoning_events)
    # NODE_END에는 완료 문구만 표시(추론 블록에 이미 보고 문안이 있으므로 중복 제거)
    pending.append(
        AgentEvent(
            event_type="NODE_END",
            node="reporter",
            phase="report",
            message="보고 문안 구성이 완료되었습니다.",
            metadata={"summary": summary, "verdict": verdict, "reasoning": reasoning_text, "note_source": note_source},
        ).to_payload(),
    )
    return {
        "reporter_output": reporter_output_dict,
        "last_node_summary": last_node_summary,
        "pending_events": pending,
    }


async def finalizer_node(state: AgentState) -> AgentState:
    score = _score_with_hitl_adjustment(state["score_breakdown"], state["flags"])
    hitl_request = state.get("hitl_request")
    completed_tail = None
    if not hitl_request and not state.get("flags", {}).get("hasHitlResponse"):
        verify_ready = _is_verify_ready_without_hitl(state)
        reporter_verdict = str((state.get("reporter_output") or {}).get("verdict") or "").upper()
        if verify_ready and reporter_verdict in {"", "READY", "COMPLETED", "AUTO_APPROVED"}:
            completed_tail = await _llm_summarize_completed_reason(state)
    reason, status = _build_grounded_reason(state, completed_tail=completed_tail)
    final_reasoning = f"{reason} 최종 상태는 {status}로 확정한다.".strip()
    final_reasoning, final_check, final_retried = call_node_llm_with_consistency_check("finalizer", {"status": status}, final_reasoning, max_retries=1)
    finalizer_context = {
        "status": status,
        "score_breakdown": score,
        "has_hitl_request": bool(hitl_request),
        "last_node_summary": state.get("last_node_summary", "없음"),
        "score_semantics": (
            "정책점수(policy_score)는 위험 지표: 높을수록 휴일/심야/근태충돌/고위험업종 등 위반 정황이 많음. "
            "근거점수(evidence_score)는 수집 증거 충실도: 높을수록 규정 조항·전표 증거가 잘 확보됨. "
            "따라서 정책점수 높음=위험 높음, 근거점수 높음=증거 충실."
        ),
    }
    final_reasoning, final_reasoning_events, note_source = await _stream_reasoning_events_with_llm("finalizer", final_reasoning, context=finalizer_context)
    # REVIEW_REQUIRED 경로도 공통 검토 팝업에서 사유/질문을 사용하므로 반드시 생성·저장한다.
    if status == "REVIEW_REQUIRED" and not hitl_request:
        verification_summary = (state.get("verification") or {}).get("verification_summary") or {}
        verifier_output = state.get("verifier_output") or {}
        claim_results = verifier_output.get("claim_results") or []
        stop_reasons = _build_system_auto_finalize_blockers(
            verification_summary,
            quality_signals=(state.get("verification") or {}).get("quality_signals") or [],
            fallback_reason=reason,
        )
        seed_request = {
            "required": True,
            "handoff": "FINANCE_REVIEWER",
            "why_hitl": reason,
            "blocking_gate": "REVIEW_REQUIRED",
            "blocking_reason": reason,
            "reasons": stop_reasons,
            "auto_finalize_blockers": stop_reasons,
            "required_inputs": [],
            "evidence_snapshot": [],
        }
        llm_review = await _generate_hitl_review_content(
            seed_request,
            verification_summary,
            claim_results if isinstance(claim_results, list) else [],
            final_reasoning,
        )
        review_reasons = [str(x).strip() for x in (llm_review.get("review_reasons") or []) if str(x).strip()]
        review_questions = [str(x).strip() for x in (llm_review.get("review_questions") or []) if str(x).strip()]
        if not review_reasons or not review_questions:
            retry_result = await _retry_fill_hitl_review_when_empty(
                seed_request,
                verification_summary,
                claim_results if isinstance(claim_results, list) else [],
                final_reasoning,
                empty_reasons=not review_reasons,
                empty_questions=not review_questions,
            )
            if not review_reasons:
                review_reasons = [str(x).strip() for x in (retry_result.get("review_reasons") or []) if str(x).strip()]
            if not review_questions:
                review_questions = [str(x).strip() for x in (retry_result.get("review_questions") or []) if str(x).strip()]
        if not review_reasons:
            review_reasons = [str(reason or "검토가 필요한 근거를 확인해 주세요.").strip()]
        if not review_questions:
            review_questions = [f"다음 판단 사유를 해소할 근거를 확인할 수 있는가: {str(reason or '')[:180]}"]
        hitl_request = {
            **seed_request,
            "why_hitl": review_reasons[0],
            "blocking_reason": review_reasons[0],
            "reasons": review_reasons,
            "auto_finalize_blockers": stop_reasons,
            "unresolved_claims": review_reasons,
            "review_questions": review_questions,
            "questions": review_questions,
        }

    probe_facts = (_find_tool_result(state["tool_results"], "policy_rulebook_probe") or {}).get("facts", {}) or {}
    policy_refs = probe_facts.get("policy_refs") or []
    reporter_out = state.get("reporter_output") or {}
    sentences = reporter_out.get("sentences") or []
    adopted_citations: list[dict[str, Any]] = []
    for s in sentences:
        for c in (s.get("citations") or []):
            if isinstance(c, dict):
                cit = dict(c)
            else:
                cit = {"chunk_id": str(c)} if c is not None else {}
            # adoption_reason: policy_refs에서 chunk_id/article 매칭하여 보강
            if "adoption_reason" not in cit:
                cid = str(cit.get("chunk_id") or "")
                art = cit.get("article")
                for ref in policy_refs:
                    ref_cids = [str(x) for x in (ref.get("chunk_ids") or [])]
                    if cid and cid in ref_cids:
                        cit["adoption_reason"] = ref.get("adoption_reason", "규정 근거로 채택")
                        break
                    if art and str(ref.get("article") or "") == str(art):
                        cit["adoption_reason"] = ref.get("adoption_reason", "규정 근거로 채택")
                        break
                else:
                    cit.setdefault("adoption_reason", "규정 근거로 채택")
            adopted_citations.append(cit)
    retrieval_snapshot = {
        "candidates_after_rerank": probe_facts.get("retrieval_candidates") or policy_refs,
        "adopted_citations": adopted_citations,
    }
    final = {
        "caseId": state["case_id"],
        "status": status,
        "reasonText": reason,
        "score": score["final_score"] / 100,
        "severity": score.get("severity") or ("HIGH" if score["final_score"] >= 70 else ("MEDIUM" if score["final_score"] >= 40 else "LOW")),
        "analysis_mode": "langgraph_agentic",
        "score_breakdown": score,
        "quality_gate_codes": (state.get("verification") or {}).get("quality_signals", []),
        "hitl_request": hitl_request,
        "tool_results": state["tool_results"],
        "policy_refs": probe_facts.get("policy_refs") or [],
        "critique": state.get("critique"),
        "hitl_response": (state["body_evidence"].get("hitlResponse") or None),
        "planner_output": state.get("planner_output"),
        "execute_output": state.get("execute_output"),
        "critic_output": state.get("critic_output"),
        "verifier_output": state.get("verifier_output"),
        "reporter_output": state.get("reporter_output"),
        "retrieval_snapshot": retrieval_snapshot,
        "verification_summary": (state.get("verification") or {}).get("verification_summary"),
    }
    pending_events: list[dict[str, Any]] = [
        AgentEvent(event_type="NODE_START", node="finalizer", phase="finalize", message="최종 판정 결과를 확정합니다.", metadata={}).to_payload(),
    ]
    if final_retried:
        pending_events.append(
            AgentEvent(
                event_type="THINKING_RETRY",
                node="finalizer",
                phase="finalize",
                message="추론 정합성 불일치 감지 — 재검토 후 문구를 보정합니다.",
                metadata={"conflict": final_check.conflict_description},
            ).to_payload()
        )
    pending_events.extend(final_reasoning_events)
    pending_events.append(
        AgentEvent(
            event_type="NODE_END",
            node="finalizer",
            phase="finalize",
            message="최종 분석 결과가 생성되었습니다.",
            observation=f"최종 상태={status}",
            metadata={"status": status, "reasoning": final_reasoning, "note_source": note_source},
        ).to_payload()
    )
    return {
        "final_result": final,
        "pending_events": pending_events,
    }


def _get_checkpointer():
    global _CHECKPOINTER
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER
    _runtime_module._CHECKPOINTER = _CHECKPOINTER
    _CHECKPOINTER = _runtime_get_checkpointer()
    return _CHECKPOINTER


def build_agent_graph():
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is not None:
        return _COMPILED_GRAPH
    _runtime_module._COMPILED_GRAPH = _COMPILED_GRAPH
    _COMPILED_GRAPH = _runtime_build_agent_graph(
        state_type=AgentState,
        start_router_node=start_router_node,
        screener_node=screener_node,
        intake_node=intake_node,
        planner_node=planner_node,
        execute_node=execute_node,
        critic_node=critic_node,
        verify_node=verify_node,
        hitl_pause_node=hitl_pause_node,
        hitl_validate_node=hitl_validate_node,
        reporter_node=reporter_node,
        finalizer_node=finalizer_node,
        route_after_critic=_route_after_critic,
        route_after_verify=_route_after_verify,
        route_after_hitl_validate=_route_after_hitl_validate,
    )
    return _COMPILED_GRAPH


def build_hitl_closure_graph():
    global _CLOSURE_GRAPH
    if _CLOSURE_GRAPH is not None:
        return _CLOSURE_GRAPH
    _runtime_module._CLOSURE_GRAPH = _CLOSURE_GRAPH
    _CLOSURE_GRAPH = _runtime_build_hitl_closure_graph(
        state_type=AgentState,
        hitl_validate_node=hitl_validate_node,
        reporter_node=reporter_node,
        finalizer_node=finalizer_node,
    )
    return _CLOSURE_GRAPH


def _closure_verification(verification: dict[str, Any] | None) -> dict[str, Any]:
    return _runtime_closure_verification(verification)


def _closure_initial_state(
    *,
    previous_result: dict[str, Any],
    body_evidence: dict[str, Any],
    case_id: str,
    intended_risk_type: str | None,
    resume_value: dict[str, Any],
) -> dict[str, Any]:
    return _runtime_closure_initial_state(
        previous_result=previous_result,
        body_evidence=body_evidence,
        case_id=case_id,
        intended_risk_type=intended_risk_type,
        resume_value=resume_value,
    )


async def run_langgraph_agentic_analysis(
    case_id: str,
    *,
    body_evidence: dict[str, Any],
    intended_risk_type: str | None = None,
    run_id: str | None = None,
    resume_value: dict[str, Any] | None = None,
    previous_result: dict[str, Any] | None = None,
    enable_hitl: bool = True,
):
    from langgraph.types import Command
    from utils.config import get_langfuse_handler

    if not run_id:
        run_id = "default-thread"
    logger.info(
        "[RESUME_TRACE] run_langgraph 진입: run_id=%s case_id=%s resume_value=%s (None이면 처음부터, 있으면 1차 checkpoint 재개 시도)",
        run_id, case_id, "있음" if resume_value else "없음",
    )
    if resume_value:
        _cmt = str(resume_value.get("comment") or "") if isinstance(resume_value, dict) else ""
        _prev = (_cmt[:80] + "…") if len(_cmt) > 80 else _cmt or "(없음)"
        logger.info(
            "[RESUME_TRACE] run_langgraph resume_value 요약: approved=%s comment_len=%s comment_preview=%s keys=%s",
            resume_value.get("approved") if isinstance(resume_value, dict) else None,
            len(_cmt),
            _prev,
            list(resume_value.keys())[:10] if isinstance(resume_value, dict) else [],
        )
    config: dict[str, Any] = {
        "configurable": {"thread_id": run_id},
        "tags": ["matertask", "analysis", f"case:{case_id}"],
    }
    handler = get_langfuse_handler(session_id=run_id)
    if handler:
        config["callbacks"] = [handler]

    graph = build_agent_graph()

    # HITL 이후 재개 시도: 우선 LangGraph의 Command(resume=...)를 사용하고,
    # 체크포인트가 없어서 'body_evidence' KeyError가 나면 동일 run_id로 새 입력으로 재시작한다.
    async def _stream_from_graph(_inputs: Any):
        async for chunk in graph.astream(_inputs, stream_mode="updates", config=config):
            yield chunk

    async def _yield_updates(chunks, path_tag: str | None = None):
        path_label = f" {path_tag}" if path_tag else ""
        async for chunk in chunks:
            if chunk.get("__interrupt__"):
                logger.info("[agent] graph __interrupt__ (HITL pause) — stream will end until review-submit resume")
                # HITL: interrupt()로 일시정지. 호출자에게 HITL_REQUIRED 전달 후 같은 run_id로 재개 대기
                interrupt_list = chunk["__interrupt__"]
                hitl_payload = interrupt_list[0].value if interrupt_list else {}
                reason_text = _format_hitl_reason_for_stream(hitl_payload)
                base_msg = "담당자 검토가 필요합니다."
                if reason_text:
                    stream_msg = f"{base_msg} {reason_text} HITL 응답 후 같은 run으로 재개됩니다."
                    reason_final = f"담당자 검토 입력을 기다립니다. 사유: {reason_text}"
                else:
                    stream_msg = f"{base_msg} HITL 응답 후 같은 run으로 재개됩니다."
                    reason_final = "담당자 검토 입력을 기다립니다."
                yield "AGENT_EVENT", AgentEvent(
                    event_type="HITL_PAUSE",
                    node="hitl_pause",
                    phase="verify",
                    message=stream_msg,
                    observation="interrupt",
                    metadata={"hitl_request": hitl_payload, "reason": reason_text},
                ).to_payload()
                yield "completed", {
                    "status": "HITL_REQUIRED",
                    "hitl_request": hitl_payload,
                    "reasonText": reason_final,
                }
                return
            for _node, update in chunk.items():
                if _node == "__interrupt__":
                    continue
                update = update or {}
                logger.info("[RESUME_TRACE] run_langgraph%s 노드 실행: run_id=%s node=%s", path_label, run_id, _node)
                pending = (update.get("pending_events") or []) or []
                thinking_count = sum(1 for e in pending if (e or {}).get("event_type", "").upper().startswith("THINKING"))
                if thinking_count:
                    logger.info("[agent] node=%s pending_events=%s (THINKING_*=%s)", _node, len(pending), thinking_count)
                for ev in pending:
                    ev = ev or {}
                    if ev.get("event_type") == "SCORE_BREAKDOWN":
                        yield "confidence", {
                            "label": "RISK_SCORE_BREAKDOWN",
                            "detail": ev.get("message"),
                            "score_breakdown": (ev.get("metadata") or {}),
                        }
                    else:
                        yield "AGENT_EVENT", ev
                final = update.get("final_result")
                if final is not None:
                    yield "completed", final

    # 1차: checkpoint 기반 resume 시도
    if resume_value is not None:
        rv_keys = list(resume_value.keys())[:12] if isinstance(resume_value, dict) else []
        logger.info(
            "[RESUME_TRACE] run_langgraph run_id=%s 1차 시작: Command(resume=...) resume_value_keys=%s (hitl_pause→hitl_validate→reporter→finalizer)",
            run_id,
            rv_keys,
        )
        try:
            yielded_terminal = False
            async for ev in _yield_updates(_stream_from_graph(Command(resume=resume_value)), path_tag="1차"):
                ev_type = ev[0] if isinstance(ev, (list, tuple)) and len(ev) >= 1 else ""
                if ev_type in ("completed", "failed"):
                    yielded_terminal = True
                yield ev
            if yielded_terminal:
                logger.info("[RESUME_TRACE] run_langgraph run_id=%s 1차 완료: checkpoint 재개 성공", run_id)
                return
            # 일부 환경/버전에서 Command(resume=...)가 예외 없이 이벤트 없이 끝나는 경우가 있다.
            # 이 경우 HOLD로 마감하기보다는 2차 경로(스크리닝부터 재실행)로 자동 전환해 결과를 반환한다.
            logger.warning(
                "[RESUME_TRACE] run_langgraph run_id=%s 1차: 스트림이 터미널 이벤트(completed/failed) 없이 종료됨 → 2차 경로로 자동 전환(스크리닝부터 재실행)",
                run_id,
            )
        except KeyError as e:
            # 체크포인트가 없거나 깨진 경우: 'body_evidence' KeyError를 만나면 동일 run_id로 새 입력으로 재시작
            if str(e) != "'body_evidence'":
                raise
            logger.warning(
                "[RESUME_TRACE] run_langgraph run_id=%s 1차 실패: checkpoint 없음 (checkpointer=%s). "
                "2차 경량(기존 결과 있음) 또는 2차 전체 재실행으로 진행.",
                run_id,
                getattr(settings, "checkpointer_backend", "memory"),
            )
            # fallthrough to 2차 (경량 가능 시 경량, 아니면 전체)

    # 2차: HITL 재개 시 기존 결과(또는 hitl_request만)가 있으면 2차 경량(closure)으로 hitl_validate→reporter→finalizer만 실행.
    # score_breakdown/tool_results가 없어도 hitl_request가 있으면 경량 경로 사용(전체 재실행 시 verify에서 또 인터럽트되는 것 방지).
    if resume_value is not None and previous_result and (previous_result.get("hitl_request") or previous_result.get("score_breakdown") or previous_result.get("tool_results")):
        logger.info(
            "[RESUME_TRACE] run_langgraph run_id=%s 2차 경량: 기존 결과+검토의견 반영 (execute 재실행 없음, hitl_validate→reporter→finalizer)",
            run_id,
        )
        body_with_hitl = dict(body_evidence or {})
        body_with_hitl["hitlResponse"] = resume_value
        body_with_hitl["_enable_hitl"] = enable_hitl
        closure_state = _closure_initial_state(
            previous_result=previous_result,
            body_evidence=body_with_hitl,
            case_id=case_id,
            intended_risk_type=intended_risk_type,
            resume_value=resume_value,
        )
        closure_graph = build_hitl_closure_graph()
        closure_config: dict[str, Any] = {"configurable": {"thread_id": run_id}}
        _handler = get_langfuse_handler(session_id=run_id)
        if _handler:
            closure_config["callbacks"] = [_handler]

        async def _stream_closure():
            async for chunk in closure_graph.astream(closure_state, stream_mode="updates", config=closure_config):
                yield chunk

        async for ev in _yield_updates(_stream_closure(), path_tag="2차경량"):
            yield ev
        return

    if resume_value is not None:
        logger.info(
            "[RESUME_TRACE] run_langgraph run_id=%s 2차 시작: 스크리닝부터 전체 실행 (execute 포함 재실행, body_evidence에 hitlResponse 주입)",
            run_id,
        )
    else:
        logger.info("[RESUME_TRACE] run_langgraph run_id=%s 경로: 스크리닝부터 전체 실행 (resume_value 없음)", run_id)
    body_with_hitl = dict(body_evidence or {})
    if resume_value is not None:
        body_with_hitl["hitlResponse"] = resume_value
    body_with_hitl["_enable_hitl"] = enable_hitl
    inputs = {
        "case_id": case_id,
        "body_evidence": body_with_hitl,
        "intended_risk_type": intended_risk_type,
    }
    path_tag_2 = "2차" if resume_value is not None else ""
    async for ev in _yield_updates(_stream_from_graph(inputs), path_tag=path_tag_2 or None):
        yield ev
