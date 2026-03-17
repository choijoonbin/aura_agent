from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

from agent.output_models import FallbackReason, ScoringResult
from agent.langgraph_domain import _find_tool_result, _tool_result_key
from utils.config import settings
from utils.llm_azure import completion_kwargs_for_azure

logger = logging.getLogger(__name__)

_RUBRIC_PATH = Path("docs/work_info/logic/scoring_rubric_v1.md")
_JUDGE_CIRCUIT_HISTORY: deque[bool] = deque(maxlen=max(1, int(settings.llm_judge_circuit_window)))
_JUDGE_CIRCUIT_OPEN = False
_POSITIVE_POLICY_WORDS = ("우수", "양호", "안전", "좋", "긍정", "문제없")
_NEGATIVE_POLICY_WORDS = ("위험", "위반", "불리", "주의", "우려", "문제", "경고")

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


def _score_with_hitl_adjustment(score: dict[str, Any], flags: dict[str, Any]) -> dict[str, Any]:
    adjusted = dict(score or {})
    adjusted.setdefault("policy_score", int((score or {}).get("policy_score", 0)))
    adjusted.setdefault("evidence_score", int((score or {}).get("evidence_score", 0)))
    adjusted.setdefault("final_score", int((score or {}).get("final_score", 0)))
    adjusted.setdefault("reasons", list((score or {}).get("reasons") or []))
    adjusted.setdefault("policy_weight", float((score or {}).get("policy_weight", settings.score_policy_weight)))
    adjusted.setdefault("evidence_weight", float((score or {}).get("evidence_weight", settings.score_evidence_weight)))
    adjusted.setdefault("compound_multiplier", float((score or {}).get("compound_multiplier", 1.0)))
    adjusted.setdefault("amount_weight", float((score or {}).get("amount_weight", 1.0)))
    adjusted.setdefault("signals", list((score or {}).get("signals") or []))
    adjusted.setdefault("calculation_trace", str((score or {}).get("calculation_trace") or ""))
    adjusted.setdefault("rule_score", int((score or {}).get("rule_score", adjusted.get("final_score", 0))))
    adjusted.setdefault("llm_score", int((score or {}).get("llm_score", adjusted.get("final_score", 0))))
    adjusted.setdefault("verification_gate", str((score or {}).get("verification_gate") or "pass"))
    adjusted.setdefault("final_decision", str((score or {}).get("final_decision") or "PASS"))
    adjusted.setdefault("fidelity", int((score or {}).get("fidelity", 0)))
    adjusted.setdefault("rule_fidelity", int((score or {}).get("rule_fidelity", adjusted.get("fidelity", 0))))
    adjusted.setdefault("llm_fidelity", int((score or {}).get("llm_fidelity", adjusted.get("fidelity", 0))))
    adjusted.setdefault("fallback_used", bool((score or {}).get("fallback_used", False)))
    adjusted.setdefault("fallback_reason", (score or {}).get("fallback_reason"))
    adjusted.setdefault("judge_skipped", bool((score or {}).get("judge_skipped", False)))
    adjusted.setdefault("skip_reason", (score or {}).get("skip_reason"))
    adjusted.setdefault("latency_ms", (score or {}).get("latency_ms"))
    adjusted.setdefault("summary_reason", str((score or {}).get("summary_reason") or ""))
    adjusted.setdefault("diagnostic_log", str((score or {}).get("diagnostic_log") or ""))
    adjusted.setdefault("llm_judge_enabled", bool((score or {}).get("llm_judge_enabled", False)))
    adjusted.setdefault("retry_count", int((score or {}).get("retry_count", 0)))
    adjusted.setdefault("max_retries", int((score or {}).get("max_retries", 2)))
    adjusted.setdefault("version_meta", dict((score or {}).get("version_meta") or {}))
    adjusted.setdefault("conflict_warning", bool((score or {}).get("conflict_warning", False)))
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
        adjusted["final_score"] = min(100, int(round(adjusted["policy_score"] * pw + adjusted["evidence_score"] * ew)))
    adjusted["severity"] = _score_to_severity(float(adjusted.get("final_score", 0)))
    adjusted["calculation_trace"] = (
        f"policy({float(adjusted.get('policy_score', 0)):.1f}) × {float(adjusted.get('policy_weight', settings.score_policy_weight)):.1f} + "
        f"evidence({float(adjusted.get('evidence_score', 0)):.1f}) × {float(adjusted.get('evidence_weight', settings.score_evidence_weight)):.1f} = "
        f"{float(adjusted.get('final_score', 0)):.1f} [compound×{float(adjusted.get('compound_multiplier', 1.0)):.2f}, amount×{float(adjusted.get('amount_weight', 1.0)):.2f}]"
    )
    return adjusted


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


def _to_int_score(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except Exception:
        return None


def _split_sentences(text: str) -> list[str]:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return []
    parts = re.split(r"(?<=[.!?])\s+|(?<=다\.)\s+", raw)
    return [p.strip() for p in parts if p and p.strip()]


def _strip_policy_semantic_conflict_sentences(text: str, policy_score: int | None) -> str:
    if policy_score is None:
        return str(text or "").strip()
    kept: list[str] = []
    for sentence in _split_sentences(text):
        if "정책점수" not in sentence and "policy_score" not in sentence:
            kept.append(sentence)
            continue
        lower = sentence.lower()
        has_positive = any(word in sentence for word in _POSITIVE_POLICY_WORDS)
        has_negative = any(word in sentence for word in _NEGATIVE_POLICY_WORDS)
        if policy_score >= 70 and has_positive and not has_negative:
            continue
        if policy_score <= 30 and has_negative and not has_positive:
            continue
        if "high" in lower and "good" in lower and policy_score >= 70:
            continue
        kept.append(sentence)
    return " ".join(kept).strip()


def _contains_semantics_sentence(text: str) -> bool:
    if not text:
        return False
    has_policy = bool(re.search(r"(정책점수|policy_score).{0,40}(위험|위반|불리|주의)", text))
    has_evidence = bool(re.search(r"(근거점수|evidence_score).{0,40}(근거|증거|충실|유리|부족|확보)", text))
    return has_policy and has_evidence


def _make_score_semantics_sentence(policy_score: int | None, evidence_score: int | None, final_score: int | None) -> str:
    if policy_score is None and evidence_score is None:
        return ""
    policy_txt = (
        f"정책점수 {policy_score}점은 위반/위험 신호 강도를 뜻해 점수가 높을수록 불리합니다."
        if policy_score is not None
        else "정책점수는 위반/위험 신호 강도를 뜻해 점수가 높을수록 불리합니다."
    )
    evidence_txt = (
        f" 근거점수 {evidence_score}점은 근거 충실도를 뜻해 점수가 높을수록 유리합니다."
        if evidence_score is not None
        else " 근거점수는 근거 충실도를 뜻해 점수가 높을수록 유리합니다."
    )
    final_txt = f" 현재 최종점수는 {final_score}점입니다." if final_score is not None else ""
    return f"{policy_txt}{evidence_txt}{final_txt}".strip()


def _sanitize_summary_reason(summary_reason: str, policy_score: Any, evidence_score: Any, final_score: Any) -> str:
    policy = _to_int_score(policy_score)
    evidence = _to_int_score(evidence_score)
    final = _to_int_score(final_score)
    cleaned = _strip_policy_semantic_conflict_sentences(summary_reason, policy)
    semantics = _make_score_semantics_sentence(policy, evidence, final)
    if not cleaned:
        return semantics or "규칙 기반 점수로 판정했습니다."
    # 점수 해석 문장이 없는 경우 명시적으로 추가해 역해석을 방지한다.
    if semantics and not _contains_semantics_sentence(cleaned):
        return f"{semantics} {cleaned}".strip()
    return cleaned


def _plan_from_flags(flags: dict[str, Any]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    if flags.get("isHoliday") or flags.get("hrStatus") in {"LEAVE", "OFF", "VACATION"}:
        plan.append({"tool": "holiday_compliance_probe", "reason": "휴일/휴무 리스크 확인", "owner": "planner"})
    if flags.get("budgetExceeded"):
        plan.append({"tool": "budget_risk_probe", "reason": "예산 초과 확인", "owner": "planner"})
    if flags.get("mccCode"):
        plan.append({"tool": "merchant_risk_probe", "reason": "가맹점 업종 코드 위험 확인", "owner": "planner"})
    plan.append({"tool": "document_evidence_probe", "reason": "전표 증거 수집", "owner": "specialist"})
    plan.append({"tool": "policy_rulebook_probe", "reason": "내부 규정 조항 조회", "owner": "specialist"})
    if settings.enable_legacy_aura_specialist:
        plan.append({"tool": "legacy_aura_deep_audit", "reason": "기존 Aura 심층 검토", "owner": "specialist"})
    return plan


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


def _reason_prefix(points: float) -> str:
    if points > 0:
        return "[가산] "
    if points < 0:
        return "[감점] "
    return ""


def _score(flags: dict[str, Any], tool_results: list[dict[str, Any]]) -> dict[str, Any]:
    from agent.output_models import ScoreSignalDetail

    signals: list[ScoreSignalDetail] = []
    reasons: list[str] = []

    base_policy_score = 0.0
    base_evidence_score = 20.0

    if bool(flags.get("isHoliday")):
        pts = _POLICY_SIGNAL_POINTS["isHoliday"]
        base_policy_score += pts
        reasons.append(_reason_prefix(pts) + "휴일 사용 정황")
        signals.append(ScoreSignalDetail(signal="isHoliday", label="휴일/주말 사용", raw_value=True, points=pts, category="policy"))

    hr = str(flags.get("hrStatus") or "").upper()
    if hr in {"LEAVE", "OFF", "VACATION"}:
        pts = _POLICY_SIGNAL_POINTS["hrStatus_conflict"]
        base_policy_score += pts
        reasons.append(_reason_prefix(pts) + f"근태 상태 충돌 ({hr})")
        signals.append(ScoreSignalDetail(signal="hrStatus_conflict", label=f"근태 충돌({hr})", raw_value=hr, points=pts, category="policy"))

    if bool(flags.get("isNight")):
        pts = _POLICY_SIGNAL_POINTS["isNight"]
        base_policy_score += pts
        reasons.append(_reason_prefix(pts) + "심야 시간대")
        signals.append(ScoreSignalDetail(signal="isNight", label="심야 시간대(22시~06시)", raw_value=True, points=pts, category="policy"))

    if bool(flags.get("budgetExceeded")):
        pts = _POLICY_SIGNAL_POINTS["budgetExceeded"]
        base_policy_score += pts
        reasons.append(_reason_prefix(pts) + "예산 초과")
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
            reasons.append(_reason_prefix(delta) + "도구 확인: 휴일+근태 중복 위험(HIGH)")
            signals.append(ScoreSignalDetail(signal="holidayRisk_HIGH", label="도구 확인 - 휴일+근태 중복", raw_value="HIGH", points=delta, category="policy"))
        elif holiday_risk is True:
            delta = _HOLIDAY_RISK_POLICY_DELTA["MEDIUM"]
            tool_policy_delta += delta
            reasons.append(_reason_prefix(delta) + "도구 확인: 휴일 위험(MEDIUM)")
            signals.append(ScoreSignalDetail(signal="holidayRisk_MEDIUM", label="도구 확인 - 휴일 위험", raw_value="MEDIUM", points=delta, category="policy"))

    if merchant_result:
        m_facts = merchant_result.get("facts") or {}
        merchant_risk = str(m_facts.get("merchantRisk") or "UNKNOWN").upper()
        delta = _MERCHANT_RISK_POLICY_DELTA.get(merchant_risk, 0.0)
        if delta > 0:
            tool_policy_delta += delta
            reasons.append(_reason_prefix(delta) + f"도구 확인: 가맹점 위험도 {merchant_risk}")
            signals.append(ScoreSignalDetail(signal=f"merchantRisk_{merchant_risk}", label=f"가맹점/업종 위험도({merchant_risk})", raw_value=merchant_risk, points=delta, category="policy"))

    if policy_result:
        p_facts = policy_result.get("facts") or {}
        ref_count = int(p_facts.get("ref_count") or 0)
        delta = _lookup_tiered(ref_count, _POLICY_REF_EVIDENCE_POINTS)
        if delta > 0:
            tool_evidence_delta += delta
            reasons.append(_reason_prefix(delta) + f"규정 조항 {ref_count}건 확보")
            signals.append(ScoreSignalDetail(signal="policyRefs", label=f"규정 조항 {ref_count}건", raw_value=ref_count, points=delta, category="evidence"))

    if doc_result:
        d_facts = doc_result.get("facts") or {}
        line_count = int(d_facts.get("lineItemCount") or 0)
        delta = _lookup_tiered(line_count, _LINE_ITEM_EVIDENCE_POINTS)
        if delta > 0:
            tool_evidence_delta += delta
            reasons.append(_reason_prefix(delta) + f"전표 라인아이템 {line_count}건 확보")
            signals.append(ScoreSignalDetail(signal="lineItems", label=f"전표 라인 {line_count}건", raw_value=line_count, points=delta, category="evidence"))

    if any(_tool_result_key(r) == "legacy_aura_deep_audit" and r.get("facts") for r in tool_results):
        tool_evidence_delta += 15.0
        reasons.append(_reason_prefix(15.0) + "심층 감사 결과 확보")
        signals.append(ScoreSignalDetail(signal="legacyAudit", label="심층 감사 결과", raw_value=True, points=15.0, category="evidence"))

    if bool(flags.get("hasHitlResponse")):
        tool_evidence_delta += 10.0
        reasons.append(_reason_prefix(10.0) + "담당자 검토 응답 확보")
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
        reasons.append(_reason_prefix(-penalty) + f"도구 실행 성공률 {success_rate:.0%} → evidence_score -{penalty}점")
    if total_tools >= 3 and success_rate == 1.0:
        evidence_score = min(100.0, evidence_score + 5.0)
        reasons.append(_reason_prefix(5.0) + f"계획한 {total_tools}개 도구 전체 성공 → evidence_score +5점")

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
        reasons.append(_reason_prefix(0.0) + f"복합 위험 승수 적용 ({high_risk_count}개 고위험 신호)")
        signals.append(ScoreSignalDetail(signal="compound_multiplier", label=f"복합 위험({high_risk_count}개)", raw_value=high_risk_count, points=0.0, category="multiplier"))

    policy_score = policy_score * compound_multiplier

    amount = float(flags.get("amount") or 0)
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
    reasons.append(_reason_prefix(0.0) + f"{amount_label} ({int(amount):,}원)")
    signals.append(ScoreSignalDetail(signal="amount_weight", label=f"{amount_label}({int(amount):,}원)", raw_value=amount, points=0.0, category="amount"))

    policy_score = min(100.0, policy_score * amount_multiplier)

    evidence_completeness = min(1.0, (min(ref_count, 3) / 3.0) * 0.6 + (min(line_count, 2) / 2.0) * 0.4)
    policy_weight = 0.75 - (0.25 * evidence_completeness)
    evidence_weight = 1.0 - policy_weight

    final_score_raw = policy_score * policy_weight + evidence_score * evidence_weight
    if high_risk_count >= 3 and evidence_completeness < 0.4:
        penalty = (0.4 - evidence_completeness) * 20.0
        final_score_raw -= penalty
        reasons.append(_reason_prefix(-penalty) + f"증거 부족 보수 패널티 적용 (-{penalty:.1f})")
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
        "evidence_completeness": round(float(evidence_completeness), 4),
        "rule_violation_summary": [str(x) for x in reasons[:6]],
    }


def _scoring_versions() -> dict[str, str]:
    return {
        "scoring_version": settings.scoring_version,
        "rubric_version": settings.scoring_rubric_version,
        "prompt_version": settings.scoring_prompt_version,
    }


def _load_scoring_rubric_text() -> str:
    try:
        if _RUBRIC_PATH.exists():
            return _RUBRIC_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return (
        "정책(policy), 증거(evidence), 충실도(fidelity)를 각각 0~100으로 평가한다. "
        "점수와 함께 사용자용 요약(summary_reason)과 내부 상세(internal_reason)를 반환한다."
    )


def _derive_rule_gate(rule_score_breakdown: dict[str, Any], tool_results: list[dict[str, Any]]) -> str:
    failed_tools = [
        str(_tool_result_key(r) or "")
        for r in (tool_results or [])
        if not bool((r or {}).get("ok"))
    ]
    if any(t in {"policy_rulebook_probe", "document_evidence_probe"} for t in failed_tools):
        return "regenerate"
    final_score = int(rule_score_breakdown.get("final_score") or 0)
    evidence_completeness = float(rule_score_breakdown.get("evidence_completeness") or 0.0)
    if final_score >= 75 and evidence_completeness < 0.4:
        return "hold"
    if final_score >= 55:
        return "caution"
    return "pass"


def _resolve_final_decision(
    *,
    rule_gate: str,
    rule_score: int,
    llm_score: int | None,
    conflict_warning: bool = False,
) -> str:
    gate = str(rule_gate or "").lower()
    if gate == "hold":
        return "HOLD"
    if gate == "regenerate":
        return "REGENERATE"
    if gate == "caution" or conflict_warning:
        return "CAUTION"
    if llm_score is not None and (rule_score >= 75 or llm_score >= 75):
        return "CAUTION"
    return "PASS"


def _mark_circuit(fallback_used: bool) -> None:
    global _JUDGE_CIRCUIT_OPEN
    _JUDGE_CIRCUIT_HISTORY.append(bool(fallback_used))
    if len(_JUDGE_CIRCUIT_HISTORY) < max(1, int(settings.llm_judge_circuit_window)):
        return
    rate = sum(1 for x in _JUDGE_CIRCUIT_HISTORY if x) / len(_JUDGE_CIRCUIT_HISTORY)
    if rate >= float(settings.llm_judge_circuit_failure_threshold):
        if not _JUDGE_CIRCUIT_OPEN:
            logger.warning(
                "llm judge circuit breaker opened: fallback_rate=%.2f window=%s",
                rate,
                len(_JUDGE_CIRCUIT_HISTORY),
            )
        _JUDGE_CIRCUIT_OPEN = True


async def _call_llm_judge(
    *,
    body_evidence: dict[str, Any],
    rule_score_breakdown: dict[str, Any],
) -> tuple[ScoringResult, float]:
    from openai import AsyncAzureOpenAI, AsyncOpenAI

    base_url = (settings.openai_base_url or "").strip()
    api_key = settings.openai_api_key
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY missing")
    if ".openai.azure.com" in base_url:
        client: Any = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=base_url,
            api_version=settings.openai_api_version,
        )
    else:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)

    rubric = _load_scoring_rubric_text()
    rule_violation_summary = rule_score_breakdown.get("rule_violation_summary") or []
    evidence_completeness = float(rule_score_breakdown.get("evidence_completeness") or 0.0)
    payload = {
        "body_evidence": {
            "occurredAt": body_evidence.get("occurredAt"),
            "merchantName": body_evidence.get("merchantName"),
            "amount": body_evidence.get("amount"),
            "isHoliday": body_evidence.get("isHoliday"),
            "hrStatus": body_evidence.get("hrStatus"),
            "mccCode": body_evidence.get("mccCode"),
            "budgetExceeded": body_evidence.get("budgetExceeded"),
        },
        "rule_engine_result": {
            "policy_score": rule_score_breakdown.get("policy_score"),
            "evidence_score": rule_score_breakdown.get("evidence_score"),
            "final_score": rule_score_breakdown.get("final_score"),
            "severity": rule_score_breakdown.get("severity"),
            "evidence_completeness": evidence_completeness,
            "rule_violation_summary": rule_violation_summary,
        },
        "instruction": "규칙 엔진 발견사항을 반드시 반영해 policy/evidence/grounding을 0~100으로 평가",
    }
    system_prompt = (
        "당신은 재무 감사 검증 심사관이다. 아래 루브릭으로 점수화하라.\n"
        f"{rubric}\n"
        "점수 해석 규칙(반드시 준수): "
        "policy_score는 위반/위험 신호 점수이므로 높을수록 불리하다. "
        "evidence_score는 근거 충실도 점수이므로 높을수록 유리하다. "
        "policy_score가 높은데도 '우수/양호/안전'으로 표현하면 안 된다.\n"
        "출력 JSON 스키마:\n"
        "{policy_score:int,evidence_score:int,grounding_score:int,overall_score:int,summary_reason:str,internal_reason:str}\n"
        "요약(summary_reason)은 사용자 친화적 2~3문장, internal_reason은 구체적 근거를 포함."
    )
    user_prompt = json.dumps(payload, ensure_ascii=False)
    start = time.perf_counter()
    create_kwargs = completion_kwargs_for_azure(
        base_url,
        model=settings.llm_judge_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )
    timeout_sec = max(0.5, float(settings.llm_judge_timeout_ms) / 1000.0)
    response = await asyncio.wait_for(client.chat.completions.create(**create_kwargs), timeout=timeout_sec)
    latency_ms = (time.perf_counter() - start) * 1000.0
    content = (response.choices[0].message.content or "").strip()
    if not content:
        raise ValueError("empty llm judge response")
    try:
        obj = json.loads(content)
    except Exception as e:
        raise ValueError(f"judge parse failed: {e}") from e
    try:
        parsed = ScoringResult.model_validate(obj)
    except Exception as e:
        raise ValueError(f"judge schema failed: {e}") from e
    return parsed, latency_ms


async def _score_hybrid(
    state: dict[str, Any],
    flags: dict[str, Any],
    tool_results: list[dict[str, Any]],
) -> dict[str, Any]:
    out = dict(_score(flags, tool_results))
    out["version_meta"] = _scoring_versions()
    out["llm_judge_enabled"] = bool(settings.llm_judge_enabled)
    out["max_retries"] = int(settings.llm_judge_max_retries)
    out["retry_count"] = int(state.get("retry_count") or 0)
    out["fallback_used"] = False
    out["fallback_reason"] = None
    out["judge_skipped"] = False
    out["skip_reason"] = None
    out["latency_ms"] = None
    out["diagnostic_log"] = ""
    out["summary_reason"] = "규칙 기반 점수로 판정했습니다."

    rule_score = int(out.get("final_score") or 0)
    rule_fidelity = int(round(float(out.get("evidence_completeness") or 0.0) * 100))
    out["rule_score"] = rule_score
    out["llm_score"] = rule_score
    out["rule_fidelity"] = rule_fidelity
    out["llm_fidelity"] = rule_fidelity
    out["fidelity"] = rule_fidelity
    rule_gate = _derive_rule_gate(out, tool_results)
    out["verification_gate"] = rule_gate
    out["final_decision"] = _resolve_final_decision(rule_gate=rule_gate, rule_score=rule_score, llm_score=None)
    out["conflict_warning"] = False

    if not bool(settings.llm_judge_enabled):
        out["judge_skipped"] = True
        out["skip_reason"] = "llm_judge_disabled"
        out["diagnostic_log"] = "LLM Judge 비활성화 상태입니다."
        return out
    if rule_gate in {"hold", "regenerate"}:
        out["judge_skipped"] = True
        out["skip_reason"] = "rule_gate_blocked"
        out["diagnostic_log"] = f"규칙 게이트({rule_gate})가 확정되어 LLM Judge 호출을 생략했습니다."
        return out
    if _JUDGE_CIRCUIT_OPEN:
        out["judge_skipped"] = True
        out["skip_reason"] = "circuit_breaker_open"
        out["diagnostic_log"] = "LLM Judge circuit breaker가 열려 호출을 생략했습니다."
        return out

    last_error_code: str | None = None
    last_error_message: str = ""
    max_retries = max(0, int(settings.llm_judge_max_retries))
    for attempt in range(max_retries + 1):
        try:
            judge, latency_ms = await _call_llm_judge(
                body_evidence=state.get("body_evidence") or {},
                rule_score_breakdown=out,
            )
            llm_score = int(judge.overall_score)
            llm_fidelity = int(judge.grounding_score)
            fidelity = min(rule_fidelity, llm_fidelity)
            out["llm_score"] = llm_score
            out["llm_fidelity"] = llm_fidelity
            out["fidelity"] = fidelity
            out["latency_ms"] = round(latency_ms, 2)
            out["summary_reason"] = _sanitize_summary_reason(
                str(judge.summary_reason or "").strip() or out["summary_reason"],
                out.get("policy_score"),
                out.get("evidence_score"),
                out.get("final_score"),
            )
            out["diagnostic_log"] = str(judge.internal_reason or "").strip()
            conflict = abs(rule_score - llm_score) >= int(settings.llm_judge_conflict_threshold)
            out["conflict_warning"] = conflict
            if conflict:
                out["summary_reason"] = f"{out['summary_reason']} (판단 불일치 주의: 규칙 점수와 LLM 점수 편차가 큽니다.)".strip()
            out["final_decision"] = _resolve_final_decision(
                rule_gate=rule_gate,
                rule_score=rule_score,
                llm_score=llm_score,
                conflict_warning=conflict,
            )
            _mark_circuit(False)
            return out
        except asyncio.TimeoutError as e:
            last_error_code = FallbackReason.TIMEOUT.value
            last_error_message = str(e)
        except ValueError as e:
            msg = str(e).lower()
            if "schema" in msg:
                last_error_code = FallbackReason.SCHEMA_ERROR.value
            elif "parse" in msg:
                last_error_code = FallbackReason.PARSE_ERROR.value
            else:
                last_error_code = FallbackReason.PARSE_ERROR.value
            last_error_message = str(e)
        except Exception as e:
            last_error_code = FallbackReason.PROVIDER_ERROR.value
            last_error_message = str(e)
        out["retry_count"] = attempt + 1
        if attempt < max_retries:
            continue

    out["fallback_used"] = True
    out["fallback_reason"] = last_error_code or FallbackReason.PROVIDER_ERROR.value
    out["diagnostic_log"] = (
        f"LLM Judge 실패로 규칙 점수로 fallback했습니다. reason={out['fallback_reason']} detail={last_error_message[:400]}"
    )
    logger.warning(
        "llm_judge fallback: reason=%s retries=%s rule_score=%s detail=%s",
        out["fallback_reason"],
        out.get("retry_count", 0),
        rule_score,
        last_error_message[:200],
    )
    out["final_decision"] = _resolve_final_decision(rule_gate=rule_gate, rule_score=rule_score, llm_score=None)
    _mark_circuit(True)
    return out
