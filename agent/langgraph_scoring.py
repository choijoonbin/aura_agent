from __future__ import annotations

from typing import Any

from agent.langgraph_domain import _find_tool_result, _tool_result_key
from utils.config import settings

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
    }
