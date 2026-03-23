from __future__ import annotations

import asyncio
from typing import Any

from langgraph.graph import END, START, StateGraph

from utils.config import settings

_CHECKPOINTER: Any | None = None
_COMPILED_GRAPH: Any | None = None
_CLOSURE_GRAPH: Any | None = None


def _get_checkpointer():
    global _CHECKPOINTER
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER

    backend = getattr(settings, "checkpointer_backend", "memory").lower()

    if backend == "postgres":
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # type: ignore[import-untyped]

            async def _init_postgres_checkpointer() -> Any:
                cp = await AsyncPostgresSaver.from_conn_string(settings.database_url)
                await cp.setup()
                return cp

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None and loop.is_running():
                import concurrent.futures

                def _run_init() -> Any:
                    return asyncio.run(_init_postgres_checkpointer())

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(_run_init)
                    _CHECKPOINTER = future.result(timeout=15)
            else:
                _loop = asyncio.new_event_loop()
                try:
                    _CHECKPOINTER = _loop.run_until_complete(_init_postgres_checkpointer())
                finally:
                    _loop.close()
        except ImportError:
            import warnings

            warnings.warn(
                "langgraph-checkpoint-postgres가 없어 MemorySaver로 fallback합니다. "
                "pip install langgraph-checkpoint-postgres",
                RuntimeWarning,
                stacklevel=2,
            )
            from langgraph.checkpoint.memory import MemorySaver

            _CHECKPOINTER = MemorySaver()
    else:
        from langgraph.checkpoint.memory import MemorySaver

        _CHECKPOINTER = MemorySaver()

    return _CHECKPOINTER


def build_agent_graph(
    *,
    state_type: type,
    start_router_node: Any,
    screener_node: Any,
    intake_node: Any,
    planner_node: Any,
    execute_node: Any,
    critic_node: Any,
    verify_node: Any,
    hitl_pause_node: Any,
    hitl_validate_node: Any,
    reporter_node: Any,
    finalizer_node: Any,
    route_after_critic: Any,
    route_after_verify: Any,
    route_after_hitl_validate: Any,
) -> Any:
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is not None:
        return _COMPILED_GRAPH

    workflow = StateGraph(state_type)
    workflow.add_node("start_router", start_router_node)
    workflow.add_node("screener", screener_node)
    workflow.add_node("intake", intake_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("execute", execute_node)
    workflow.add_node("critic", critic_node)
    workflow.add_node("verify", verify_node)
    workflow.add_node("hitl_pause", hitl_pause_node)
    workflow.add_node("hitl_validate", hitl_validate_node)
    workflow.add_node("reporter", reporter_node)
    workflow.add_node("finalizer", finalizer_node)

    def _route_after_start_router(state: dict[str, Any]) -> str:
        return "intake" if state.get("screening_result") else "screener"

    workflow.add_edge(START, "start_router")
    workflow.add_conditional_edges("start_router", _route_after_start_router, {"intake": "intake", "screener": "screener"})
    workflow.add_edge("screener", "intake")
    workflow.add_edge("intake", "planner")
    workflow.add_edge("planner", "execute")
    workflow.add_edge("execute", "critic")
    workflow.add_conditional_edges("critic", route_after_critic, {"planner": "planner", "verify": "verify"})
    workflow.add_conditional_edges("verify", route_after_verify, {"planner": "planner", "hitl_pause": "hitl_pause", "reporter": "reporter"})
    workflow.add_edge("hitl_pause", "hitl_validate")
    workflow.add_conditional_edges("hitl_validate", route_after_hitl_validate, {"hitl_pause": "hitl_pause", "reporter": "reporter"})
    workflow.add_edge("reporter", "finalizer")
    workflow.add_edge("finalizer", END)
    _COMPILED_GRAPH = workflow.compile(checkpointer=_get_checkpointer())
    return _COMPILED_GRAPH


def build_hitl_closure_graph(
    *,
    state_type: type,
    hitl_validate_node: Any,
    reporter_node: Any,
    finalizer_node: Any,
) -> Any:
    global _CLOSURE_GRAPH
    if _CLOSURE_GRAPH is not None:
        return _CLOSURE_GRAPH
    workflow = StateGraph(state_type)
    workflow.add_node("hitl_validate", hitl_validate_node)
    workflow.add_node("reporter", reporter_node)
    workflow.add_node("finalizer", finalizer_node)
    workflow.add_edge(START, "hitl_validate")
    workflow.add_edge("hitl_validate", "reporter")
    workflow.add_edge("reporter", "finalizer")
    workflow.add_edge("finalizer", END)
    _CLOSURE_GRAPH = workflow.compile(checkpointer=None)
    return _CLOSURE_GRAPH


def _closure_verification(verification: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(verification or {})
    if "quality_signals" not in out:
        out["quality_signals"] = out.get("quality_gate_codes") or ["OK"]
    return out


def _closure_initial_state(
    *,
    previous_result: dict[str, Any],
    body_evidence: dict[str, Any],
    case_id: str,
    intended_risk_type: str | None,
    resume_value: dict[str, Any],
) -> dict[str, Any]:
    body = dict(body_evidence or {})
    body["hitlResponse"] = resume_value
    flags = dict(previous_result.get("flags") or {})
    flags["hasHitlResponse"] = True
    flags["hitlApproved"] = resume_value.get("approved") is True
    return {
        "case_id": case_id,
        "body_evidence": body,
        "intended_risk_type": intended_risk_type or None,
        "hitl_request": previous_result.get("hitl_request"),
        "score_breakdown": dict(previous_result.get("score_breakdown") or {}),
        "tool_results": list(previous_result.get("tool_results") or []),
        "flags": flags,
        "verification": _closure_verification(previous_result.get("verification")),
        "verifier_output": dict(previous_result.get("verifier_output") or {}),
        "rule_score": int(previous_result.get("rule_score") or ((previous_result.get("score_breakdown") or {}).get("rule_score") or 0)),
        "llm_score": int(previous_result.get("llm_score") or ((previous_result.get("score_breakdown") or {}).get("llm_score") or 0)),
        "final_score": int(previous_result.get("final_score") or ((previous_result.get("score_breakdown") or {}).get("final_score") or 0)),
        "verification_gate": str(previous_result.get("verification_gate") or ((previous_result.get("score_breakdown") or {}).get("verification_gate") or "pass")),
        "summary_reason": str(previous_result.get("summary_reason") or ((previous_result.get("score_breakdown") or {}).get("summary_reason") or "")),
        "diagnostic_log": str(previous_result.get("diagnostic_log") or ((previous_result.get("score_breakdown") or {}).get("diagnostic_log") or "")),
        "fidelity": int(previous_result.get("fidelity") or ((previous_result.get("score_breakdown") or {}).get("fidelity") or 0)),
        "rule_fidelity": int(previous_result.get("rule_fidelity") or ((previous_result.get("score_breakdown") or {}).get("rule_fidelity") or 0)),
        "llm_fidelity": int(previous_result.get("llm_fidelity") or ((previous_result.get("score_breakdown") or {}).get("llm_fidelity") or 0)),
        "fallback_used": bool(previous_result.get("fallback_used") or ((previous_result.get("score_breakdown") or {}).get("fallback_used") or False)),
        "fallback_reason": str(previous_result.get("fallback_reason") or ((previous_result.get("score_breakdown") or {}).get("fallback_reason") or "")),
        "retry_count": int(previous_result.get("retry_count") or ((previous_result.get("score_breakdown") or {}).get("retry_count") or 0)),
        "max_retries": int(previous_result.get("max_retries") or ((previous_result.get("score_breakdown") or {}).get("max_retries") or 2)),
        "latency_ms": dict(previous_result.get("latency_ms") or {"llm_judge": float((previous_result.get("score_breakdown") or {}).get("latency_ms") or 0.0)}),
        "version_meta": dict(previous_result.get("version_meta") or ((previous_result.get("score_breakdown") or {}).get("version_meta") or {})),
        "evaluation_history": list(previous_result.get("evaluation_history") or []),
        "last_node_summary": str(previous_result.get("last_node_summary") or ""),
    }
