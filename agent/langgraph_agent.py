from __future__ import annotations

import logging
from typing import Any

import agent.langgraph_runtime as _runtime_module
import agent.langgraph_nodes as _nodes_module
from langgraph.types import interrupt
from agent.langgraph_nodes import (
    AgentState,
    _assess_hitl_resolution_requirements,
    _available_planner_tools,
    _build_grounded_reason,
    _build_prescreened_result,
    _build_system_auto_finalize_blockers,
    _build_verification_targets,
    _compact_reasoning_for_stream,
    _compute_plan_achievement,
    _derive_flags,
    _derive_hitl_from_regulation,
    _extract_reasoning_token,
    _find_tool_result,
    _format_hitl_reason_for_stream,
    _format_occurred_at,
    _generate_hitl_review_content,
    _get_hitl_response_value,
    _get_tools_by_name,
    _invoke_llm_planner,
    _is_valid_screening_case_type,
    _is_verify_ready_without_hitl,
    _llm_decide_hitl_verdict,
    _llm_summarize_completed_reason,
    _llm_summarize_hold_reason,
    _lookup_tiered,
    _pick_llm_review_reason,
    _plan_from_flags,
    _reason_prefix,
    _reasoning_stream_events,
    _retry_fill_hitl_review_when_empty,
    _route_after_critic,
    _route_after_hitl_validate,
    _route_after_verify,
    _score,
    _score_to_severity,
    _score_with_hitl_adjustment,
    _select_policy_refs_by_relevance,
    _should_skip_tool,
    _stream_reasoning_events_with_llm,
    _tool_result_key,
    _top_policy_refs,
    _voucher_summary_for_context,
    call_node_llm_with_consistency_check,
    check_reasoning_consistency,
    critic_node as _nodes_critic_node,
    execute_node as _nodes_execute_node,
    finalizer_node as _nodes_finalizer_node,
    hitl_pause_node as _nodes_hitl_pause_node,
    hitl_validate_node as _nodes_hitl_validate_node,
    intake_node as _nodes_intake_node,
    planner_node as _nodes_planner_node,
    reporter_node as _nodes_reporter_node,
    screener_node as _nodes_screener_node,
    start_router_node as _nodes_start_router_node,
    verify_node as _nodes_verify_node,
)
from agent.langgraph_runtime import (
    _closure_initial_state as _runtime_closure_initial_state,
    _closure_verification as _runtime_closure_verification,
    _get_checkpointer as _runtime_get_checkpointer,
    build_agent_graph as _runtime_build_agent_graph,
    build_hitl_closure_graph as _runtime_build_hitl_closure_graph,
)
from agent.langgraph_runflow import run_langgraph_agentic_analysis_impl as _run_langgraph_agentic_analysis_impl
from utils.config import settings

logger = logging.getLogger(__name__)

_CHECKPOINTER: Any | None = None
_COMPILED_GRAPH: Any | None = None
_CLOSURE_GRAPH: Any | None = None


def _sync_nodes_module_hooks() -> None:
    """Preserve legacy monkeypatching surface on agent.langgraph_agent."""
    _nodes_module._stream_reasoning_events_with_llm = _stream_reasoning_events_with_llm
    _nodes_module._get_tools_by_name = _get_tools_by_name
    _nodes_module._invoke_llm_planner = _invoke_llm_planner
    _nodes_module._available_planner_tools = _available_planner_tools
    _nodes_module._select_policy_refs_by_relevance = _select_policy_refs_by_relevance
    _nodes_module._llm_decide_hitl_verdict = _llm_decide_hitl_verdict
    _nodes_module._llm_summarize_hold_reason = _llm_summarize_hold_reason
    _nodes_module._llm_summarize_completed_reason = _llm_summarize_completed_reason
    _nodes_module._build_grounded_reason = _build_grounded_reason
    _nodes_module._build_verification_targets = _build_verification_targets
    _nodes_module._derive_hitl_from_regulation = _derive_hitl_from_regulation
    _nodes_module._generate_hitl_review_content = _generate_hitl_review_content
    _nodes_module._retry_fill_hitl_review_when_empty = _retry_fill_hitl_review_when_empty


async def start_router_node(state: AgentState) -> AgentState:
    _sync_nodes_module_hooks()
    return await _nodes_start_router_node(state)


async def screener_node(state: AgentState) -> AgentState:
    _sync_nodes_module_hooks()
    return await _nodes_screener_node(state)


async def intake_node(state: AgentState) -> AgentState:
    _sync_nodes_module_hooks()
    return await _nodes_intake_node(state)


async def planner_node(state: AgentState) -> AgentState:
    _sync_nodes_module_hooks()
    return await _nodes_planner_node(state)


async def execute_node(state: AgentState) -> AgentState:
    _sync_nodes_module_hooks()
    return await _nodes_execute_node(state)


async def critic_node(state: AgentState) -> AgentState:
    _sync_nodes_module_hooks()
    return await _nodes_critic_node(state)


async def verify_node(state: AgentState) -> AgentState:
    _sync_nodes_module_hooks()
    return await _nodes_verify_node(state)


async def hitl_pause_node(state: AgentState) -> AgentState:
    # Keep legacy patching surface for tests/callers that patch
    # agent.langgraph_agent.interrupt.
    _sync_nodes_module_hooks()
    _nodes_module.interrupt = interrupt
    return await _nodes_hitl_pause_node(state)


async def hitl_validate_node(state: AgentState) -> AgentState:
    _sync_nodes_module_hooks()
    return await _nodes_hitl_validate_node(state)


async def reporter_node(state: AgentState) -> AgentState:
    _sync_nodes_module_hooks()
    return await _nodes_reporter_node(state)


async def finalizer_node(state: AgentState) -> AgentState:
    _sync_nodes_module_hooks()
    return await _nodes_finalizer_node(state)


def _get_checkpointer():
    global _CHECKPOINTER
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER
    _runtime_module._CHECKPOINTER = _CHECKPOINTER
    _CHECKPOINTER = _runtime_get_checkpointer()
    return _CHECKPOINTER


def build_agent_graph():
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is not None:
        return _COMPILED_GRAPH
    _runtime_module._COMPILED_GRAPH = _COMPILED_GRAPH
    _COMPILED_GRAPH = _runtime_build_agent_graph(
        state_type=AgentState,
        start_router_node=start_router_node,
        screener_node=screener_node,
        intake_node=intake_node,
        planner_node=planner_node,
        execute_node=execute_node,
        critic_node=critic_node,
        verify_node=verify_node,
        hitl_pause_node=hitl_pause_node,
        hitl_validate_node=hitl_validate_node,
        reporter_node=reporter_node,
        finalizer_node=finalizer_node,
        route_after_critic=_route_after_critic,
        route_after_verify=_route_after_verify,
        route_after_hitl_validate=_route_after_hitl_validate,
    )
    return _COMPILED_GRAPH


def build_hitl_closure_graph():
    global _CLOSURE_GRAPH
    if _CLOSURE_GRAPH is not None:
        return _CLOSURE_GRAPH
    _runtime_module._CLOSURE_GRAPH = _CLOSURE_GRAPH
    _CLOSURE_GRAPH = _runtime_build_hitl_closure_graph(
        state_type=AgentState,
        hitl_validate_node=hitl_validate_node,
        reporter_node=reporter_node,
        finalizer_node=finalizer_node,
    )
    return _CLOSURE_GRAPH


def _closure_verification(verification: dict[str, Any] | None) -> dict[str, Any]:
    return _runtime_closure_verification(verification)


def _closure_initial_state(
    *,
    previous_result: dict[str, Any],
    body_evidence: dict[str, Any],
    case_id: str,
    intended_risk_type: str | None,
    resume_value: dict[str, Any],
) -> dict[str, Any]:
    return _runtime_closure_initial_state(
        previous_result=previous_result,
        body_evidence=body_evidence,
        case_id=case_id,
        intended_risk_type=intended_risk_type,
        resume_value=resume_value,
    )


async def run_langgraph_agentic_analysis(
    case_id: str,
    *,
    body_evidence: dict[str, Any],
    intended_risk_type: str | None = None,
    run_id: str | None = None,
    resume_value: dict[str, Any] | None = None,
    previous_result: dict[str, Any] | None = None,
    enable_hitl: bool = True,
):
    from utils.config import get_langfuse_handler

    async for ev in _run_langgraph_agentic_analysis_impl(
        case_id=case_id,
        body_evidence=body_evidence,
        intended_risk_type=intended_risk_type,
        run_id=run_id,
        resume_value=resume_value,
        previous_result=previous_result,
        enable_hitl=enable_hitl,
        build_agent_graph=build_agent_graph,
        build_hitl_closure_graph=build_hitl_closure_graph,
        closure_initial_state=_closure_initial_state,
        format_hitl_reason_for_stream=_format_hitl_reason_for_stream,
        get_langfuse_handler=get_langfuse_handler,
        checkpointer_backend=getattr(settings, "checkpointer_backend", "memory"),
        app_logger=logger,
    ):
        yield ev
