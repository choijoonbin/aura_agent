"""
Deterministic screening engine — classifies raw voucher evidence into a case type.

This mirrors the original aura-platform precheck_pipeline logic:
  1. Extract signals from raw voucher fields (no LLM required)
  2. Score each signal deterministically
  3. Derive case_type by priority rule
  4. Assign severity from final score

The result is stored in AgentCase.case_type before the main LangGraph analysis runs.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# MCC risk category maps
# ---------------------------------------------------------------------------
_MCC_HIGH_RISK = {
    "5813",  # Drinking Places, Bars
    "7993",  # Video Game Arcades/Establishments
    "7994",  # Video Games Supply Stores
    "5912",  # Drug Stores / Pharmacies (high-risk if night)
}
_MCC_LEISURE = {
    "7992",  # Golf Courses/Public
    "7996",  # Amusement Parks
    "7997",  # Clubs, Country Clubs, Athletic Clubs
    "7941",  # Sports Clubs, Athletic Fields
    "7011",  # Hotels, Motels, Resorts
}
_MCC_MEDIUM_RISK = {
    "5812",  # Eating Places, Restaurants
    "5811",  # Caterers
    "5814",  # Fast Food Restaurants
}


def _extract_signals(body: dict[str, Any]) -> dict[str, Any]:
    """Extract all relevant boolean/categorical signals from body_evidence."""
    is_holiday = bool(body.get("isHoliday"))

    hr_raw = str(body.get("hrStatus") or body.get("hrStatusRaw") or "").upper()
    is_leave = hr_raw in {"LEAVE", "OFF", "VACATION"}

    occurred = str(body.get("occurredAt") or "")
    is_night = False
    hour: int | None = None
    try:
        hour = int(occurred[11:13])
        is_night = hour >= 22 or hour < 6
    except Exception:
        pass

    budget_exceeded = bool(body.get("budgetExceeded"))
    mcc = str(body.get("mccCode") or "").strip()
    amount = float(body.get("amount") or 0)

    return {
        "is_holiday": is_holiday,
        "is_leave": is_leave,
        "is_night": is_night,
        "hour": hour,
        "budget_exceeded": budget_exceeded,
        "mcc_code": mcc,
        "mcc_high_risk": mcc in _MCC_HIGH_RISK,
        "mcc_leisure": mcc in _MCC_LEISURE,
        "mcc_medium_risk": mcc in _MCC_MEDIUM_RISK,
        "amount": amount,
        "hr_status": hr_raw,
    }


def _score_signals(signals: dict[str, Any]) -> tuple[int, list[str]]:
    """Score deterministically. Returns (score, reasons list)."""
    score = 0
    reasons: list[str] = []

    if signals["is_holiday"]:
        score += 35
        reasons.append("주말/휴일 발생")
    if signals["is_leave"]:
        score += 20
        reasons.append(f"근태 상태 {signals['hr_status']} (휴가/결근)")
    if signals["is_night"]:
        score += 10
        reasons.append(f"심야 시간대 사용 ({signals['hour']:02d}시)")
    if signals["budget_exceeded"]:
        score += 10
        reasons.append("예산 한도 초과 플래그")
    if signals["mcc_high_risk"]:
        score += 30
        reasons.append(f"고위험 업종 가맹점 업종 코드(MCC) {signals['mcc_code']}")
    elif signals["mcc_leisure"]:
        score += 25
        reasons.append(f"레저/오락 업종 가맹점 업종 코드(MCC) {signals['mcc_code']}")
    elif signals["mcc_medium_risk"]:
        score += 15
        reasons.append(f"일반 식음료 업종 가맹점 업종 코드(MCC) {signals['mcc_code']}")

    return min(score, 100), reasons


def _derive_case_type(signals: dict[str, Any], score: int) -> str:
    """Priority-based case type derivation (mirrors original _align_case_type_with_signals)."""
    is_holiday = signals["is_holiday"]
    is_leave = signals["is_leave"]
    budget_exceeded = signals["budget_exceeded"]
    mcc_leisure = signals["mcc_leisure"]

    # Priority 1: Holiday + leave → clear HOLIDAY_USAGE
    if is_holiday and is_leave:
        return "HOLIDAY_USAGE"

    # Priority 2: Holiday alone (weekend, no leave info) and high-risk MCC
    if is_holiday and signals["mcc_high_risk"]:
        return "HOLIDAY_USAGE"

    # Priority 3: Budget exceeded with meaningful score → LIMIT_EXCEED
    if budget_exceeded and score >= 25:
        return "LIMIT_EXCEED"

    # Priority 4: Leisure MCC + leave → personal use risk
    if mcc_leisure and is_leave:
        return "PRIVATE_USE_RISK"

    # Priority 5: High score but unclassified → unusual pattern
    if score >= 30:
        return "UNUSUAL_PATTERN"

    # Default: normal
    return "NORMAL_BASELINE"


def _derive_severity(score: int) -> str:
    if score >= 70:
        return "CRITICAL"
    if score >= 50:
        return "HIGH"
    if score >= 30:
        return "MEDIUM"
    return "LOW"


def _build_reason_text(case_type: str, signals: dict[str, Any], reasons: list[str]) -> str:
    label_map = {
        "HOLIDAY_USAGE": "휴일/휴무 중 사용 의심",
        "LIMIT_EXCEED": "한도 초과 의심",
        "PRIVATE_USE_RISK": "사적 사용 위험",
        "UNUSUAL_PATTERN": "비정상 패턴",
        "NORMAL_BASELINE": "정상 범위",
    }
    label = label_map.get(case_type, case_type)
    if not reasons:
        return f"스크리닝 결과: {label} — 특이사항 없음"
    detail = " / ".join(reasons)
    return f"스크리닝 결과: {label} — {detail}"


def run_screening(body_evidence: dict[str, Any]) -> dict[str, Any]:
    """
    Main entry point. Takes body_evidence dict (same shape as analysis payload)
    and returns a screening result dict.

    Returns:
        {
            case_type: str,         # HOLIDAY_USAGE | LIMIT_EXCEED | PRIVATE_USE_RISK |
                                    #   UNUSUAL_PATTERN | NORMAL_BASELINE
            severity: str,          # CRITICAL | HIGH | MEDIUM | LOW
            score: int,             # 0-100
            signals: dict,          # raw extracted signals
            reasons: list[str],     # human-readable reason list
            reason_text: str,       # combined reason string
        }
    """
    signals = _extract_signals(body_evidence)
    score, reasons = _score_signals(signals)
    case_type = _derive_case_type(signals, score)
    severity = _derive_severity(score)
    reason_text = _build_reason_text(case_type, signals, reasons)

    return {
        "case_type": case_type,
        "severity": severity,
        "score": score,
        "signals": signals,
        "reasons": reasons,
        "reason_text": reason_text,
    }
