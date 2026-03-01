from __future__ import annotations

from typing import Any


def build_hitl_request(body_evidence: dict[str, Any], tool_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    missing = ((body_evidence.get("dataQuality") or {}).get("missingFields") or [])
    document = body_evidence.get("document") or {}
    items = document.get("items") or []
    holiday_risk = bool(body_evidence.get("isHoliday")) or (body_evidence.get("hrStatus") in {"LEAVE", "OFF", "VACATION"})
    has_legacy_result = any(r.get("skill") == "legacy_aura_deep_audit" and r.get("facts") for r in tool_results)

    questions: list[str] = []
    reasons: list[str] = []

    if holiday_risk and not items:
        reasons.append("전표 라인아이템 부족")
        questions.append("휴일 사용의 업무 목적과 참석자 정보를 확인해 주세요.")
    if missing:
        reasons.append("입력 필드 일부 누락")
        questions.append(f"누락 필드 보완이 필요합니다: {', '.join(missing)}")
    if holiday_risk and not has_legacy_result:
        reasons.append("심층 감사 결과 미확보")
        questions.append("추가 규정 검토 후 확정 판단이 가능한지 검토해 주세요.")

    if not reasons:
        return None

    return {
        "required": True,
        "reasons": reasons,
        "questions": questions,
        "handoff": "FINANCE_REVIEWER",
    }
