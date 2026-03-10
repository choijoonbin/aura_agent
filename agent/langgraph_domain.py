from __future__ import annotations

from datetime import datetime
from typing import Any

_VALID_SCREENING_CASE_TYPES = {
    "HOLIDAY_USAGE",
    "LIMIT_EXCEED",
    "PRIVATE_USE_RISK",
    "UNUSUAL_PATTERN",
    "NORMAL_BASELINE",
}


def _is_valid_screening_case_type(value: Any) -> bool:
    return str(value or "").strip().upper() in _VALID_SCREENING_CASE_TYPES


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


def _voucher_summary_for_context(body_evidence: dict[str, Any]) -> str:
    merchant = (body_evidence.get("merchantName") or "").strip() or "거래처 미상"
    amount = body_evidence.get("amount")
    amount_str = f"{int(amount):,}원" if amount is not None else "금액 미상"
    occurred = _format_occurred_at(body_evidence.get("occurredAt"))
    return f"거래처 {merchant}, {amount_str}, {occurred}"


def _tool_result_key(r: dict[str, Any]) -> str:
    return str(r.get("tool") or r.get("skill") or "")


def _find_tool_result(tool_results: list[dict[str, Any]], tool_name: str) -> dict[str, Any] | None:
    return next((r for r in tool_results if _tool_result_key(r) == tool_name), None)


def _top_policy_refs(tool_results: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    refs = (_find_tool_result(tool_results, "policy_rulebook_probe") or {}).get("facts", {}).get("policy_refs") or []
    return list(refs[:limit])


def _build_prescreened_result(body: dict[str, Any]) -> dict[str, Any]:
    ct = body.get("case_type") or body.get("intended_risk_type") or ""
    if not _is_valid_screening_case_type(ct):
        ct = ""
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
