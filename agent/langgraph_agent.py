from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent.event_schema import AgentEvent
from agent.hitl import build_hitl_request
from agent.output_models import (
    Citation,
    ClaimVerificationResult,
    CriticOutput,
    PlanStep,
    PlannerOutput,
    ReporterOutput,
    ReporterSentence,
    VerifierGate,
    VerifierOutput,
)
from agent.reasoning_notes import extract_reasoning
from agent.screener import run_screening
from agent.skills import get_langchain_tools
from agent.tool_schemas import SkillContextInput
from utils.config import settings

# Phase C: plan 기반 도구 실행은 LangChain tool 호출로만 수행 (registry direct dispatch 제거)
_TOOLS_BY_NAME: dict[str, Any] = {}
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


@dataclass
class ConsistencyCheckResult:
    is_consistent: bool
    conflict_description: str


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
    return text


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


def _voucher_summary_for_context(body_evidence: dict[str, Any]) -> str:
    """사용자 안내용 한 줄 요약. AI 작업 메모에서 내부 ID 대신 이 문구를 사용하도록 전달."""
    merchant = (body_evidence.get("merchantName") or "").strip() or "거래처 미상"
    amount = body_evidence.get("amount")
    amount_str = f"{int(amount):,}원" if amount is not None else "금액 미상"
    occurred = _format_occurred_at(body_evidence.get("occurredAt"))
    return f"거래처 {merchant}, {amount_str}, {occurred}"


def _find_tool_result(tool_results: list[dict[str, Any]], skill_name: str) -> dict[str, Any] | None:
    return next((result for result in tool_results if result.get("skill") == skill_name), None)


def _top_policy_refs(tool_results: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    refs = (_find_tool_result(tool_results, "policy_rulebook_probe") or {}).get("facts", {}).get("policy_refs") or []
    return list(refs[:limit])


def _should_skip_skill(step: dict[str, Any], *, state: AgentState, tool_results: list[dict[str, Any]]) -> tuple[bool, str | None]:
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


def _build_grounded_reason(state: AgentState) -> tuple[str, str]:
    score = _score_with_hitl_adjustment(state["score_breakdown"], state["flags"])
    hitl_request = state.get("hitl_request")
    body = state["body_evidence"]
    occurred_at = _format_occurred_at(body.get("occurredAt"))
    merchant = body.get("merchantName") or "거래처 미상"
    refs = _top_policy_refs(state.get("tool_results", []), limit=2)
    ref_labels: list[str] = []
    for ref in refs:
        article = ref.get("article") or "조항 미상"
        parent_title = ref.get("parent_title")
        label = article
        if parent_title:
            label = f"{label} ({parent_title})"
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
                status = "COMPLETED_AFTER_HITL"
                tail = "담당자 검토 결과 승인 가능으로 확인되어 최종 판단을 확정했습니다."
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
    executed_map = {r.get("skill"): r for r in tool_results}
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

    if any(r.get("skill") == "legacy_aura_deep_audit" and r.get("facts") for r in tool_results):
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


async def screener_node(state: AgentState) -> AgentState:
    """
    Phase 0 — Screening.
    Runs deterministic signal analysis on raw body_evidence to classify
    the case_type BEFORE the agent begins deep analysis.
    This mirrors the original aura-platform /detect/screen flow.
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
        ],
    }


async def intake_node(state: AgentState) -> AgentState:
    flags = _derive_flags(state["body_evidence"])
    reasoning_parts = ["전표 입력값에서 핵심 위험 지표를 추출했다."]
    if flags.get("isHoliday"):
        reasoning_parts.append("휴일 사용 정황이 감지되었다.")
    if flags.get("budgetExceeded"):
        reasoning_parts.append("예산 초과 플래그가 있다.")
    if flags.get("mccCode"):
        reasoning_parts.append("MCC 업종 코드가 있어 업종 위험 검증이 필요하다.")
    reasoning_text = " ".join(reasoning_parts)
    last_node_summary = f"intake 완료: {reasoning_text[:60]}…" if len(reasoning_text) > 60 else f"intake 완료: {reasoning_text}"
    pending: list[dict[str, Any]] = [
        AgentEvent(event_type="NODE_START", node="intake", phase="analyze", message="입력 데이터를 정규화합니다.", metadata=dict(flags)).to_payload(),
    ]
    pending.extend(_reasoning_stream_events("intake", reasoning_text))
    pending.append(
        AgentEvent(event_type="NODE_END", node="intake", phase="analyze", message="입력 정규화가 완료되었습니다.", metadata={"reasoning": reasoning_text, **flags}).to_payload(),
    )
    return {
        "flags": flags,
        "last_node_summary": last_node_summary,
        "pending_events": pending,
    }


async def planner_node(state: AgentState) -> AgentState:
    replan_context = state.get("replan_context")
    if replan_context:
        already_run = set(replan_context.get("previous_tool_results") or [])
        base_plan = _plan_from_flags(state["flags"])
        always_rerun = {"policy_rulebook_probe", "document_evidence_probe"}
        plan = [step for step in base_plan if step["tool"] not in already_run or step["tool"] in always_rerun]
        if not plan:
            plan = base_plan
    else:
        plan = _plan_from_flags(state["flags"])
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
    rationale = "플래그(휴일/예산/가맹점 업종 코드(MCC) 등)와 기본 조사 경로로 도구 순서를 결정했다."
    reasoning_parts = [rationale]
    if state["flags"].get("isHoliday") or state["flags"].get("hrStatus") in {"LEAVE", "OFF", "VACATION"}:
        reasoning_parts.append("isHoliday 또는 hrStatus=LEAVE가 감지되어 휴일 경비 사용 여부를 최우선 검증 경로로 설정했다.")
    if state["flags"].get("mccCode"):
        reasoning_parts.append("MCC 업종 코드가 있으므로 merchant_risk_probe를 실행해 업종 위험도를 확인한다.")
    if not state["flags"].get("budgetExceeded"):
        reasoning_parts.append("budgetExceeded=False이므로 budget_probe는 생략한다.")
    reasoning_parts.append(f"선택된 도구 순서: {', '.join(tool_sequence)}.")
    reasoning_text = " ".join(reasoning_parts)
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
        AgentEvent(event_type="NODE_START", node="planner", phase="plan", message="조사 계획을 수립합니다.", metadata={"plan": plan}).to_payload(),
    ]
    pending.extend(_reasoning_stream_events("planner", reasoning_text))
    pending.append(
        AgentEvent(
            event_type="PLAN_READY",
            node="planner",
            phase="plan",
            message="조사 계획이 확정되었습니다.",
            metadata={"plan": plan, "reasoning": reasoning_text},
        ).to_payload(),
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
    pending_events: list[dict[str, Any]] = []
    for step in state["plan"]:
        tool_name = step.get("tool", "")
        skip, reason = _should_skip_skill(step, state=state, tool_results=tool_results)
        if skip:
            tool_obj = tools_by_name.get(tool_name)
            tool_description = getattr(tool_obj, "description", None) if tool_obj else None
            msg = reason or "기존 증거가 충분해 생략한다."
            pending_events.append(
                AgentEvent(
                    event_type="TOOL_SKIPPED",
                    node="executor",
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
            tool_results.append({"skill": tool_name, "ok": False, "facts": {}, "summary": f"알 수 없는 도구: {tool_name}"})
            continue
        tool_description = getattr(tool, "description", None) or ""
        step_reason = step.get("reason", "")
        pending_events.append(
            AgentEvent(
                event_type="TOOL_CALL",
                node="executor",
                phase="execute",
                tool=tool_name,
                message=f"{step_reason} — {tool_name} 실행.",
                thought=step_reason,
                action=f"{tool_name} 실행",
                observation="도구 실행 중.",
                metadata={"reason": step_reason, "owner": step.get("owner"), "tool_description": tool_description},
            ).to_payload()
        )
        inp = SkillContextInput(
            case_id=state["case_id"],
            body_evidence=state["body_evidence"],
            intended_risk_type=state.get("intended_risk_type"),
            prior_tool_results=list(tool_results),
        )
        result = await tool.ainvoke(inp.model_dump())
        if not isinstance(result, dict):
            result = {"skill": tool_name, "ok": False, "facts": {}, "summary": str(result)}
        tool_results.append(result)
        result_summary = result.get("summary") or "도구 결과 수집 완료"
        pending_events.append(
            AgentEvent(
                event_type="TOOL_RESULT",
                node="executor",
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
        "node": "executor",
        "phase": "execute",
        "metadata": score,
    })
    plan_achievement = _compute_plan_achievement(state.get("plan") or [], tool_results)
    return {
        "tool_results": tool_results,
        "score_breakdown": score,
        "pending_events": pending_events,
        "plan_achievement": plan_achievement,
        "last_node_summary": f"execute 완료: {len(tool_results)}개 도구 실행",
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
    legacy = next((r for r in state.get("tool_results", []) if r.get("skill") == "legacy_aura_deep_audit"), None)
    missing = ((state["body_evidence"].get("dataQuality") or {}).get("missingFields") or [])
    critique = {
        "has_legacy_result": bool(legacy and legacy.get("facts")),
        "missing_fields": missing,
        "risk_of_overclaim": bool(missing),
        "recommend_hold": bool(missing and not state["flags"].get("hasHitlResponse")),
    }
    loop_count = state.get("critic_loop_count") or 0
    replan_required = bool(
        critique["risk_of_overclaim"]
        and not state["flags"].get("hasHitlResponse")
        and loop_count < _MAX_CRITIC_LOOP
    )
    replan_reason = ""
    replan_context: dict[str, Any] | None = None
    if replan_required:
        replan_reason = (
            f"누락 필드 {missing}로 인해 과잉 주장 위험이 감지되었습니다. "
            "재조사 시 해당 필드를 보완하는 도구를 우선 실행하십시오."
        )
        replan_context = {
            "critic_feedback": replan_reason,
            "missing_fields": missing,
            "loop_count": loop_count + 1,
            "previous_tool_results": [r.get("skill") for r in state.get("tool_results", [])],
        }
    verification_targets = _build_verification_targets(state)
    critic_output = CriticOutput(
        overclaim_risk=critique["risk_of_overclaim"],
        contradictions=[],
        missing_counter_evidence=missing,
        recommend_hold=critique["recommend_hold"],
        rationale="입력 누락 필드가 있으면 과잉 주장 위험이 있어 보류를 권고한다." if missing else "추가 보류 조건 없이 진행 가능하다.",
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
    check = check_reasoning_consistency("critic", {**critic_output.model_dump(), "reasoning": reasoning_text})
    if not check.is_consistent:
        reasoning_text = _repair_reasoning_for_consistency("critic", critic_output, current_reasoning=reasoning_text)
    critic_output_dict = critic_output.model_dump()
    critic_output_dict["reasoning"] = reasoning_text
    last_node_summary = f"critic 완료: {reasoning_text[:60]}…" if len(reasoning_text) > 60 else f"critic 완료: {reasoning_text}"
    pending: list[dict[str, Any]] = [
        AgentEvent(event_type="NODE_START", node="critic", phase="reflect", message="전문 도구 결과와 입력 품질을 교차 검토합니다.", metadata={}).to_payload(),
    ]
    if not check.is_consistent:
        pending.append(
            AgentEvent(
                event_type="THINKING_RETRY",
                node="critic",
                phase="reflect",
                message="추론 정합성 불일치 감지 — 재검토 후 문구를 보정합니다.",
                metadata={"conflict": check.conflict_description},
            ).to_payload()
        )
    pending.extend(_reasoning_stream_events("critic", reasoning_text))
    pending.append(
        AgentEvent(
            event_type="NODE_END",
            node="critic",
            phase="reflect",
            message="비판적 재검토가 완료되었습니다.",
            metadata={"reasoning": reasoning_text, **critique},
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


async def verify_node(state: AgentState) -> AgentState:
    from services.evidence_verification import EVIDENCE_GATE_HOLD, EVIDENCE_GATE_REGENERATE, verify_evidence_coverage_claims

    verification = {"needs_hitl": False, "quality_signals": ["OK"]}
    verification_targets = (state.get("critic_output") or {}).get("verification_targets") or []
    probe_facts = (_find_tool_result(state["tool_results"], "policy_rulebook_probe") or {}).get("facts", {}) or {}
    retrieved_chunks = probe_facts.get("retrieval_candidates") or probe_facts.get("policy_refs") or []
    verification_summary: dict[str, Any] = {}
    if verification_targets and retrieved_chunks:
        verification_summary = verify_evidence_coverage_claims(verification_targets, retrieved_chunks)
    elif verification_targets:
        verification_summary = {"covered": 0, "total": len(verification_targets), "coverage_ratio": 0.0, "details": [], "gate_policy": EVIDENCE_GATE_HOLD, "missing_citations": verification_targets}
    verification["verification_summary"] = verification_summary
    hitl_request = build_hitl_request(
        state["body_evidence"],
        state["tool_results"],
        critique=state.get("critique"),
        verification_summary=verification_summary,
        screening_result=state.get("screening_result"),
        score_breakdown=state.get("score_breakdown"),
    )
    if state["flags"].get("hasHitlResponse"):
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
    check = check_reasoning_consistency("verify", {**verifier_output.model_dump(), "reasoning": reasoning_text})
    if not check.is_consistent:
        reasoning_text = _repair_reasoning_for_consistency("verify", verifier_output, current_reasoning=reasoning_text)
    verifier_output_dict = verifier_output.model_dump()
    verifier_output_dict["reasoning"] = reasoning_text
    events: list[dict[str, Any]] = [
        AgentEvent(event_type="NODE_START", node="verifier", phase="verify", message="근거 정합성과 추가 검토 필요 여부를 확인합니다.", metadata={}).to_payload(),
    ]
    if not check.is_consistent:
        events.append(
            AgentEvent(
                event_type="THINKING_RETRY",
                node="verifier",
                phase="verify",
                message="추론 정합성 불일치 감지 — 재검토 후 문구를 보정합니다.",
                metadata={"conflict": check.conflict_description},
            ).to_payload()
        )
    events.extend(_reasoning_stream_events("verifier", reasoning_text))
    events.append(
        AgentEvent(
            event_type="GATE_APPLIED",
            node="verifier",
            phase="verify",
            message="검증 게이트 적용이 완료되었습니다.",
            decision_code="HITL_REQUIRED" if needs_hitl else "READY",
            observation="담당자 검토 필요" if needs_hitl else "자동 진행 가능",
            metadata={**verification},
        ).to_payload(),
    )
    if hitl_request:
        events.append(
            AgentEvent(
                event_type="HITL_REQUESTED",
                node="verifier",
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
    """Phase D: HITL 필요 시 reporter로 가지 않고 hitl_pause로 끝낸다."""
    if state.get("hitl_request"):
        return "hitl_pause"
    return "reporter"


async def hitl_pause_node(state: AgentState) -> AgentState:
    """Phase D: HITL 필요 시 interrupt()로 중단. resume 시 hitl_response를 body_evidence에 반영하고 reporter로 이어짐."""
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
        summary += " 담당자 검토 결과 승인 가능으로 판단되어 최종 확정 후보로 전환되었습니다."
        verdict = "COMPLETED_AFTER_HITL"
    elif state["flags"].get("hasHitlResponse") and hitl_approved is False:
        summary += " 담당자 검토 결과 보류/추가 검토 의견이 있어 자동 확정을 중단합니다."
        verdict = "HOLD_AFTER_HITL"
    else:
        summary += " 현재 수집된 증거 기준으로 추가 검토 우선순위가 높습니다."
        verdict = "READY"
    refs = _top_policy_refs(state.get("tool_results", []), limit=5)
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
    check = check_reasoning_consistency("reporter", {"verdict": verdict, "reasoning": reasoning_text})
    if not check.is_consistent:
        reasoning_text = _repair_reasoning_for_consistency("reporter", {"verdict": verdict}, current_reasoning=reasoning_text)
    reporter_output = ReporterOutput(summary=summary, verdict=verdict, sentences=sentences_list, reasoning=reasoning_text)
    reporter_output_dict = reporter_output.model_dump()
    last_node_summary = f"reporter 완료: {reasoning_text[:60]}…" if len(reasoning_text) > 60 else f"reporter 완료: {reasoning_text}"
    pending: list[dict[str, Any]] = [
        AgentEvent(event_type="NODE_START", node="reporter", phase="report", message="사용자에게 제시할 보고 문안을 구성합니다.", metadata={}).to_payload(),
    ]
    if not check.is_consistent:
        pending.append(
            AgentEvent(
                event_type="THINKING_RETRY",
                node="reporter",
                phase="report",
                message="추론 정합성 불일치 감지 — 재검토 후 문구를 보정합니다.",
                metadata={"conflict": check.conflict_description},
            ).to_payload()
        )
    pending.extend(_reasoning_stream_events("reporter", reasoning_text))
    pending.append(
        AgentEvent(
            event_type="NODE_END",
            node="reporter",
            phase="report",
            message=summary,
            metadata={"summary": summary, "verdict": verdict},
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
        "critic_output": state.get("critic_output"),
        "verifier_output": state.get("verifier_output"),
        "reporter_output": state.get("reporter_output"),
        "retrieval_snapshot": retrieval_snapshot,
        "verification_summary": (state.get("verification") or {}).get("verification_summary"),
    }
    return {
        "final_result": final,
        "pending_events": [
            AgentEvent(event_type="NODE_END", node="finalizer", phase="finalize", message="최종 분석 결과가 생성되었습니다.", observation=f"최종 상태={status}", metadata={"status": status}).to_payload(),
        ],
    }


def _get_checkpointer():
    """동일 프로세스 내 run resume를 위해 checkpointer는 싱글톤으로 유지한다."""
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        from langgraph.checkpoint.memory import MemorySaver

        _CHECKPOINTER = MemorySaver()
    return _CHECKPOINTER


def build_agent_graph():
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is not None:
        return _COMPILED_GRAPH

    workflow = StateGraph(AgentState)
    # Phase 0: Screening (case type classification from raw signals)
    workflow.add_node("screener", screener_node)
    # Phase 1-7: Deep analysis
    workflow.add_node("intake", intake_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("execute", execute_node)
    workflow.add_node("critic", critic_node)
    workflow.add_node("verify", verify_node)
    workflow.add_node("hitl_pause", hitl_pause_node)  # Phase D: HITL 시 run 조기 종료
    workflow.add_node("reporter", reporter_node)
    workflow.add_node("finalizer", finalizer_node)
    workflow.add_edge(START, "screener")
    workflow.add_edge("screener", "intake")
    workflow.add_edge("intake", "planner")
    workflow.add_edge("planner", "execute")
    workflow.add_edge("execute", "critic")
    workflow.add_conditional_edges("critic", _route_after_critic, {"planner": "planner", "verify": "verify"})
    workflow.add_conditional_edges("verify", _route_after_verify, {"hitl_pause": "hitl_pause", "reporter": "reporter"})
    workflow.add_edge("hitl_pause", "reporter")  # resume 후 reporter로 이어짐
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
        try:
            async for ev in _yield_updates(_stream_from_graph(Command(resume=resume_value))):
                yield ev
            return
        except KeyError as e:
            # 체크포인트가 없거나 깨진 경우: 'body_evidence' KeyError를 만나면 동일 run_id로 새 입력으로 재시작
            if str(e) != "'body_evidence'":
                raise
            # fallthrough to fresh run with hitlResponse 주입

    # 2차: 새 입력으로 실행 (resume 없음 또는 resume 실패)
    body_with_hitl = dict(body_evidence or {})
    if resume_value is not None:
        body_with_hitl["hitlResponse"] = resume_value
    inputs: Any = {
        "case_id": case_id,
        "body_evidence": body_with_hitl,
        "intended_risk_type": intended_risk_type,
    }

    async for ev in _yield_updates(_stream_from_graph(inputs)):
        yield ev
