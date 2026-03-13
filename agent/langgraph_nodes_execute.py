from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from agent.event_schema import AgentEvent
from agent.output_models import ExecuteOutput
from agent.tool_schemas import ToolContextInput
from utils.config import settings

_SEQUENTIAL_LAST_TOOLS = frozenset({"policy_rulebook_probe", "legacy_aura_deep_audit"})
_PARALLEL_TOOL_DEPENDENCIES: dict[str, frozenset[str]] = {
    "merchant_risk_probe": frozenset({"holiday_compliance_probe"}),
}
_TOOL_CALL_SHORT_MESSAGE: dict[str, str] = {
    "holiday_compliance_probe": "휴일·근태 적격성 확인 중",
    "merchant_risk_probe": "가맹점 업종 위험 점검 중",
    "policy_rulebook_probe": "규정 조항 조회 중",
    "document_evidence_probe": "전표·증빙 수집 중",
    "budget_risk_probe": "예산 초과 점검 중",
    "legacy_aura_deep_audit": "심층 감사 실행 중",
}


async def execute_node_impl(
    state: dict[str, Any],
    *,
    get_tools_by_name: Callable[[], dict[str, Any]],
    should_skip_tool: Callable[[dict[str, Any], list[dict[str, Any]]], tuple[bool, str | None]],
    score: Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]],
    score_hybrid: Callable[[dict[str, Any], dict[str, Any], list[dict[str, Any]]], Awaitable[dict[str, Any]]] | None,
    tool_result_key: Callable[[dict[str, Any]], str],
    compute_plan_achievement: Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]],
    stream_reasoning_events_with_llm: Callable[[str, str], Awaitable[tuple[str, list[dict[str, Any]], str]]],
) -> dict[str, Any]:
    tools_by_name = get_tools_by_name()
    tool_results: list[dict[str, Any]] = []
    skipped_tools: list[str] = []
    failed_tools: list[str] = []
    pending_events: list[dict[str, Any]] = [
        AgentEvent(
            event_type="NODE_START",
            node="execute",
            phase="execute",
            message="계획된 도구를 순차 실행합니다.",
            metadata={"planned_tools": [step.get("tool") for step in state.get("plan") or []]},
        ).to_payload()
    ]
    plan = state.get("plan") or []
    use_parallel = getattr(settings, "enable_parallel_tool_execution", False)

    async def _invoke_one(
        step: dict[str, Any],
        prior: list[dict[str, Any]],
    ) -> dict[str, Any]:
        tool_name = step.get("tool", "")
        tool = tools_by_name.get(tool_name)
        if not tool:
            return {"tool": tool_name, "ok": False, "facts": {}, "summary": f"알 수 없는 도구: {tool_name}"}
        inp = ToolContextInput(
            case_id=state["case_id"],
            body_evidence=state["body_evidence"],
            intended_risk_type=state.get("intended_risk_type"),
            prior_tool_results=list(prior),
        )
        result = await tool.ainvoke(inp.model_dump())
        return result if isinstance(result, dict) else {"tool": tool_name, "ok": False, "facts": {}, "summary": str(result)}

    if use_parallel:
        parallel_steps = [s for s in plan if s.get("tool", "") not in _SEQUENTIAL_LAST_TOOLS]
        sequential_steps = [s for s in plan if s.get("tool", "") in _SEQUENTIAL_LAST_TOOLS]
        planned_tool_names = {s.get("tool", "") for s in plan if s.get("tool", "")}
        finished_parallel_tools: set[str] = set()
        remaining_parallel_steps = list(parallel_steps)

        while remaining_parallel_steps:
            ready_steps: list[dict[str, Any]] = []
            blocked_steps: list[dict[str, Any]] = []

            for step in remaining_parallel_steps:
                tool_name = step.get("tool", "")
                deps = {
                    dep for dep in _PARALLEL_TOOL_DEPENDENCIES.get(tool_name, frozenset())
                    if dep in planned_tool_names
                }
                if deps.issubset(finished_parallel_tools):
                    ready_steps.append(step)
                else:
                    blocked_steps.append(step)

            if not ready_steps and blocked_steps:
                ready_steps = [blocked_steps.pop(0)]

            to_run: list[tuple[dict[str, Any], str]] = []
            for step in ready_steps:
                tool_name = step.get("tool", "")
                skip, reason = should_skip_tool(step, tool_results)
                if skip:
                    tool_obj = tools_by_name.get(tool_name)
                    tool_description = getattr(tool_obj, "description", None) if tool_obj else None
                    msg = reason or "기존 증거가 충분해 생략한다."
                    skipped_tools.append(tool_name)
                    finished_parallel_tools.add(tool_name)
                    pending_events.append(
                        AgentEvent(
                            event_type="TOOL_SKIPPED",
                            node="execute",
                            phase="execute",
                            tool=tool_name,
                            message=msg,
                            thought=msg,
                            action="추가 specialist 호출을 생략한다.",
                            observation=msg,
                            metadata={"reason": reason, "owner": step.get("owner"), "tool_description": tool_description},
                        ).to_payload()
                    )
                    continue
                tool = tools_by_name.get(tool_name)
                if not tool:
                    failed_tools.append(tool_name)
                    finished_parallel_tools.add(tool_name)
                    tool_results.append({"tool": tool_name, "ok": False, "facts": {}, "summary": f"알 수 없는 도구: {tool_name}"})
                    continue
                tool_description = getattr(tool, "description", None) or ""
                step_reason = step.get("reason", "")
                short_msg = _TOOL_CALL_SHORT_MESSAGE.get(tool_name) or f"{step_reason} — {tool_name} 실행."
                pending_events.append(
                    AgentEvent(
                        event_type="TOOL_CALL",
                        node="execute",
                        phase="execute",
                        tool=tool_name,
                        message=short_msg,
                        thought=step_reason,
                        action=f"{tool_name} 실행",
                        observation="도구 실행 중.",
                        metadata={"reason": step_reason, "owner": step.get("owner"), "tool_description": tool_description},
                    ).to_payload()
                )
                to_run.append((step, tool_description))

            if to_run:
                parallel_results = await asyncio.gather(
                    *[_invoke_one(step, tool_results) for step, _ in to_run],
                    return_exceptions=False,
                )
                for (step, tool_description), result in zip(to_run, parallel_results):
                    tool_name = step.get("tool", "")
                    if not result.get("ok"):
                        failed_tools.append(tool_name)
                    tool_results.append(result)
                    finished_parallel_tools.add(tool_name)
                    pending_events.append(
                        AgentEvent(
                            event_type="TOOL_RESULT",
                            node="execute",
                            phase="execute",
                            tool=tool_name,
                            message=result.get("summary") or "도구 결과 수집 완료",
                            thought="수집한 사실을 다음 판단 단계에 반영한다.",
                            action=f"{tool_name} 결과 반영",
                            observation=result.get("summary") or "",
                            metadata={**result, "tool_description": tool_description},
                        ).to_payload()
                    )

            remaining_parallel_steps = blocked_steps
        for step in sequential_steps:
            tool_name = step.get("tool", "")
            skip, reason = should_skip_tool(step, tool_results)
            if skip:
                tool_obj = tools_by_name.get(tool_name)
                tool_description = getattr(tool_obj, "description", None) if tool_obj else None
                msg = reason or "기존 증거가 충분해 생략한다."
                skipped_tools.append(tool_name)
                pending_events.append(
                    AgentEvent(
                        event_type="TOOL_SKIPPED",
                        node="execute",
                        phase="execute",
                        tool=tool_name,
                        message=msg,
                        thought=msg,
                        action="추가 specialist 호출을 생략한다.",
                        observation=msg,
                        metadata={"reason": reason, "owner": step.get("owner"), "tool_description": tool_description},
                    ).to_payload()
                )
                continue
            tool = tools_by_name.get(tool_name)
            if not tool:
                failed_tools.append(tool_name)
                tool_results.append({"tool": tool_name, "ok": False, "facts": {}, "summary": f"알 수 없는 도구: {tool_name}"})
                continue
            tool_description = getattr(tool, "description", None) or ""
            step_reason = step.get("reason", "")
            short_msg = _TOOL_CALL_SHORT_MESSAGE.get(tool_name) or f"{step_reason} — {tool_name} 실행."
            pending_events.append(
                AgentEvent(
                    event_type="TOOL_CALL",
                    node="execute",
                    phase="execute",
                    tool=tool_name,
                    message=short_msg,
                    thought=step_reason,
                    action=f"{tool_name} 실행",
                    observation="도구 실행 중.",
                    metadata={"reason": step_reason, "owner": step.get("owner"), "tool_description": tool_description},
                ).to_payload()
            )
            result = await _invoke_one(step, tool_results)
            if not result.get("ok"):
                failed_tools.append(tool_name)
            tool_results.append(result)
            pending_events.append(
                AgentEvent(
                    event_type="TOOL_RESULT",
                    node="execute",
                    phase="execute",
                    tool=tool_name,
                    message=result.get("summary") or "도구 결과 수집 완료",
                    thought="수집한 사실을 다음 판단 단계에 반영한다.",
                    action=f"{tool_name} 결과 반영",
                    observation=result.get("summary") or "",
                    metadata={**result, "tool_description": tool_description},
                ).to_payload()
            )
    else:
        for step in plan:
            tool_name = step.get("tool", "")
            skip, reason = should_skip_tool(step, tool_results)
            if skip:
                tool_obj = tools_by_name.get(tool_name)
                tool_description = getattr(tool_obj, "description", None) if tool_obj else None
                msg = reason or "기존 증거가 충분해 생략한다."
                skipped_tools.append(tool_name)
                pending_events.append(
                    AgentEvent(
                        event_type="TOOL_SKIPPED",
                        node="execute",
                        phase="execute",
                        tool=tool_name,
                        message=msg,
                        thought=msg,
                        action="추가 specialist 호출을 생략한다.",
                        observation=msg,
                        metadata={"reason": reason, "owner": step.get("owner"), "tool_description": tool_description},
                    ).to_payload()
                )
                continue
            tool = tools_by_name.get(tool_name)
            if not tool:
                failed_tools.append(tool_name)
                tool_results.append({"tool": tool_name, "ok": False, "facts": {}, "summary": f"알 수 없는 도구: {tool_name}"})
                continue
            tool_description = getattr(tool, "description", None) or ""
            step_reason = step.get("reason", "")
            short_msg = _TOOL_CALL_SHORT_MESSAGE.get(tool_name) or f"{step_reason} — {tool_name} 실행."
            pending_events.append(
                AgentEvent(
                    event_type="TOOL_CALL",
                    node="execute",
                    phase="execute",
                    tool=tool_name,
                    message=short_msg,
                    thought=step_reason,
                    action=f"{tool_name} 실행",
                    observation="도구 실행 중.",
                    metadata={"reason": step_reason, "owner": step.get("owner"), "tool_description": tool_description},
                ).to_payload()
            )
            inp = ToolContextInput(
                case_id=state["case_id"],
                body_evidence=state["body_evidence"],
                intended_risk_type=state.get("intended_risk_type"),
                prior_tool_results=list(tool_results),
            )
            result = await tool.ainvoke(inp.model_dump())
            if not isinstance(result, dict):
                result = {"tool": tool_name, "ok": False, "facts": {}, "summary": str(result)}
            if not bool(result.get("ok")):
                failed_tools.append(tool_name)
            tool_results.append(result)
            result_summary = result.get("summary") or "도구 결과 수집 완료"
            pending_events.append(
                AgentEvent(
                    event_type="TOOL_RESULT",
                    node="execute",
                    phase="execute",
                    tool=tool_name,
                    message=result_summary,
                    thought="수집한 사실을 다음 판단 단계에 반영한다.",
                    action=f"{tool_name} 결과 반영",
                    observation=result_summary,
                    metadata={**result, "tool_description": tool_description},
                ).to_payload()
            )
    if score_hybrid is not None:
        score_breakdown = await score_hybrid(state, state["flags"], tool_results)
    else:
        score_breakdown = score(state["flags"], tool_results)
    trace = score_breakdown.get("calculation_trace", "")
    final_decision = str(score_breakdown.get("final_decision") or "-")
    conflict_warning = bool(score_breakdown.get("conflict_warning"))
    pending_events.append({
        "event_type": "SCORE_BREAKDOWN",
        "message": (
            f"정책점수 {score_breakdown['policy_score']}점 / 근거점수 {score_breakdown['evidence_score']}점 / "
            f"최종 {score_breakdown['final_score']}점 [{score_breakdown.get('severity', '-')}] "
            f"/ 판정 {final_decision} — {trace}"
        ),
        "node": "execute",
        "phase": "execute",
        "metadata": score_breakdown,
    })
    if conflict_warning:
        pending_events.append(
            AgentEvent(
                event_type="RISK_CONFLICT",
                node="execute",
                phase="execute",
                message="판단 불일치 주의: 규칙 점수와 LLM Judge 점수 편차가 큽니다.",
                metadata={
                    "rule_score": score_breakdown.get("rule_score"),
                    "llm_score": score_breakdown.get("llm_score"),
                    "diagnostic_log": score_breakdown.get("diagnostic_log"),
                },
            ).to_payload()
        )
    executed_tools = [tool_result_key(r) for r in tool_results if tool_result_key(r)]
    reasoning_parts = [
        f"{len(executed_tools)}개 도구를 실행해 정책점수 {score_breakdown['policy_score']}점, 근거점수 {score_breakdown['evidence_score']}점을 산출했다.",
        "수집된 도구 결과를 critic 단계의 반박 가능성 검토 입력으로 전달한다.",
    ]
    if skipped_tools:
        reasoning_parts.append(f"생략 도구: {', '.join(skipped_tools)}.")
    if failed_tools:
        reasoning_parts.append(f"실패 도구: {', '.join(sorted(set(failed_tools)))}.")
    execute_reasoning = " ".join(reasoning_parts).strip()
    execute_context = {
        "executed_tools": executed_tools,
        "skipped_tools": skipped_tools,
        "failed_tools": sorted(set(failed_tools)),
        "score": score_breakdown,
        "last_node_summary": state.get("last_node_summary", "없음"),
    }
    execute_reasoning, execute_reasoning_events, note_source = await stream_reasoning_events_with_llm(
        "execute",
        execute_reasoning,
        context=execute_context,
    )
    pending_events.extend(execute_reasoning_events)
    execute_output = ExecuteOutput(
        executed_tools=executed_tools,
        skipped_tools=skipped_tools,
        failed_tools=sorted(set(failed_tools)),
        policy_score=int(score_breakdown.get("policy_score") or 0),
        evidence_score=int(score_breakdown.get("evidence_score") or 0),
        final_score=int(score_breakdown.get("final_score") or 0),
        reasoning=execute_reasoning,
    )
    evaluation_history = list(state.get("evaluation_history") or [])
    evaluation_history.append(
        {
            "iteration": len(evaluation_history),
            "rule_score": int(score_breakdown.get("rule_score") or score_breakdown.get("final_score") or 0),
            "llm_score": int(score_breakdown.get("llm_score") or score_breakdown.get("final_score") or 0),
            "final_score": int(score_breakdown.get("final_score") or 0),
            "verification_gate": str(score_breakdown.get("verification_gate") or "pass"),
            "fallback_used": bool(score_breakdown.get("fallback_used")),
            "fallback_reason": score_breakdown.get("fallback_reason"),
        }
    )
    pending_events.append(
        AgentEvent(
            event_type="NODE_END",
            node="execute",
            phase="execute",
            message="도구 실행과 점수 산출이 완료되었습니다.",
            metadata={"executed_tools": len(tool_results), "reasoning": execute_reasoning, "note_source": note_source},
        ).to_payload()
    )
    plan_achievement = compute_plan_achievement(state.get("plan") or [], tool_results)
    return {
        "tool_results": tool_results,
        "score_breakdown": score_breakdown,
        "execute_output": execute_output.model_dump(),
        "pending_events": pending_events,
        "plan_achievement": plan_achievement,
        "rule_score": int(score_breakdown.get("rule_score") or score_breakdown.get("final_score") or 0),
        "llm_score": int(score_breakdown.get("llm_score") or score_breakdown.get("final_score") or 0),
        "final_score": int(score_breakdown.get("final_score") or 0),
        "verification_gate": str(score_breakdown.get("verification_gate") or "pass"),
        "summary_reason": str(score_breakdown.get("summary_reason") or ""),
        "diagnostic_log": str(score_breakdown.get("diagnostic_log") or ""),
        "fidelity": int(score_breakdown.get("fidelity") or 0),
        "rule_fidelity": int(score_breakdown.get("rule_fidelity") or 0),
        "llm_fidelity": int(score_breakdown.get("llm_fidelity") or 0),
        "fallback_used": bool(score_breakdown.get("fallback_used")),
        "fallback_reason": str(score_breakdown.get("fallback_reason") or ""),
        "retry_count": int(score_breakdown.get("retry_count") or 0),
        "max_retries": int(score_breakdown.get("max_retries") or 2),
        "latency_ms": {"llm_judge": float(score_breakdown.get("latency_ms") or 0.0)},
        "version_meta": dict(score_breakdown.get("version_meta") or {}),
        "evaluation_history": evaluation_history,
        "last_node_summary": f"execute 완료: {execute_reasoning[:60]}…" if len(execute_reasoning) > 60 else f"execute 완료: {execute_reasoning}",
    }
