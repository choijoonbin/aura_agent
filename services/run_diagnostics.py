"""
Phase H: run 단위 관찰 지표. 내부적으로 확인 가능한 핵심 지표를 계산한다.
"""
from __future__ import annotations

from typing import Any

from services.citation_metrics import citation_coverage


def _extract_unsupported_claims(res: dict[str, Any]) -> list[dict[str, Any]]:
    verifier_output = res.get("verifier_output") or {}
    claims = verifier_output.get("unsupported_claims") or []
    if not claims:
        review_audit = (verifier_output.get("review_audit") or res.get("review_audit") or {})
        claims = review_audit.get("unsupported_claims") or []
    out: list[dict[str, Any]] = []
    for c in claims:
        if isinstance(c, dict):
            out.append(c)
        elif hasattr(c, "model_dump"):
            out.append(c.model_dump())
    return out


def _unsupported_taxonomy_counts(unsupported_claims: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in unsupported_claims:
        key = str(c.get("taxonomy") or "unknown").strip().lower() or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


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
            "unsupported_claim_count": 0,
            "unsupported_taxonomy_counts": {},
            "fail_closed_unsupported": False,
            "rule_score": None,
            "llm_score": None,
            "final_decision": None,
            "fallback_reason": None,
            "judge_latency_ms": None,
            "judge_skipped": None,
            "skip_reason": None,
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
    unsupported_claims = _extract_unsupported_claims(res)
    unsupported_count = len(unsupported_claims)
    unsupported_counts = _unsupported_taxonomy_counts(unsupported_claims)
    quality_codes = [str(v).upper() for v in (res.get("quality_gate_codes") or [])]
    fail_closed_unsupported = (
        "FAIL_CLOSED_UNSUPPORTED" in quality_codes
        or any(str(k).lower() in {"no_citation", "contradictory_evidence", "missing_mandatory_evidence", "low_retrieval_confidence"} for k in unsupported_counts.keys())
    )

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
        "unsupported_claim_count": unsupported_count,
        "unsupported_taxonomy_counts": unsupported_counts,
        "fail_closed_unsupported": fail_closed_unsupported,
        "rule_score": (res.get("score_breakdown") or {}).get("rule_score"),
        "llm_score": (res.get("score_breakdown") or {}).get("llm_score"),
        "final_decision": (res.get("score_breakdown") or {}).get("final_decision"),
        "fallback_reason": (res.get("score_breakdown") or {}).get("fallback_reason"),
        "judge_latency_ms": (res.get("score_breakdown") or {}).get("latency_ms"),
        "judge_skipped": (res.get("score_breakdown") or {}).get("judge_skipped"),
        "skip_reason": (res.get("score_breakdown") or {}).get("skip_reason"),
    }


def compare_runs_diagnostics(
    run_diagnostics_list: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    여러 run의 diagnostics를 한 번에 비교용으로 반환.
    run_diagnostics_list: get_run_diagnostics() 결과 리스트 (순서는 run_id 순).
    """
    if not run_diagnostics_list:
        return {"run_ids": [], "diagnostics": [], "comparison_ready": False}
    return {
        "run_ids": [d.get("run_id") for d in run_diagnostics_list],
        "diagnostics": run_diagnostics_list,
        "comparison_ready": True,
    }
