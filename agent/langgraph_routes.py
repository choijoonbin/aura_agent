from __future__ import annotations

from typing import Any


def _route_after_critic(state: dict[str, Any], *, max_critic_loop: int) -> str:
    critic_out = state.get("critic_output") or {}
    loop_count = state.get("critic_loop_count") or 0
    has_hitl_response = (state.get("flags") or {}).get("hasHitlResponse", False)
    replan_required = bool(critic_out.get("replan_required"))
    under_limit = loop_count < max_critic_loop
    if replan_required and under_limit and not has_hitl_response:
        return "planner"
    return "verify"


def _route_after_verify(state: dict[str, Any]) -> str:
    """Phase D: 위험/규정 위배로 hitl_request가 있으면 항상 담당자 검토(hitl_pause). HITL 체크박스와 무관."""
    if state.get("hitl_request"):
        return "hitl_pause"
    return "reporter"


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


def _route_after_hitl_validate(state: dict[str, Any]) -> str:
    """hitl_validate 후: 재요청이 있으면 hitl_pause, 없으면 reporter."""
    if state.get("hitl_request"):
        return "hitl_pause"
    return "reporter"
