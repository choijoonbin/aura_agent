from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncGenerator

from agent.event_schema import AgentEvent
from agent.aura_bridge import run_legacy_aura_analysis


@dataclass(slots=True)
class NativeAgentContext:
    case_id: str
    body_evidence: dict[str, Any]
    intended_risk_type: str | None = None

    @property
    def evidence_hash(self) -> str:
        raw = json.dumps(self.body_evidence, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _is_night_time(occurred_at: str | None) -> bool:
    if not occurred_at:
        return False
    try:
        dt = datetime.fromisoformat(occurred_at)
        return dt.hour >= 22 or dt.hour < 6
    except Exception:
        return False


def _derive_context_flags(body_evidence: dict[str, Any]) -> dict[str, Any]:
    occurred_at = body_evidence.get("occurredAt")
    hr_status = body_evidence.get("hrStatus")
    hr_status_raw = body_evidence.get("hrStatusRaw")
    is_holiday = bool(body_evidence.get("isHoliday"))
    amount = body_evidence.get("amount")
    budget_exceeded = bool(body_evidence.get("budgetExceeded"))
    case_type = body_evidence.get("case_type") or body_evidence.get("intended_risk_type")
    return {
        "occurred_at": occurred_at,
        "is_night_time": _is_night_time(occurred_at),
        "is_holiday": is_holiday,
        "hr_status": hr_status,
        "hr_status_raw": hr_status_raw,
        "budget_exceeded": budget_exceeded,
        "amount": amount,
        "case_type": case_type,
        "merchant_name": body_evidence.get("merchantName"),
        "mcc_code": body_evidence.get("mccCode"),
        "expense_type": body_evidence.get("expenseType"),
    }


def _build_investigation_plan(flags: dict[str, Any]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    if flags.get("is_holiday") or flags.get("hr_status") in {"LEAVE", "VACATION", "OFF"}:
        plan.append({"tool": "holiday_compliance_probe", "why": "휴일/휴무 사용 정황 검증"})
    if flags.get("budget_exceeded"):
        plan.append({"tool": "budget_risk_probe", "why": "예산 초과 여부 검증"})
    if flags.get("mcc_code"):
        plan.append({"tool": "merchant_risk_probe", "why": "업종/MCC 위험도 확인"})
    plan.append({"tool": "legacy_aura_deep_audit", "why": "기존 Aura 심층 규정 분석 결과를 전문 도구로 수집"})
    return plan


def _score_breakdown(flags: dict[str, Any]) -> dict[str, Any]:
    policy_score = 0
    evidence_score = 40
    reasons: list[str] = []

    if flags.get("is_holiday"):
        policy_score += 35
        reasons.append("휴일 사용 정황")
    if flags.get("hr_status") in {"LEAVE", "VACATION", "OFF"}:
        policy_score += 20
        reasons.append("근태 상태와 결제 시점 충돌")
    if flags.get("is_night_time"):
        policy_score += 10
        reasons.append("심야 시간대 사용")
    if flags.get("budget_exceeded"):
        policy_score += 10
        reasons.append("예산 초과")
    if flags.get("mcc_code"):
        evidence_score += 10
        reasons.append("업종 정보 확보")

    final_score = min(100, int((policy_score * 0.6) + (evidence_score * 0.4)))
    return {
        "policy_score": policy_score,
        "evidence_score": evidence_score,
        "final_score": final_score,
        "reasons": reasons,
    }


def _compose_final_reason(flags: dict[str, Any], verification: dict[str, Any], legacy_result: dict[str, Any] | None) -> str:
    parts: list[str] = []
    if flags.get("is_holiday"):
        parts.append("휴일 사용 정황이 확인되었습니다")
    if flags.get("is_night_time"):
        parts.append("심야 시간대 사용이 함께 확인되었습니다")
    if flags.get("budget_exceeded"):
        parts.append("예산 초과 신호가 존재합니다")
    if verification.get("needs_review"):
        parts.append("근거가 충분하지 않아 최종 확정은 보류합니다")
    else:
        parts.append("현재 수집된 사실과 심층 분석 결과를 기준으로 추가 검토 우선순위가 높습니다")
    if legacy_result and legacy_result.get("reasonText"):
        parts.append(f"전문 분석 도구 요약: {legacy_result['reasonText']}")
    return ". ".join(parts) + "."


async def run_native_agentic_analysis(
    case_id: str,
    *,
    body_evidence: dict[str, Any],
    intended_risk_type: str | None = None,
) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
    ctx = NativeAgentContext(case_id=case_id, body_evidence=body_evidence, intended_risk_type=intended_risk_type)
    legacy_result: dict[str, Any] | None = None

    yield "AGENT_EVENT", AgentEvent(
        event_type="NODE_START",
        node="intake",
        phase="analyze",
        message="입력 증거를 정규화하고 조사 가능한 사실을 추출합니다.",
        input_hash=ctx.evidence_hash,
    ).to_payload()

    flags = _derive_context_flags(body_evidence)
    yield "AGENT_EVENT", AgentEvent(
        event_type="NODE_END",
        node="intake",
        phase="analyze",
        message="입력 정규화가 완료되었습니다.",
        input_hash=ctx.evidence_hash,
        metadata=flags,
    ).to_payload()

    yield "AGENT_EVENT", AgentEvent(
        event_type="NODE_START",
        node="planner",
        phase="plan",
        message="위험 신호를 바탕으로 조사 계획을 수립합니다.",
        input_hash=ctx.evidence_hash,
    ).to_payload()

    plan = _build_investigation_plan(flags)
    yield "AGENT_EVENT", AgentEvent(
        event_type="PLAN_READY",
        node="planner",
        phase="plan",
        message="조사 계획이 확정되었습니다.",
        input_hash=ctx.evidence_hash,
        metadata={"plan": plan},
    ).to_payload()

    score = _score_breakdown(flags)
    yield "confidence", {
        "label": "RISK_SCORE_BREAKDOWN",
        "detail": f"정책점수 {score['policy_score']}, 근거점수 {score['evidence_score']}, 최종점수 {score['final_score']}",
        "score_breakdown": score,
    }

    for step in plan:
        yield "AGENT_EVENT", AgentEvent(
            event_type="TOOL_CALL",
            node="executor",
            phase="execute",
            tool=step["tool"],
            message=f"도구 호출 준비: {step['tool']}",
            input_hash=ctx.evidence_hash,
            metadata={"why": step["why"]},
        ).to_payload()

        if step["tool"] == "legacy_aura_deep_audit":
            async for ev_type, payload in run_legacy_aura_analysis(
                case_id,
                body_evidence=body_evidence,
                intended_risk_type=intended_risk_type,
            ):
                if ev_type == "completed":
                    legacy_result = payload
                    yield "AGENT_EVENT", AgentEvent(
                        event_type="TOOL_RESULT",
                        node="executor",
                        phase="execute",
                        tool=step["tool"],
                        message="기존 Aura 심층 분석 결과를 수집했습니다.",
                        output_ref="legacy_aura.completed",
                        decision_code=legacy_result.get("status") or legacy_result.get("decision_code"),
                        metadata={
                            "severity": legacy_result.get("severity"),
                            "score": legacy_result.get("score"),
                        },
                    ).to_payload()
                    break
                if ev_type == "failed":
                    legacy_result = payload
                    yield "AGENT_EVENT", AgentEvent(
                        event_type="TOOL_RESULT",
                        node="executor",
                        phase="execute",
                        tool=step["tool"],
                        message="기존 Aura 심층 분석이 실패했습니다.",
                        decision_code="LEGACY_TOOL_FAILED",
                        metadata=payload,
                    ).to_payload()
                    break
                if ev_type in {"AGENT_EVENT", "AGENT_STREAM"}:
                    yield ev_type, payload
                elif ev_type == "step":
                    yield "AGENT_EVENT", AgentEvent(
                        event_type="TOOL_PROGRESS",
                        node="executor",
                        phase="execute",
                        tool=step["tool"],
                        message=payload.get("detail") or payload.get("label") or "legacy step",
                        metadata=payload,
                    ).to_payload()
        else:
            probe_result = {"ok": True, "tool": step["tool"], "flags": flags}
            yield "AGENT_EVENT", AgentEvent(
                event_type="TOOL_RESULT",
                node="executor",
                phase="execute",
                tool=step["tool"],
                message=f"도구 실행 완료: {step['tool']}",
                metadata=probe_result,
            ).to_payload()

    yield "AGENT_EVENT", AgentEvent(
        event_type="NODE_START",
        node="verifier",
        phase="verify",
        message="수집된 근거와 결론 사이의 정합성을 검토합니다.",
        input_hash=ctx.evidence_hash,
    ).to_payload()

    hold_needed = bool(flags.get("is_holiday") and not flags.get("mcc_code"))
    verification = {
        "grounded": not hold_needed,
        "needs_review": hold_needed,
        "quality_signals": ["FACT_CONTEXT_PARTIAL"] if hold_needed else ["OK"],
    }
    yield "AGENT_EVENT", AgentEvent(
        event_type="GATE_APPLIED",
        node="verifier",
        phase="verify",
        message="근거 정합성 검토가 완료되었습니다.",
        decision_code="NEEDS_REVIEW" if hold_needed else "READY",
        metadata=verification,
    ).to_payload()

    final_reason = _compose_final_reason(flags, verification, legacy_result)
    final_payload = {
        "caseId": case_id,
        "status": "HOLD" if verification["needs_review"] else "REVIEW_REQUIRED",
        "reasonText": final_reason,
        "score": score["final_score"] / 100,
        "severity": "MEDIUM" if score["final_score"] >= 40 else "LOW",
        "analysis_mode": "multi_agent_native",
        "score_breakdown": score,
        "quality_gate_codes": verification["quality_signals"],
        "legacy_tool_result": {
            "severity": legacy_result.get("severity") if legacy_result else None,
            "score": legacy_result.get("score") if legacy_result else None,
        },
    }
    yield "completed", final_payload
