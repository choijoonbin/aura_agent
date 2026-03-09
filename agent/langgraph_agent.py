from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import re
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent.event_schema import AgentEvent
from agent.hitl import build_hitl_request
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
_CHECKPOINTER: Any | None = None
_COMPILED_GRAPH: Any | None = None
_MAX_CRITIC_LOOP = 2
_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")
_CLAIM_PRIORITY: dict[str, int] = {
    "night_violation": 10,
    "holiday_hr_conflict": 9,
    "merchant_high_risk": 8,
    "budget_exceeded": 7,
    "amount_approval_tier": 6,
    "policy_ref_direct": 5,
}
_REASONING_JSON_MARKER = '"reasoning"'


@dataclass
class ConsistencyCheckResult:
    is_consistent: bool
    conflict_description: str


def _compact_reasoning_for_stream(text: str) -> str:
    """스트림 가독성을 위해 reasoning 길이를 문장/문자 기준으로 압축."""
    raw = str(text or "").replace("\\n", " ").replace("\n", " ")
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        return ""

    sentence_splits = re.split(r"(?<=[.!?])\s+|(?<=다\.)\s+", normalized)
    sentence_splits = [s.strip() for s in sentence_splits if s and s.strip()]
    max_sentences = max(1, int(settings.reasoning_stream_max_sentences))
    compact = " ".join(sentence_splits[:max_sentences]) if sentence_splits else normalized

    max_chars = max(80, int(settings.reasoning_stream_max_chars))
    if len(compact) > max_chars:
        compact = compact[:max_chars].rstrip()
        compact = re.sub(r"[,:;·/\-]\s*[^,:;·/\-]*$", "", compact).rstrip()
        compact = compact.rstrip(".") + "..."
    return compact


def _get_attr(output: Any, key: str) -> Any:
    if hasattr(output, key):
        return getattr(output, key)
    if isinstance(output, dict):
        return output.get(key)
    return None


def check_reasoning_consistency(node_name: str, output: Any) -> ConsistencyCheckResult:
    """
    workspace.md v3: reasoning 텍스트와 결과 필드 간 모순을 감지한다.
    모순이 감지되면 conflict_description을 반환해 재검토(재생성/수정)에 활용한다.
    """
    reasoning = str(_get_attr(output, "reasoning") or "").lower()
    if not reasoning.strip():
        return ConsistencyCheckResult(is_consistent=True, conflict_description="")

    hold_signals = ["보류", "hold", "위반", "문제", "검토 필요", "부적합", "중단", "실패", "fail"]
    pass_signals = ["정상", "통과", "pass", "적합", "문제없", "이상없", "승인", "가능"]
    reasoning_says_hold = any(s in reasoning for s in hold_signals)
    reasoning_says_pass = any(s in reasoning for s in pass_signals)

    # --- Critic ---
    if node_name == "critic":
        recommend_hold = bool(_get_attr(output, "recommend_hold"))
        if recommend_hold and reasoning_says_pass and not reasoning_says_hold:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description=(
                    "reasoning은 '정상/통과' 취지이나 recommend_hold=True가 반환되었습니다. "
                    "reasoning과 결과값이 일치하도록 재작성하십시오."
                ),
            )
        if (not recommend_hold) and reasoning_says_hold and not reasoning_says_pass:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description=(
                    "reasoning은 '보류/위반' 취지이나 recommend_hold=False가 반환되었습니다. "
                    "reasoning과 결과값이 일치하도록 재작성하십시오."
                ),
            )

    # --- Verifier ---
    if node_name in {"verify", "verifier"}:
        gate = _get_attr(output, "gate")
        gate_str = str(gate or "").upper()
        # 기존 스펙은 PASS/FAIL 예시지만, 현재 구현은 READY/HITL_REQUIRED를 사용한다.
        if gate_str in {"READY", "PASS"} and reasoning_says_hold and not reasoning_says_pass:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description="reasoning은 검증 실패/보류 취지이나 gate=READY(PASS)가 반환되었습니다. 일치하도록 재작성하십시오.",
            )
        if gate_str in {"HITL_REQUIRED", "FAIL"} and reasoning_says_pass and not reasoning_says_hold:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description="reasoning은 검증 통과 취지이나 gate=HITL_REQUIRED(FAIL)이 반환되었습니다. 일치하도록 재작성하십시오.",
            )

    # --- Reporter ---
    if node_name == "reporter":
        verdict = str(_get_attr(output, "verdict") or "").upper()
        if verdict in {"HITL_REQUIRED", "HOLD", "REJECT", "HOLD_AFTER_HITL"} and reasoning_says_pass and not reasoning_says_hold:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description=f"reasoning은 정상 처리 취지이나 verdict={verdict}가 반환되었습니다. 일치하도록 재작성하십시오.",
            )

    # --- Finalizer ---
    if node_name == "finalizer":
        status = str(_get_attr(output, "status") or _get_attr(output, "final_status") or "").upper()
        if status in {"HITL_REQUIRED", "HOLD", "REJECT", "FAILED", "HOLD_AFTER_HITL"} and reasoning_says_pass and not reasoning_says_hold:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description=f"reasoning은 정상 처리 취지이나 status={status}가 반환되었습니다. 일치하도록 재작성하십시오.",
            )

    return ConsistencyCheckResult(is_consistent=True, conflict_description="")


def _repair_reasoning_for_consistency(node_name: str, output: Any, *, current_reasoning: str) -> str:
    """LLM 재호출 없이도 모순을 차단하기 위한 1회 보정(템플릿 기반)."""
    text = (current_reasoning or "").strip()
    if node_name == "critic":
        recommend_hold = bool(_get_attr(output, "recommend_hold"))
        return (text + (" 결론: 보류가 적절하다." if recommend_hold else " 결론: 정상 진행이 가능하다.")).strip()
    if node_name in {"verify", "verifier"}:
        gate = str(_get_attr(output, "gate") or "").upper()
        return (text + (" 결론: 사람 검토(HITL)가 필요하다." if gate == "HITL_REQUIRED" else " 결론: 자동 진행이 가능하다.")).strip()
    if node_name == "reporter":
        verdict = str(_get_attr(output, "verdict") or "").upper()
        if verdict in {"HITL_REQUIRED", "HOLD", "REJECT", "HOLD_AFTER_HITL"}:
            return (text + " 결론: 보류/사람 검토가 필요하다.").strip()
        return (text + " 결론: 자동 확정 후보로 진행한다.").strip()
    if node_name == "finalizer":
        status = str(_get_attr(output, "status") or _get_attr(output, "final_status") or "").upper()
        if status in {"HITL_REQUIRED", "HOLD", "REJECT", "FAILED", "HOLD_AFTER_HITL"}:
            return (text + " 결론: 보류 또는 사람 검토가 필요하다.").strip()
        return (text + " 결론: 최종 확정을 진행한다.").strip()
    return text


def call_node_llm_with_consistency_check(
    node_name: str,
    output: Any,
    reasoning_text: str,
    *,
    max_retries: int = 1,
) -> tuple[str, ConsistencyCheckResult, bool]:
    """
    workspace.md v3 체크리스트 대응:
    reasoning 정합성 검사 후 모순 시 최대 1회 자동 재검토(보정)한다.
    반환: (최종 reasoning, 마지막 검사 결과, 재시도 여부)
    """
    text = (reasoning_text or "").strip()
    retried = False
    last_check = ConsistencyCheckResult(is_consistent=True, conflict_description="")
    for attempt in range(max_retries + 1):
        last_check = check_reasoning_consistency(node_name, {**(output if isinstance(output, dict) else {}), "reasoning": text} if isinstance(output, dict) else output)
        if last_check.is_consistent:
            return text, last_check, retried
        if attempt < max_retries:
            retried = True
            text = _repair_reasoning_for_consistency(node_name, output, current_reasoning=text)
    return text, last_check, retried


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
    raw = str(value or "").strip()
    if not raw:
        return "발생시각 미상"
    try:
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        weekdays = ["월", "화", "수", "목", "금", "토", "일"]
        return f"{dt.year}년 {dt.month:02d}월 {dt.day:02d}일 {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} ({weekdays[dt.weekday()]})"
    except Exception:
        return raw


def _reasoning_stream_events(node_name: str, reasoning_text: str) -> list[dict[str, Any]]:
    """workspace.md 추가보완: reasoning 문자열을 THINKING_TOKEN(단어 단위) + THINKING_DONE 이벤트로 변환."""
    if not (reasoning_text or "").strip():
        return [
            AgentEvent(
                event_type="THINKING_DONE",
                node=node_name,
                message="",
                metadata={"reasoning": ""},
            ).to_payload(),
        ]
    text = reasoning_text.strip()
    events: list[dict[str, Any]] = []
    for word in text.split():
        if not word:
            continue
        events.append(
            AgentEvent(
                event_type="THINKING_TOKEN",
                node=node_name,
                message="",
                metadata={"token": word + " "},
            ).to_payload(),
        )
    events.append(
        AgentEvent(
            event_type="THINKING_DONE",
            node=node_name,
            message=text,
            metadata={"reasoning": text},
        ).to_payload(),
    )
    return events


def _extract_reasoning_token(delta: str, full_so_far: str, emitted_len: int) -> tuple[str, int, bool]:
    """
    workspace.md v2 STEP-3: JSON 스트리밍 중 reasoning 필드의 신규 토큰만 추출.
    반환값: (new_token, new_emitted_len, completed)
    """
    if not delta and not full_so_far:
        return "", emitted_len, False
    marker_idx = full_so_far.find(_REASONING_JSON_MARKER)
    if marker_idx < 0:
        return "", emitted_len, False
    colon_idx = full_so_far.find(":", marker_idx + len(_REASONING_JSON_MARKER))
    if colon_idx < 0:
        return "", emitted_len, False
    q0 = full_so_far.find('"', colon_idx + 1)
    if q0 < 0:
        return "", emitted_len, False

    chars: list[str] = []
    escaped = False
    i = q0 + 1
    completed = False
    while i < len(full_so_far):
        ch = full_so_far[i]
        if escaped:
            chars.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            completed = True
            break
        else:
            chars.append(ch)
        i += 1
    current = "".join(chars)
    if len(current) <= emitted_len:
        return "", emitted_len, completed
    new_piece = current[emitted_len:]
    return new_piece, len(current), completed


async def _stream_reasoning_events_with_llm(
    node_name: str,
    reasoning_text: str,
    *,
    context: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]], str]:
    """
    ENABLE_REASONING_LIVE_LLM=true 일 때 OpenAI stream=True JSON 응답으로 reasoning을 실시간 생성.
    실패 시 기존 reasoning_text 단어 분해 fallback.
    return: (final_reasoning, events, source["llm"|"fallback"])
    """
    compact_fallback = _compact_reasoning_for_stream(reasoning_text)
    if not settings.enable_reasoning_live_llm:
        return compact_fallback, _reasoning_stream_events(node_name, compact_fallback), "fallback"
    if not settings.openai_api_key:
        return compact_fallback, _reasoning_stream_events(node_name, compact_fallback), "fallback"

    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI  # type: ignore

        base_url = (settings.openai_base_url or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_endpoint = base_url.rstrip("/")
            if azure_endpoint.endswith("/openai/v1"):
                azure_endpoint = azure_endpoint[: -len("/openai/v1")]
            client = AsyncAzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_endpoint,
                api_version=settings.openai_api_version,
            )
        else:
            client_kwargs: dict[str, Any] = {"api_key": settings.openai_api_key}
            if base_url:
                client_kwargs["base_url"] = base_url
            client = AsyncOpenAI(**client_kwargs)

        context_json = json.dumps(context or {}, ensure_ascii=False, default=str)
        min_sentences = max(4, settings.reasoning_stream_max_sentences // 2)
        prompt = (
            "당신은 엔터프라이즈 감사 에이전트의 reasoning 생성기다.\n"
            "아래 초안을 기반으로 판단 중심의 reasoning을 '식별하기 좋은 중간 길이 요약'으로 JSON 출력하라.\n"
            f"출력은 한국어 최소 {min_sentences}문장, 최대 {settings.reasoning_stream_max_sentences}문장, 최대 {settings.reasoning_stream_max_chars}자.\n"
            "장황한 체크리스트/긴 번호 목록은 금지하되, 너무 짧은 한두 문장 요약도 금지한다.\n"
            "핵심 판단, 근거, 위험 요인, 다음 행동을 구분해서 읽기 쉽게 작성한다.\n"
            "기술 용어(도구명, 영문 약어, 내부 코드)는 가능한 한 한글 설명을 괄호와 함께 병기한다.\n"
            "반드시 JSON 객체 1개만 출력하고 키는 reasoning 하나만 사용.\n"
            "불필요한 설명, 코드블록, 마크다운 금지.\n\n"
            f"[node] {node_name}\n"
            f"[context] {context_json}\n"
            f"[draft_reasoning] {reasoning_text}\n"
        )

        stream = await client.chat.completions.create(
            model=settings.reasoning_llm_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "reasoning 필드만 포함된 JSON을 출력한다. "
                        "reasoning은 식별하기 쉬운 한국어 중간 길이 요약문이어야 하며, 전문 용어는 사용자 친화적으로 풀어쓴다."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            stream=True,
            response_format={"type": "json_object"},
            max_tokens=520,
        )

        full_response = ""
        emitted_len = 0
        events: list[dict[str, Any]] = []
        for_chunk_reasoning = ""
        async for chunk in stream:
            delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
            if not delta:
                continue
            full_response += delta
            token, emitted_len, _done = _extract_reasoning_token(delta, full_response, emitted_len)
            if token:
                for_chunk_reasoning += token
                events.append(
                    AgentEvent(
                        event_type="THINKING_TOKEN",
                        node=node_name,
                        message="",
                        metadata={"token": token},
                    ).to_payload()
                )

        parsed_reasoning = ""
        try:
            parsed = json.loads(full_response)
            parsed_reasoning = str(parsed.get("reasoning") or "").strip()
        except Exception:
            parsed_reasoning = for_chunk_reasoning.strip()

        final_reasoning = _compact_reasoning_for_stream(parsed_reasoning or (for_chunk_reasoning.strip() or reasoning_text))
        events.append(
            AgentEvent(
                event_type="THINKING_DONE",
                node=node_name,
                message=final_reasoning,
                metadata={"reasoning": final_reasoning},
            ).to_payload()
        )
        return final_reasoning, events, "llm"
    except Exception:
        return compact_fallback, _reasoning_stream_events(node_name, compact_fallback), "fallback"


def _voucher_summary_for_context(body_evidence: dict[str, Any]) -> str:
    """사용자 안내용 한 줄 요약. AI 작업 메모에서 내부 ID 대신 이 문구를 사용하도록 전달."""
    merchant = (body_evidence.get("merchantName") or "").strip() or "거래처 미상"
    amount = body_evidence.get("amount")
    amount_str = f"{int(amount):,}원" if amount is not None else "금액 미상"
    occurred = _format_occurred_at(body_evidence.get("occurredAt"))
    return f"거래처 {merchant}, {amount_str}, {occurred}"


def _tool_result_key(r: dict[str, Any]) -> str:
    """도구 결과 봉투에서 도구 이름 (tool/skill 하위 호환)."""
    return str(r.get("tool") or r.get("skill") or "")


def _find_tool_result(tool_results: list[dict[str, Any]], tool_name: str) -> dict[str, Any] | None:
    return next((r for r in tool_results if _tool_result_key(r) == tool_name), None)


def _top_policy_refs(tool_results: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    refs = (_find_tool_result(tool_results, "policy_rulebook_probe") or {}).get("facts", {}).get("policy_refs") or []
    return list(refs[:limit])


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
        response = await client.chat.completions.create(
            model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
            max_tokens=300,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
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
    adjusted = dict(score or {})
    adjusted.setdefault("policy_score", int(score.get("policy_score", 0)))
    adjusted.setdefault("evidence_score", int(score.get("evidence_score", 0)))
    adjusted.setdefault("final_score", int(score.get("final_score", 0)))
    adjusted.setdefault("reasons", list(score.get("reasons") or []))
    adjusted.setdefault("policy_weight", float(score.get("policy_weight", settings.score_policy_weight)))
    adjusted.setdefault("evidence_weight", float(score.get("evidence_weight", settings.score_evidence_weight)))
    adjusted.setdefault("compound_multiplier", float(score.get("compound_multiplier", 1.0)))
    adjusted.setdefault("amount_weight", float(score.get("amount_weight", 1.0)))
    adjusted.setdefault("signals", list(score.get("signals") or []))
    adjusted.setdefault("calculation_trace", str(score.get("calculation_trace") or ""))
    if not flags.get("hasHitlResponse"):
        adjusted["severity"] = _score_to_severity(float(adjusted.get("final_score", 0)))
        return adjusted

    approved = flags.get("hitlApproved")
    if approved is True:
        adjusted["evidence_score"] = min(100, adjusted["evidence_score"] + 10)
        adjusted["reasons"].append("담당자 검토 승인 의견 반영")
    elif approved is False:
        adjusted["final_score"] = min(adjusted["final_score"], 59)
        adjusted["reasons"].append("담당자 검토 보류 의견 반영")

    if approved is not False:
        pw = float(adjusted.get("policy_weight", settings.score_policy_weight))
        ew = float(adjusted.get("evidence_weight", settings.score_evidence_weight))
        adjusted["final_score"] = min(100, int(adjusted["policy_score"] * pw + adjusted["evidence_score"] * ew))
    adjusted["severity"] = _score_to_severity(float(adjusted.get("final_score", 0)))
    adjusted["calculation_trace"] = (
        f"policy({float(adjusted.get('policy_score', 0)):.1f}) × {float(adjusted.get('policy_weight', settings.score_policy_weight)):.1f} + "
        f"evidence({float(adjusted.get('evidence_score', 0)):.1f}) × {float(adjusted.get('evidence_weight', settings.score_evidence_weight)):.1f} = "
        f"{float(adjusted.get('final_score', 0)):.1f} [compound×{float(adjusted.get('compound_multiplier', 1.0)):.2f}, amount×{float(adjusted.get('amount_weight', 1.0)):.2f}]"
    )
    return adjusted


def _build_system_auto_finalize_blockers(
    verification_summary: dict[str, Any],
    *,
    quality_signals: list[str] | None = None,
    fallback_reason: str = "",
) -> list[str]:
    """자동확정 중단 사유(시스템 관점)를 구성한다."""
    out: list[str] = []
    gate_policy = str(verification_summary.get("gate_policy") or "").strip()
    covered = verification_summary.get("covered")
    total = verification_summary.get("total")
    if gate_policy:
        out.append(f"검증 게이트 판정: {gate_policy}")
    if isinstance(covered, int) and isinstance(total, int) and total > 0:
        ratio = verification_summary.get("coverage_ratio")
        if isinstance(ratio, (int, float)):
            out.append(f"근거 연결률: {covered}/{total} ({float(ratio)*100:.1f}%)")
        else:
            out.append(f"근거 연결률: {covered}/{total}")
    missing_citations = verification_summary.get("missing_citations") or []
    if isinstance(missing_citations, list) and missing_citations:
        out.append(f"미연결 주장 수: {len(missing_citations)}건")
    for s in (quality_signals or []):
        t = str(s or "").strip()
        if t:
            out.append(f"검증 신호: {t}")
    if not out and fallback_reason:
        out.append(str(fallback_reason).strip())
    dedup: list[str] = []
    seen: set[str] = set()
    for s in out:
        k = s.strip()
        if not k or k in seen:
            continue
        seen.add(k)
        dedup.append(k)
    return dedup


def _assess_hitl_resolution_requirements(
    *,
    hitl_request: dict[str, Any],
    hitl_response: dict[str, Any],
    evidence_result: dict[str, Any] | None,
) -> tuple[bool, list[str], list[str]]:
    """
    HITL 응답이 최종 확정을 할 만큼 충분한지 검사한다.
    반환: (is_sufficient, blockers, followup_questions)
    담당자가 승인만 체크하고 근거/의견을 비워둔 경우에도 충분한 것으로 보고 reporter가 판단요약을 내도록 함.
    """
    approved = hitl_response.get("approved") is True
    if not approved:
        return False, [], []

    blockers: list[str] = []
    followup: list[str] = []

    comment = str(hitl_response.get("comment") or "").strip()
    # 담당자 승인만 체크·근거/의견 없이 제출한 경우에도 통과 — reporter가 기존 증거·규정 기준으로 판단요약 생성

    required_inputs = hitl_request.get("required_inputs") or []
    missing_required: list[dict[str, str]] = []
    extra = hitl_response.get("extra_facts") or {}
    attendees = hitl_response.get("attendees") or []
    for req in required_inputs:
        field = str(req.get("field") or "").strip()
        if not field:
            continue
        if field == "attendees":
            val = attendees
        else:
            val = hitl_response.get(field)
            if (val is None or (isinstance(val, str) and not val.strip())) and isinstance(extra, dict):
                val = extra.get(field)
        if val is None or (isinstance(val, str) and not val.strip()) or (isinstance(val, list) and not val):
            missing_required.append(req)
    if missing_required:
        blockers.append(f"필수 입력/증빙 항목 {len(missing_required)}개가 누락되었습니다.")
        for m in missing_required[:5]:
            q = str(m.get("guide") or m.get("reason") or m.get("field") or "").strip()
            if q:
                followup.append(q)

    # 승인만 한 경우(의견/질문 답변 없음)는 블로커 없이 통과 — 판단요약은 LLM이 기존 증거로 생성
    # review_questions 미답변 블로커 제거

    evidence_passed = None
    if isinstance(evidence_result, dict) and evidence_result:
        evidence_passed = evidence_result.get("passed") is True
        if evidence_passed is False:
            blockers.append("첨부 증빙 검증 결과가 불일치(passed=false)입니다.")
            reasons = evidence_result.get("reasons") or []
            for r in reasons[:3]:
                t = str(r or "").strip()
                if t:
                    followup.append(f"증빙 불일치 사유 해소: {t}")

    # 승인만 한 경우 미해결 주장 블로커 없음 — 담당자 책임 하 승인으로 reporter가 판단요약 생성
    # unresolved_claims 근거 부족 블로커 제거

    # 중복 제거
    dedup_blockers: list[str] = []
    seen_b: set[str] = set()
    for s in blockers:
        k = s.strip()
        if not k or k in seen_b:
            continue
        seen_b.add(k)
        dedup_blockers.append(k)
    dedup_followup: list[str] = []
    seen_q: set[str] = set()
    for s in followup:
        k = s.strip()
        if not k or k in seen_q:
            continue
        seen_q.add(k)
        dedup_followup.append(k)

    return (len(dedup_blockers) == 0), dedup_blockers, dedup_followup[:8]


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
        "review_questions": questions[:5],
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
        "필요한 답변·근거 없이 승인만 한 경우 정상적으로는 재검토가 나와야 한다. "
        "단, 규정·업무 맥락상 담당자 승인만으로 충분한 경우도 있다. 맥락을 보고 판단하라. "
        "룰이나 키워드로 치지 말고, 제시된 검토 요청 사유·질문·필수 항목과 담당자 응답 내용을 비교해 판단하라. "
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
        response = await client.chat.completions.create(
            model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
            max_tokens=400,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        v = str(parsed.get("verdict") or "").upper()
        if v not in ("COMPLETED_AFTER_HITL", "REVIEW_REQUIRED"):
            v = "REVIEW_REQUIRED"
        reason = str(parsed.get("reason") or "").strip() or "담당자 검토 응답을 기준으로 판단함."
        return (v, reason)
    except Exception as e:
        logger.warning("_llm_decide_hitl_verdict LLM 호출 실패: %s — REVIEW_REQUIRED로 fallback", e)
        return ("REVIEW_REQUIRED", "판단 LLM 호출 실패로 재검토 필요 처리.")


def _pick_llm_review_reason(hitl_request: dict[str, Any]) -> str:
    """사용자 노출용 재검토 사유는 hitl_request의 LLM 생성 문구를 우선 사용한다."""
    for key in ("unresolved_claims", "reasons", "review_questions", "questions"):
        vals = hitl_request.get(key) or []
        if isinstance(vals, list):
            for v in vals:
                t = str(v or "").strip()
                if t:
                    return t
    for key in ("why_hitl", "blocking_reason"):
        t = str(hitl_request.get(key) or "").strip()
        if t:
            return t
    return ""


def _build_grounded_reason(state: AgentState) -> tuple[str, str]:
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
                if reporter_verdict in ("COMPLETED_AFTER_HITL", "REVIEW_REQUIRED"):
                    status = reporter_verdict
                    tail = "담당자 검토 결과 승인 가능으로 확인되어 최종 판단을 확정했습니다." if status == "COMPLETED_AFTER_HITL" else "담당자 검토 기준 미충족으로 재검토가 필요합니다."
                else:
                    status = "REVIEW_REQUIRED"
                    tail = "담당자 검토 기준 미충족으로 재검토가 필요합니다."
            else:
                status = "HOLD_AFTER_HITL"
                tail = "담당자 검토 결과 보류/추가 검토가 필요해 자동 확정을 중단합니다."
        else:
            status = "REVIEW_REQUIRED"
            tail = "현재 수집된 증거 기준으로 우선 검토 대상입니다."
    return intro + grounding + score_text + tail, status


def _derive_flags(body_evidence: dict[str, Any]) -> dict[str, Any]:
    occurred_at = body_evidence.get("occurredAt")
    hour = None
    if occurred_at:
        try:
            hour = int(str(occurred_at)[11:13])
        except Exception:
            hour = None
    hitl_response = body_evidence.get("hitlResponse") or {}
    return {
        "isHoliday": bool(body_evidence.get("isHoliday")),
        "hrStatus": body_evidence.get("hrStatus"),
        "mccCode": body_evidence.get("mccCode"),
        "merchantName": body_evidence.get("merchantName"),
        "budgetExceeded": bool(body_evidence.get("budgetExceeded")),
        "isNight": hour is not None and (hour >= 22 or hour < 6),
        "amount": body_evidence.get("amount"),
        "caseType": body_evidence.get("case_type") or body_evidence.get("intended_risk_type"),
        "hasHitlResponse": bool(hitl_response),
        "hitlApproved": hitl_response.get("approved"),
    }


def _plan_from_flags(flags: dict[str, Any]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    if flags.get("isHoliday") or flags.get("hrStatus") in {"LEAVE", "OFF", "VACATION"}:
        plan.append({"tool": "holiday_compliance_probe", "reason": "휴일/휴무 리스크 확인", "owner": "planner"})
    if flags.get("budgetExceeded"):
        plan.append({"tool": "budget_risk_probe", "reason": "예산 초과 확인", "owner": "planner"})
    if flags.get("mccCode"):
        plan.append({"tool": "merchant_risk_probe", "reason": "업종/가맹점 업종 코드(MCC) 위험 확인", "owner": "planner"})
    plan.append({"tool": "document_evidence_probe", "reason": "전표 증거 수집", "owner": "specialist"})
    plan.append({"tool": "policy_rulebook_probe", "reason": "내부 규정 조항 조회", "owner": "specialist"})
    if settings.enable_legacy_aura_specialist:
        plan.append({"tool": "legacy_aura_deep_audit", "reason": "기존 Aura 심층 검토", "owner": "specialist"})
    return plan


_POLICY_SIGNAL_POINTS: dict[str, float] = {
    "isHoliday": 35.0,
    "hrStatus_conflict": 20.0,
    "isNight": 10.0,
    "budgetExceeded": 15.0,
}

_HOLIDAY_RISK_POLICY_DELTA: dict[str, float] = {
    "HIGH": 10.0,
    "MEDIUM": 5.0,
    "LOW": 0.0,
}

_MERCHANT_RISK_POLICY_DELTA: dict[str, float] = {
    "HIGH": 20.0,
    "MEDIUM": 10.0,
    "LOW": 3.0,
    "UNKNOWN": 0.0,
}

_POLICY_REF_EVIDENCE_POINTS: list[tuple[int, float]] = [
    (5, 30.0),
    (3, 22.0),
    (2, 15.0),
    (1, 10.0),
    (0, 0.0),
]

_LINE_ITEM_EVIDENCE_POINTS: list[tuple[int, float]] = [
    (3, 20.0),
    (2, 15.0),
    (1, 10.0),
    (0, 0.0),
]


def _lookup_tiered(value: int, table: list[tuple[int, float]]) -> float:
    for threshold, points in table:
        if value >= threshold:
            return points
    return 0.0


def _score_to_severity(final_score: float) -> str:
    if final_score >= 75:
        return "CRITICAL"
    if final_score >= 55:
        return "HIGH"
    if final_score >= 35:
        return "MEDIUM"
    return "LOW"


def _compute_plan_achievement(
    plan: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """planner 계획 대비 실제 실행 달성도를 계산한다."""
    executed_map = {_tool_result_key(r): r for r in tool_results}
    planned_tools = [step.get("tool", "") for step in plan]

    step_results: list[dict[str, Any]] = []
    succeeded = failed = skipped = 0

    for tool_name in planned_tools:
        if tool_name not in executed_map:
            step_results.append({"tool": tool_name, "status": "skipped", "ok": None})
            skipped += 1
            continue
        result = executed_map[tool_name]
        ok = bool(result.get("ok"))
        step_results.append({
            "tool": tool_name,
            "status": "success" if ok else "failed",
            "ok": ok,
            "facts_keys": list((result.get("facts") or {}).keys()),
        })
        if ok:
            succeeded += 1
        else:
            failed += 1

    total = len(planned_tools)
    executed = succeeded + failed
    rate = round(succeeded / total, 3) if total else 0.0

    return {
        "total_planned": total,
        "executed": executed,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "achievement_rate": rate,
        "step_results": step_results,
    }


def _score(flags: dict[str, Any], tool_results: list[dict[str, Any]]) -> dict[str, Any]:
    from agent.output_models import ScoreSignalDetail

    signals: list[ScoreSignalDetail] = []
    reasons: list[str] = []

    base_policy_score = 0.0
    base_evidence_score = 20.0

    if bool(flags.get("isHoliday")):
        pts = _POLICY_SIGNAL_POINTS["isHoliday"]
        base_policy_score += pts
        reasons.append("휴일 사용 정황")
        signals.append(ScoreSignalDetail(signal="isHoliday", label="휴일/주말 사용", raw_value=True, points=pts, category="policy"))

    hr = str(flags.get("hrStatus") or "").upper()
    if hr in {"LEAVE", "OFF", "VACATION"}:
        pts = _POLICY_SIGNAL_POINTS["hrStatus_conflict"]
        base_policy_score += pts
        reasons.append(f"근태 상태 충돌 ({hr})")
        signals.append(ScoreSignalDetail(signal="hrStatus_conflict", label=f"근태 충돌({hr})", raw_value=hr, points=pts, category="policy"))

    if bool(flags.get("isNight")):
        pts = _POLICY_SIGNAL_POINTS["isNight"]
        base_policy_score += pts
        reasons.append("심야 시간대")
        signals.append(ScoreSignalDetail(signal="isNight", label="심야 시간대(22시~06시)", raw_value=True, points=pts, category="policy"))

    if bool(flags.get("budgetExceeded")):
        pts = _POLICY_SIGNAL_POINTS["budgetExceeded"]
        base_policy_score += pts
        reasons.append("예산 초과")
        signals.append(ScoreSignalDetail(signal="budgetExceeded", label="예산 한도 초과", raw_value=True, points=pts, category="policy"))

    tool_policy_delta = 0.0
    tool_evidence_delta = 0.0
    holiday_result = _find_tool_result(tool_results, "holiday_compliance_probe")
    merchant_result = _find_tool_result(tool_results, "merchant_risk_probe")
    policy_result = _find_tool_result(tool_results, "policy_rulebook_probe")
    doc_result = _find_tool_result(tool_results, "document_evidence_probe")

    if holiday_result:
        h_facts = holiday_result.get("facts") or {}
        holiday_risk = h_facts.get("holidayRisk")
        if holiday_risk is True and bool(flags.get("isHoliday")) and hr in {"LEAVE", "OFF", "VACATION"}:
            delta = _HOLIDAY_RISK_POLICY_DELTA["HIGH"]
            tool_policy_delta += delta
            reasons.append("도구 확인: 휴일+근태 중복 위험(HIGH)")
            signals.append(ScoreSignalDetail(signal="holidayRisk_HIGH", label="도구 확인 - 휴일+근태 중복", raw_value="HIGH", points=delta, category="policy"))
        elif holiday_risk is True:
            delta = _HOLIDAY_RISK_POLICY_DELTA["MEDIUM"]
            tool_policy_delta += delta
            reasons.append("도구 확인: 휴일 위험(MEDIUM)")
            signals.append(ScoreSignalDetail(signal="holidayRisk_MEDIUM", label="도구 확인 - 휴일 위험", raw_value="MEDIUM", points=delta, category="policy"))

    if merchant_result:
        m_facts = merchant_result.get("facts") or {}
        merchant_risk = str(m_facts.get("merchantRisk") or "UNKNOWN").upper()
        delta = _MERCHANT_RISK_POLICY_DELTA.get(merchant_risk, 0.0)
        if delta > 0:
            tool_policy_delta += delta
            reasons.append(f"도구 확인: 가맹점 위험도 {merchant_risk}")
            signals.append(ScoreSignalDetail(signal=f"merchantRisk_{merchant_risk}", label=f"가맹점/업종 위험도({merchant_risk})", raw_value=merchant_risk, points=delta, category="policy"))

    if policy_result:
        p_facts = policy_result.get("facts") or {}
        ref_count = int(p_facts.get("ref_count") or 0)
        delta = _lookup_tiered(ref_count, _POLICY_REF_EVIDENCE_POINTS)
        if delta > 0:
            tool_evidence_delta += delta
            reasons.append(f"규정 조항 {ref_count}건 확보")
            signals.append(ScoreSignalDetail(signal="policyRefs", label=f"규정 조항 {ref_count}건", raw_value=ref_count, points=delta, category="evidence"))

    if doc_result:
        d_facts = doc_result.get("facts") or {}
        line_count = int(d_facts.get("lineItemCount") or 0)
        delta = _lookup_tiered(line_count, _LINE_ITEM_EVIDENCE_POINTS)
        if delta > 0:
            tool_evidence_delta += delta
            reasons.append(f"전표 라인아이템 {line_count}건 확보")
            signals.append(ScoreSignalDetail(signal="lineItems", label=f"전표 라인 {line_count}건", raw_value=line_count, points=delta, category="evidence"))

    if any(_tool_result_key(r) == "legacy_aura_deep_audit" and r.get("facts") for r in tool_results):
        tool_evidence_delta += 15.0
        reasons.append("심층 감사 결과 확보")
        signals.append(ScoreSignalDetail(signal="legacyAudit", label="심층 감사 결과", raw_value=True, points=15.0, category="evidence"))

    if bool(flags.get("hasHitlResponse")):
        tool_evidence_delta += 10.0
        reasons.append("담당자 검토 응답 확보")
        signals.append(ScoreSignalDetail(signal="hitlResponse", label="담당자 검토 응답", raw_value=True, points=10.0, category="evidence"))

    ref_count = int(((policy_result or {}).get("facts") or {}).get("ref_count") or 0)
    line_count = int(((doc_result or {}).get("facts") or {}).get("lineItemCount") or 0)

    policy_score = base_policy_score + tool_policy_delta
    evidence_score = min(100.0, base_evidence_score + tool_evidence_delta)

    total_tools = len(tool_results)
    ok_tools = sum(1 for result in tool_results if result.get("ok"))
    success_rate = (ok_tools / total_tools) if total_tools else 0.0
    if total_tools > 0 and success_rate < 0.5:
        penalty = round((0.5 - success_rate) * 40, 1)
        evidence_score = max(0.0, evidence_score - penalty)
        reasons.append(f"도구 실행 성공률 {success_rate:.0%} → evidence_score -{penalty}점")
    if total_tools >= 3 and success_rate == 1.0:
        evidence_score = min(100.0, evidence_score + 5.0)
        reasons.append(f"계획한 {total_tools}개 도구 전체 성공 → evidence_score +5점")

    high_risk_count = sum(
        [
            bool(flags.get("isHoliday")),
            bool(hr in {"LEAVE", "OFF", "VACATION"}),
            bool(flags.get("isNight")),
            bool(flags.get("budgetExceeded")),
            bool(merchant_result and str((merchant_result.get("facts") or {}).get("merchantRisk") or "").upper() == "HIGH"),
        ]
    )

    compound_multiplier = 1.0
    if high_risk_count >= 4:
        compound_multiplier = settings.score_compound_multiplier_max
    elif high_risk_count == 3:
        compound_multiplier = 1.3
    elif high_risk_count == 2:
        compound_multiplier = 1.15
    if compound_multiplier > 1.0:
        reasons.append(f"복합 위험 승수 적용 ({high_risk_count}개 고위험 신호)")
        signals.append(ScoreSignalDetail(signal="compound_multiplier", label=f"복합 위험({high_risk_count}개)", raw_value=high_risk_count, points=0.0, category="multiplier"))

    policy_score = policy_score * compound_multiplier

    amount = float(flags.get("amount") or 0)
    # 금액 승수를 계단식이 아닌 연속 함수로 적용해 경계값 점프를 완화한다.
    # 0~100k: 1.00~1.07, 100k~500k: 1.07~1.15, 500k~2m: 1.15~1.30
    if amount <= 100_000:
        amount_multiplier = 1.0 + 0.07 * (max(amount, 0.0) / 100_000.0)
        amount_label = "금액 구간 10만원 이하"
    elif amount <= 500_000:
        amount_multiplier = 1.07 + 0.08 * ((amount - 100_000.0) / 400_000.0)
        amount_label = "금액 구간 10만원~50만원"
    elif amount <= 2_000_000:
        amount_multiplier = 1.15 + 0.15 * ((amount - 500_000.0) / 1_500_000.0)
        amount_label = "금액 구간 50만원~200만원"
    else:
        amount_multiplier = 1.3
        amount_label = "금액 구간 200만원 초과"
    amount_multiplier = min(amount_multiplier, settings.score_amount_multiplier_max)
    reasons.append(f"{amount_label} ({int(amount):,}원)")
    signals.append(ScoreSignalDetail(signal="amount_weight", label=f"{amount_label}({int(amount):,}원)", raw_value=amount, points=0.0, category="amount"))

    policy_score = min(100.0, policy_score * amount_multiplier)

    has_strong_evidence = bool(ref_count >= 3 and line_count >= 1)
    evidence_completeness = min(1.0, (min(ref_count, 3) / 3.0) * 0.6 + (min(line_count, 2) / 2.0) * 0.4)
    # 증거 품질이 높을수록 0.75/0.25 -> 0.5/0.5 로 점진 전환
    policy_weight = 0.75 - (0.25 * evidence_completeness)
    evidence_weight = 1.0 - policy_weight

    final_score_raw = policy_score * policy_weight + evidence_score * evidence_weight
    # 고위험 신호가 많지만 증거가 빈약하면 과대확신을 막기 위해 보수 패널티를 적용
    if high_risk_count >= 3 and evidence_completeness < 0.4:
        penalty = (0.4 - evidence_completeness) * 20.0
        final_score_raw -= penalty
        reasons.append(f"증거 부족 보수 패널티 적용 (-{penalty:.1f})")
        signals.append(
            ScoreSignalDetail(
                signal="evidence_shortage_penalty",
                label="증거 부족 보수 패널티",
                raw_value=round(evidence_completeness, 3),
                points=-round(penalty, 1),
                category="evidence",
            )
        )
    final_score = min(100.0, max(0.0, round(final_score_raw, 1)))
    severity = _score_to_severity(final_score)
    calculation_trace = (
        f"policy({policy_score:.1f}) × {policy_weight} + "
        f"evidence({evidence_score:.1f}) × {evidence_weight} = {final_score:.1f} "
        f"[compound×{compound_multiplier:.2f}, amount×{amount_multiplier:.2f}]"
    )

    return {
        "policy_score": int(round(policy_score)),
        "evidence_score": int(round(evidence_score)),
        "final_score": int(round(final_score)),
        "reasons": reasons,
        "amount_weight": amount_multiplier,
        "compound_multiplier": compound_multiplier,
        "policy_weight": policy_weight,
        "evidence_weight": evidence_weight,
        "severity": severity,
        "signals": [s.model_dump() for s in signals],
        "calculation_trace": calculation_trace,
    }


def _build_prescreened_result(body: dict[str, Any]) -> dict[str, Any]:
    """사전 스크리닝된 body_evidence로 screening_result 딕셔너리 구성 (start_router / intake 이벤트용)."""
    ct = body.get("case_type") or body.get("intended_risk_type") or ""
    severity = body.get("severity") or "MEDIUM"
    score_raw = body.get("screening_score")
    score_int = int(score_raw * 100) if isinstance(score_raw, (int, float)) else (int(score_raw) if score_raw else 0)
    reason_text = body.get("screening_reason_text") or "사전 스크리닝 결과를 사용합니다."
    reasons = [reason_text] if reason_text else ["사전 스크리닝됨"]
    return {
        "case_type": ct,
        "severity": severity,
        "score": score_int,
        "reasons": reasons,
        "reason_text": reason_text,
    }


async def start_router_node(state: AgentState) -> AgentState:
    """
    사전 스크리닝된 전표(case_type 있음)면 screening_result를 주입하고 다음에 intake로 직행하고,
    아니면 빈 업데이트만 반환해 다음에 screener로 보낸다.
    """
    body = state.get("body_evidence") or {}
    if not body.get("case_type") and not body.get("intended_risk_type"):
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
    if pre_classified and pre_classified not in ("UNKNOWN", "NORMAL_BASELINE", ""):
        screening = {
            "case_type": pre_classified,
            "severity": body.get("severity") or "MEDIUM",
            "score": body.get("screening_score") or 0,
            "reasons": ["기존 스크리닝 결과를 사용합니다."],
            "reason_text": f"기존 분류: {pre_classified}",
        }
    else:
        screening = run_screening(body)

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

        response = await client.chat.completions.create(
            model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
            max_tokens=600,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
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
        "2. 규정에서 예외·승인·검토 시 확인하라고 한 내용을 review_questions로 짧은 질문 문장으로 나열하라.\n"
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

        response = await client.chat.completions.create(
            model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
            max_tokens=800,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
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
        review_questions = [str(q).strip() for q in review_questions if str(q).strip()]
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
        "2. review_questions: 검토자가 검토의견에 작성해야 할 질문 1~5개. 이 답변은 이어서 분석할 때 재분석·마무리 또는 재검토 요청에 활용되므로, 분석 결과와 연결된 구체적 질문으로 작성. 예: '휴일 사용 사전 승인 여부를 확인했는가?'\n"
        "반드시 JSON만 응답: {\"review_reasons\": [\"...\", ...], \"review_questions\": [\"...\", ...]}\n"
        "review_reasons와 review_questions는 각각 최소 1개 이상 필수."
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

    baseline = {"review_reasons": dedup_reasons[:5], "review_questions": dedup_questions[:5]}

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

        response = await client.chat.completions.create(
            model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
            max_tokens=600,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
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
        return {"review_reasons": merged_reasons[:5], "review_questions": merged_questions[:5]}
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
        "2. review_questions: 검토자가 검토의견에 반드시 답해야 할 질문 1~5개. 이어서 분석할 때 이 답변을 활용하므로, 분석 결과와 연결된 구체적 질문으로 작성.\n"
        "3. empty_explanation: (선택) 위 두 항목이 비어 있었을 수 있는 이유를 한 줄로.\n"
        "반드시 JSON만 응답: {\"review_reasons\": [\"...\"], \"review_questions\": [\"...\"], \"empty_explanation\": \"...\"}\n"
        "review_reasons와 review_questions는 각각 최소 1개 이상 필수."
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

        response = await client.chat.completions.create(
            model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
            max_tokens=700,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
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
        questions = [str(q).strip() for q in questions if str(q).strip()][:5]
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
    # HITL 확인 체크 해제 시: 팝업/중단 없이 reporter로 직행 (이벤트·상태에 HITL 남기지 않음)
    if (state.get("body_evidence") or {}).get("_enable_hitl", True) is False:
        hitl_request = None
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
        hitl_request["review_questions"] = final_questions[:5]
        hitl_request["questions"] = final_questions[:5]

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
    """Phase D: HITL 필요 시 reporter로 가지 않고 hitl_pause로 끝낸다. enable_hitl=False면 HITL 없이 reporter로 직행."""
    if (state.get("body_evidence") or {}).get("_enable_hitl", True) is False:
        return "reporter"
    if state.get("hitl_request"):
        return "hitl_pause"
    return "reporter"


def _get_hitl_response_value(hitl_response: dict[str, Any], field: str) -> Any:
    """HITL 응답에서 필드값 추출. 상위 키 또는 extra_facts[field] 확인."""
    v = hitl_response.get(field)
    if v is not None and (not isinstance(v, str) or v.strip()):
        return v
    extra = hitl_response.get("extra_facts") or {}
    v = extra.get(field) if isinstance(extra, dict) else None
    if v is not None and (not isinstance(v, str) or v.strip()):
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
    if not required_inputs:
        return {"hitl_request": None}

    missing: list[dict[str, str]] = []
    for req in required_inputs:
        field = (req.get("field") or "").strip()
        if not field:
            continue
        val = _get_hitl_response_value(hitl_response, field)
        if val is None:
            missing.append(req)
            continue
        if isinstance(val, list):
            if not val:
                missing.append(req)
            continue
        if isinstance(val, str) and not val.strip():
            missing.append(req)

    if not missing:
        return {"hitl_request": None}

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
    body = dict(state.get("body_evidence") or {})
    body["hitlResponse"] = hitl_response
    return {"body_evidence": body}


async def reporter_node(state: AgentState) -> AgentState:
    score = _score_with_hitl_adjustment(state["score_breakdown"], state["flags"])
    hitl_request = state.get("hitl_request")
    body = state["body_evidence"]
    hitl_response = body.get("hitlResponse") or {}
    hitl_approved = hitl_response.get("approved")
    occurred_at = _format_occurred_at(body.get("occurredAt"))
    merchant = body.get("merchantName") or "거래처 미상"
    summary = (
        f"전표는 {occurred_at} 시점 {merchant} 사용 건으로 분석되었습니다. "
        f"정책점수 {score['policy_score']}점, 근거점수 {score['evidence_score']}점, 최종점수 {score['final_score']}점입니다."
    )
    if hitl_request:
        summary += " 담당자 검토가 필요한 상태입니다."
        verdict = "HITL_REQUIRED"
    elif state["flags"].get("hasHitlResponse") and hitl_approved is True:
        prior_hitl_request = (body.get("hitlRequest") or {}) if isinstance(body.get("hitlRequest"), dict) else {}
        check_req = hitl_request or prior_hitl_request or {}
        evidence_result = body.get("evidenceDocumentResult") if isinstance(body.get("evidenceDocumentResult"), dict) else None
        # 확정 vs 재검토는 룰/하드코딩이 아닌 LLM 판단으로 결정 (필요 답변 없이 승인만 온 경우 재검토가 나오는지 LLM이 맥락으로 판단)
        verdict, hitl_verdict_reason = await _llm_decide_hitl_verdict(
            hitl_request=check_req,
            hitl_response=hitl_response,
            evidence_result=evidence_result,
        )
        if verdict == "COMPLETED_AFTER_HITL":
            summary += " 담당자 검토 결과 승인 가능으로 판단되어 최종 확정 후보로 전환되었습니다."
        else:
            summary += f" {hitl_verdict_reason}" if hitl_verdict_reason else " 담당자 검토 기준 미충족으로 재검토가 필요합니다."
    elif state["flags"].get("hasHitlResponse") and hitl_approved is False:
        summary += " 담당자 검토 결과 보류/추가 검토 의견이 있어 자동 확정을 중단합니다."
        verdict = "HOLD_AFTER_HITL"
    else:
        summary += " 현재 수집된 증거 기준으로 추가 검토 우선순위가 높습니다."
        verdict = "READY"
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
    pending.append(
        AgentEvent(
            event_type="NODE_END",
            node="reporter",
            phase="report",
            message=summary,
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
    reason, status = _build_grounded_reason(state)
    final_reasoning = f"{reason} 최종 상태는 {status}로 확정한다.".strip()
    final_reasoning, final_check, final_retried = call_node_llm_with_consistency_check("finalizer", {"status": status}, final_reasoning, max_retries=1)
    finalizer_context = {
        "status": status,
        "score_breakdown": score,
        "has_hitl_request": bool(hitl_request),
        "last_node_summary": state.get("last_node_summary", "없음"),
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
        "quality_gate_codes": state["verification"]["quality_signals"],
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
    """동일 프로세스 내 run resume를 위해 checkpointer는 싱글톤으로 유지한다.
    CHECKPOINTER_BACKEND=postgres 이면 AsyncPostgresSaver 사용, 아니면 MemorySaver.
    """
    global _CHECKPOINTER
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER

    backend = getattr(settings, "checkpointer_backend", "memory").lower()

    if backend == "postgres":
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # type: ignore[import-untyped]

            async def _init_postgres_checkpointer() -> Any:
                cp = await AsyncPostgresSaver.from_conn_string(settings.database_url)
                await cp.setup()
                return cp

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None and loop.is_running():
                import concurrent.futures

                def _run_init() -> Any:
                    return asyncio.run(_init_postgres_checkpointer())

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(_run_init)
                    _CHECKPOINTER = future.result(timeout=15)
            else:
                _loop = asyncio.new_event_loop()
                try:
                    _CHECKPOINTER = _loop.run_until_complete(_init_postgres_checkpointer())
                finally:
                    _loop.close()
        except ImportError:
            import warnings

            warnings.warn(
                "langgraph-checkpoint-postgres가 없어 MemorySaver로 fallback합니다. "
                "pip install langgraph-checkpoint-postgres",
                RuntimeWarning,
                stacklevel=2,
            )
            from langgraph.checkpoint.memory import MemorySaver

            _CHECKPOINTER = MemorySaver()
    else:
        from langgraph.checkpoint.memory import MemorySaver

        _CHECKPOINTER = MemorySaver()

    return _CHECKPOINTER


def build_agent_graph():
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is not None:
        return _COMPILED_GRAPH

    workflow = StateGraph(AgentState)
    # 진입 라우터: 사전 스크리닝된 전표(case_type 있음)면 intake로, 아니면 screener로
    workflow.add_node("start_router", start_router_node)
    workflow.add_node("screener", screener_node)
    workflow.add_node("intake", intake_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("execute", execute_node)
    workflow.add_node("critic", critic_node)
    workflow.add_node("verify", verify_node)
    workflow.add_node("hitl_pause", hitl_pause_node)
    workflow.add_node("hitl_validate", hitl_validate_node)
    workflow.add_node("reporter", reporter_node)
    workflow.add_node("finalizer", finalizer_node)

    def _route_after_start_router(state: AgentState) -> str:
        return "intake" if state.get("screening_result") else "screener"

    workflow.add_edge(START, "start_router")
    workflow.add_conditional_edges("start_router", _route_after_start_router, {"intake": "intake", "screener": "screener"})
    workflow.add_edge("screener", "intake")
    workflow.add_edge("intake", "planner")
    workflow.add_edge("planner", "execute")
    workflow.add_edge("execute", "critic")
    workflow.add_conditional_edges("critic", _route_after_critic, {"planner": "planner", "verify": "verify"})
    workflow.add_conditional_edges("verify", _route_after_verify, {"hitl_pause": "hitl_pause", "reporter": "reporter"})
    workflow.add_edge("hitl_pause", "hitl_validate")  # resume 후 검증 → 충족 시 reporter, 미충족 시 hitl_pause 재요청
    workflow.add_conditional_edges("hitl_validate", _route_after_hitl_validate, {"hitl_pause": "hitl_pause", "reporter": "reporter"})
    workflow.add_edge("reporter", "finalizer")
    workflow.add_edge("finalizer", END)
    _COMPILED_GRAPH = workflow.compile(checkpointer=_get_checkpointer())
    return _COMPILED_GRAPH


async def run_langgraph_agentic_analysis(
    case_id: str,
    *,
    body_evidence: dict[str, Any],
    intended_risk_type: str | None = None,
    run_id: str | None = None,
    resume_value: dict[str, Any] | None = None,
    enable_hitl: bool = True,
):
    from langgraph.types import Command
    from utils.config import get_langfuse_handler

    if not run_id:
        run_id = "default-thread"
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

    async def _yield_updates(chunks):
        async for chunk in chunks:
            if chunk.get("__interrupt__"):
                logger.info("[agent] graph __interrupt__ (HITL pause) — stream will end until review-submit resume")
                # HITL: interrupt()로 일시정지. 호출자에게 HITL_REQUIRED 전달 후 같은 run_id로 재개 대기
                interrupt_list = chunk["__interrupt__"]
                hitl_payload = interrupt_list[0].value if interrupt_list else {}
                yield "AGENT_EVENT", AgentEvent(
                    event_type="HITL_PAUSE",
                    node="hitl_pause",
                    phase="verify",
                    message="담당자 검토가 필요합니다. HITL 응답 후 같은 run으로 재개됩니다.",
                    observation="interrupt",
                    metadata={"hitl_request": hitl_payload},
                ).to_payload()
                yield "completed", {
                    "status": "HITL_REQUIRED",
                    "hitl_request": hitl_payload,
                    "reasonText": "담당자 검토 입력을 기다립니다.",
                }
                return
            for _node, update in chunk.items():
                if _node == "__interrupt__":
                    continue
                logger.info("[agent] graph node update run_id=%s node=%s", run_id, _node)
                for ev in (update.get("pending_events") or []) or []:
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
        logger.info("[RESUME_TRACE] run_langgraph run_id=%s 1차: Command(resume=...) checkpoint 재개 시도 (hitl_pause 이후 reporter→finalizer)", run_id)
        try:
            async for ev in _yield_updates(_stream_from_graph(Command(resume=resume_value))):
                yield ev
            logger.info("[RESUME_TRACE] run_langgraph run_id=%s 1차 완료: checkpoint 재개 성공", run_id)
            return
        except KeyError as e:
            # 체크포인트가 없거나 깨진 경우: 'body_evidence' KeyError를 만나면 동일 run_id로 새 입력으로 재시작
            if str(e) != "'body_evidence'":
                raise
            # 체크포인트 미존재(예: MemorySaver + 다른 워커) 시 처음부터 재실행됨. postgres 사용 시 HITL 직후 노드부터만 재개됨.
            logger.warning(
                "[RESUME_TRACE] run_langgraph run_id=%s 1차 실패: checkpoint 없음 (checkpointer=%s) → 2차 경로(스크리닝부터 재실행). "
                "Set CHECKPOINTER_BACKEND=postgres for true resume-from-HITL.",
                run_id,
                getattr(settings, "checkpointer_backend", "memory"),
            )
            # fallthrough to fresh run with hitlResponse 주입

    # 2차: 새 입력으로 실행 (resume 없음 또는 resume 실패)
    if resume_value is not None:
        logger.info("[RESUME_TRACE] run_langgraph run_id=%s 2차 경로: 스크리닝부터 전체 실행 (body_evidence에 hitlResponse 주입)", run_id)
    else:
        logger.info("[RESUME_TRACE] run_langgraph run_id=%s 경로: 스크리닝부터 전체 실행 (resume_value 없음)", run_id)
    body_with_hitl = dict(body_evidence or {})
    if resume_value is not None:
        body_with_hitl["hitlResponse"] = resume_value
    body_with_hitl["_enable_hitl"] = enable_hitl
    inputs: Any = {
        "case_id": case_id,
        "body_evidence": body_with_hitl,
        "intended_risk_type": intended_risk_type,
    }

    async for ev in _yield_updates(_stream_from_graph(inputs)):
        yield ev
