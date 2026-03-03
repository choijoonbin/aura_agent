"""
Phase H: run 단위 관찰 지표. 내부적으로 확인 가능한 핵심 지표를 계산한다.
"""
from __future__ import annotations

from typing import Any

from services.citation_metrics import citation_coverage


def get_run_diagnostics(
    *,
    result: dict[str, Any] | None,
    timeline: list[dict[str, Any]],
    lineage: dict[str, Any] | None,
    hitl_request: dict[str, Any] | None,
    hitl_response: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    단일 run에 대한 지표. result/timeline/lineage는 runtime 또는 DB에서 가져온 값.
    """
    res = result.get("result") if result else None
    if not res:
        return {
            "run_id": result.get("run_id") if result else None,
            "tool_call_success_rate": None,
            "tool_call_total": 0,
            "tool_call_ok": 0,
            "hitl_requested": bool(hitl_request),
            "resume_success": bool(hitl_response) if hitl_request else None,
            "citation_coverage": None,
            "fallback_usage_rate": None,
            "event_count": len(timeline),
        }
    tool_results = res.get("tool_results") or []
    tool_total = len(tool_results)
    tool_ok = sum(1 for r in tool_results if r.get("ok"))
    tool_success_rate = round(tool_ok / tool_total, 4) if tool_total else None

    reporter_output = res.get("reporter_output")
    cov = citation_coverage(reporter_output) if reporter_output else None

    agent_events = [e for e in timeline if (e.get("payload") or {}).get("event_type") == "AGENT_EVENT" or e.get("event_type") == "AGENT_EVENT"]
    payloads = [e.get("payload") or e for e in agent_events]
    with_meta = [p for p in payloads if isinstance(p, dict) and p.get("metadata")]
    total_notes = len(with_meta)
    fallback_count = sum(1 for p in with_meta if (p.get("metadata") or {}).get("note_source") == "fallback")
    fallback_rate = round(fallback_count / total_notes, 4) if total_notes else None

    return {
        "run_id": result.get("run_id") if result else None,
        "tool_call_success_rate": tool_success_rate,
        "tool_call_total": tool_total,
        "tool_call_ok": tool_ok,
        "hitl_requested": bool(hitl_request),
        "resume_success": bool(hitl_response) if hitl_request else None,
        "citation_coverage": cov,
        "fallback_usage_rate": fallback_rate,
        "event_count": len(timeline),
        "lineage_mode": (lineage or {}).get("mode"),
        "parent_run_id": (lineage or {}).get("parent_run_id"),
    }
