from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent.event_schema import AgentEvent
from agent.hitl import build_hitl_request
from agent.skills import SKILL_REGISTRY


class AgentState(TypedDict, total=False):
    case_id: str
    body_evidence: dict[str, Any]
    intended_risk_type: str | None
    flags: dict[str, Any]
    plan: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    score_breakdown: dict[str, Any]
    critique: dict[str, Any]
    verification: dict[str, Any]
    hitl_request: dict[str, Any] | None
    final_result: dict[str, Any]
    pending_events: list[dict[str, Any]]


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


def _find_tool_result(tool_results: list[dict[str, Any]], skill_name: str) -> dict[str, Any] | None:
    return next((result for result in tool_results if result.get("skill") == skill_name), None)


def _top_policy_refs(tool_results: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    refs = (_find_tool_result(tool_results, "policy_rulebook_probe") or {}).get("facts", {}).get("policy_refs") or []
    return list(refs[:limit])


def _should_skip_skill(step: dict[str, Any], *, state: AgentState, tool_results: list[dict[str, Any]]) -> tuple[bool, str | None]:
    tool = step["tool"]
    if tool != "legacy_aura_deep_audit":
        return False, None

    policy_ref_count = (_find_tool_result(tool_results, "policy_rulebook_probe") or {}).get("facts", {}).get("ref_count", 0)
    line_item_count = (_find_tool_result(tool_results, "document_evidence_probe") or {}).get("facts", {}).get("lineItemCount", 0)
    has_missing_fields = bool(((state["body_evidence"].get("dataQuality") or {}).get("missingFields") or []))
    budget_exceeded = bool(state.get("flags", {}).get("budgetExceeded"))

    # 규정 근거와 전표 증거가 충분하고 입력 누락이 없으면 legacy specialist 호출을 생략한다.
    if policy_ref_count >= 2 and line_item_count > 0 and not has_missing_fields and not budget_exceeded:
        return True, "규정 근거와 전표 증거가 충분해 추가 legacy 심층 분석 없이 진행합니다."
    return False, None


def _build_grounded_reason(state: AgentState) -> tuple[str, str]:
    score = state["score_breakdown"]
    hitl_request = state.get("hitl_request")
    body = state["body_evidence"]
    occurred_at = _format_occurred_at(body.get("occurredAt"))
    merchant = body.get("merchantName") or "거래처 미상"
    refs = _top_policy_refs(state.get("tool_results", []), limit=2)
    ref_labels: list[str] = []
    for ref in refs:
        article = ref.get("article") or "조항 미상"
        parent_title = ref.get("parent_title")
        label = article
        if parent_title:
            label = f"{label} ({parent_title})"
        ref_labels.append(label)

    intro = f"전표는 {occurred_at} 시점 {merchant} 사용 건으로 분석되었습니다. "
    score_text = f"정책점수 {score['policy_score']}점, 근거점수 {score['evidence_score']}점이 반영되었습니다. "
    if ref_labels:
        grounding = f"관련 규정 근거는 {', '.join(ref_labels)}입니다. "
    else:
        grounding = "현재 직접 연결된 규정 근거는 제한적입니다. "

    if hitl_request:
        status = "HITL_REQUIRED"
        tail = "현재는 사람 검토가 필요한 상태로 분류되었으며, 추가 소명 확인 후 최종 확정이 필요합니다."
    else:
        if state["flags"].get("hasHitlResponse"):
            status = "REVIEW_AFTER_HITL"
            tail = "사람 검토 응답이 반영되었으며, 재평가 결과 검토 대상으로 유지됩니다."
        else:
            status = "REVIEW_REQUIRED"
            tail = "현재 수집된 증거 기준으로 우선 검토 대상입니다."
    return intro + grounding + score_text + tail, status


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
        plan.append({"tool": "merchant_risk_probe", "reason": "업종/MCC 위험 확인", "owner": "planner"})
    plan.append({"tool": "document_evidence_probe", "reason": "전표 증거 수집", "owner": "specialist"})
    plan.append({"tool": "policy_rulebook_probe", "reason": "내부 규정 조항 조회", "owner": "specialist"})
    plan.append({"tool": "legacy_aura_deep_audit", "reason": "기존 Aura 심층 검토", "owner": "specialist"})
    return plan


def _score(flags: dict[str, Any], tool_results: list[dict[str, Any]]) -> dict[str, Any]:
    policy_score = 0
    evidence_score = 30
    reasons: list[str] = []
    if flags.get("isHoliday"):
        policy_score += 35
        reasons.append("휴일 사용 정황")
    if flags.get("hrStatus") in {"LEAVE", "OFF", "VACATION"}:
        policy_score += 20
        reasons.append("근태 상태 충돌")
    if flags.get("isNight"):
        policy_score += 10
        reasons.append("심야 시간대")
    if flags.get("budgetExceeded"):
        policy_score += 10
        reasons.append("예산 초과")
    if any(r.get("skill") == "document_evidence_probe" and (r.get("facts") or {}).get("lineItemCount", 0) > 0 for r in tool_results):
        evidence_score += 20
        reasons.append("전표 라인아이템 확보")
    if any(r.get("skill") == "policy_rulebook_probe" and (r.get("facts") or {}).get("ref_count", 0) > 0 for r in tool_results):
        evidence_score += 20
        reasons.append("규정 조항 확보")
    if any(r.get("skill") == "legacy_aura_deep_audit" and r.get("facts") for r in tool_results):
        evidence_score += 20
        reasons.append("심층 감사 결과 확보")
    if flags.get("hasHitlResponse"):
        evidence_score += 10
        reasons.append("사람 검토 응답 확보")
    final_score = min(100, int(policy_score * 0.6 + evidence_score * 0.4))
    return {
        "policy_score": policy_score,
        "evidence_score": evidence_score,
        "final_score": final_score,
        "reasons": reasons,
    }


async def intake_node(state: AgentState) -> AgentState:
    flags = _derive_flags(state["body_evidence"])
    return {
        "flags": flags,
        "pending_events": [
            AgentEvent(event_type="NODE_START", node="intake", phase="analyze", message="입력 데이터를 정규화합니다.", thought="전표 입력값에서 휴일, 시간대, 근태, 예산, 업종 신호를 먼저 추출해야 합니다.", action="입력 필드와 파생 신호를 정규화합니다.", metadata={"role": "intake_agent"}).to_payload(),
            AgentEvent(event_type="NODE_END", node="intake", phase="analyze", message="입력 정규화가 완료되었습니다.", observation="핵심 위험 신호를 추출했습니다.", metadata={"role": "intake_agent", **flags}).to_payload(),
        ],
    }


async def planner_node(state: AgentState) -> AgentState:
    plan = _plan_from_flags(state["flags"])
    return {
        "plan": plan,
        "pending_events": [
            AgentEvent(event_type="NODE_START", node="planner", phase="plan", message="조사 계획을 수립합니다.", thought="어떤 규정과 어떤 도구가 이 케이스를 가장 빨리 설명할 수 있는지 우선순위를 정해야 합니다.", action="위험 신호별 조사 순서를 계산합니다.", metadata={"role": "planner_agent"}).to_payload(),
            AgentEvent(event_type="PLAN_READY", node="planner", phase="plan", message="조사 계획이 확정되었습니다.", observation="도구 선택 순서와 조사 의도를 결정했습니다.", metadata={"role": "planner_agent", "plan": plan}).to_payload(),
        ],
    }


async def execute_node(state: AgentState) -> AgentState:
    tool_results: list[dict[str, Any]] = []
    pending_events: list[dict[str, Any]] = []
    for step in state["plan"]:
        skip, reason = _should_skip_skill(step, state=state, tool_results=tool_results)
        if skip:
            pending_events.append(
                AgentEvent(
                    event_type="TOOL_SKIPPED",
                    node="executor",
                    phase="execute",
                    tool=step["tool"],
                    message=f"도구 생략: {step['tool']}",
                    thought="이미 수집된 규정/전표 근거가 충분하므로 추가 specialist 호출 비용을 줄일 수 있습니다.",
                    action="legacy specialist 호출 생략",
                    observation=reason,
                    metadata={"reason": reason, "role": "specialist_agent", "owner": step.get("owner")},
                ).to_payload()
            )
            continue
        skill = SKILL_REGISTRY[step["tool"]]
        pending_events.append(
            AgentEvent(
                event_type="TOOL_CALL",
                node="executor",
                phase="execute",
                tool=skill.name,
                message=f"도구 호출: {skill.name}",
                thought=f"{step['reason']}에 대한 사실을 확보해야 합니다.",
                action=f"{skill.name} 실행",
                metadata={"reason": step["reason"], "role": "specialist_agent", "owner": step.get("owner")},
            ).to_payload()
        )
        result = await skill.handler({
            "case_id": state["case_id"],
            "body_evidence": state["body_evidence"],
            "intended_risk_type": state.get("intended_risk_type"),
        })
        tool_results.append(result)
        pending_events.append(
            AgentEvent(
                event_type="TOOL_RESULT",
                node="executor",
                phase="execute",
                tool=skill.name,
                message=result.get("summary") or f"도구 완료: {skill.name}",
                observation=(result.get("summary") or "도구 결과 수집 완료"),
                metadata={**result, "role": "specialist_agent"},
            ).to_payload()
        )
    score = _score(state["flags"], tool_results)
    pending_events.append({
        "event_type": "SCORE_BREAKDOWN",
        "message": f"정책점수 {score['policy_score']}, 근거점수 {score['evidence_score']}, 최종점수 {score['final_score']}",
        "node": "executor",
        "phase": "execute",
        "metadata": score,
    })
    return {"tool_results": tool_results, "score_breakdown": score, "pending_events": pending_events}


async def critic_node(state: AgentState) -> AgentState:
    legacy = next((r for r in state.get("tool_results", []) if r.get("skill") == "legacy_aura_deep_audit"), None)
    missing = ((state["body_evidence"].get("dataQuality") or {}).get("missingFields") or [])
    critique = {
        "has_legacy_result": bool(legacy and legacy.get("facts")),
        "missing_fields": missing,
        "risk_of_overclaim": bool(missing),
        "recommend_hold": bool(missing and not state["flags"].get("hasHitlResponse")),
    }
    return {
        "critique": critique,
        "pending_events": [
            AgentEvent(event_type="NODE_START", node="critic", phase="reflect", message="전문 도구 결과와 입력 품질을 교차 검토합니다.", thought="성급한 결론을 막기 위해 입력 품질과 근거 일치성을 다시 검토해야 합니다.", action="과잉 주장 가능성과 누락 필드를 점검합니다.", metadata={"role": "critic_agent"}).to_payload(),
            AgentEvent(event_type="NODE_END", node="critic", phase="reflect", message="비판적 재검토가 완료되었습니다.", observation="과잉 주장 위험과 추가 검토 필요 여부를 정리했습니다.", metadata={"role": "critic_agent", **critique}).to_payload(),
        ],
    }


async def verify_node(state: AgentState) -> AgentState:
    hitl_request = build_hitl_request(state["body_evidence"], state["tool_results"])
    if state["flags"].get("hasHitlResponse"):
        hitl_request = None
    needs_hitl = bool(hitl_request) or bool(state.get("critique", {}).get("recommend_hold") and not state["flags"].get("hasHitlResponse"))
    verification = {
        "needs_hitl": needs_hitl,
        "quality_signals": ["HITL_REQUIRED"] if needs_hitl else ["OK"],
    }
    events = [
        AgentEvent(event_type="NODE_START", node="verifier", phase="verify", message="근거 정합성과 추가 검토 필요 여부를 확인합니다.", thought="현재 증거만으로 확정할 수 있는지, 사람 검토가 필요한지 결정해야 합니다.", action="검증 게이트와 HITL 조건을 평가합니다.", metadata={"role": "verifier_agent"}).to_payload(),
        AgentEvent(event_type="GATE_APPLIED", node="verifier", phase="verify", message="검증 게이트 적용이 완료되었습니다.", decision_code="HITL_REQUIRED" if needs_hitl else "READY", observation=("사람 검토 필요" if needs_hitl else "자동 진행 가능"), metadata={"role": "verifier_agent", **verification}).to_payload(),
    ]
    if hitl_request:
        events.append(
            AgentEvent(event_type="HITL_REQUESTED", node="verifier", phase="verify", message="사람 검토가 필요한 케이스로 분류되었습니다.", decision_code="HITL_REQUIRED", thought="입력 또는 근거가 부족해 자동 확정은 위험합니다.", action="재무 검토자에게 소명 요청을 생성합니다.", metadata=hitl_request).to_payload()
        )
    return {"verification": verification, "hitl_request": hitl_request, "pending_events": events}


async def reporter_node(state: AgentState) -> AgentState:
    score = state["score_breakdown"]
    hitl_request = state.get("hitl_request")
    body = state["body_evidence"]
    occurred_at = _format_occurred_at(body.get("occurredAt"))
    merchant = body.get("merchantName") or "거래처 미상"
    summary = (
        f"전표는 {occurred_at} 시점 {merchant} 사용 건으로 분석되었습니다. "
        f"정책점수 {score['policy_score']}점, 근거점수 {score['evidence_score']}점, 최종점수 {score['final_score']}점입니다."
    )
    if hitl_request:
        summary += " 사람 검토가 필요한 상태입니다."
    else:
        summary += " 현재 수집된 증거 기준으로 추가 검토 우선순위가 높습니다."
    return {
        "pending_events": [
            AgentEvent(event_type="NODE_START", node="reporter", phase="report", message="사용자에게 제시할 보고 문안을 구성합니다.", thought="분석 과정에서 모은 사실과 검증 결과를 사람이 이해할 수 있는 문장으로 바꿔야 합니다.", action="보고용 설명 문장을 구성합니다.", metadata={"role": "reporter_agent"}).to_payload(),
            AgentEvent(event_type="NODE_END", node="reporter", phase="report", message=summary, observation="사용자용 요약 문안이 준비되었습니다.", metadata={"role": "reporter_agent"}).to_payload(),
        ],
    }


async def finalizer_node(state: AgentState) -> AgentState:
    score = state["score_breakdown"]
    reason, status = _build_grounded_reason(state)
    final = {
        "caseId": state["case_id"],
        "status": status,
        "reasonText": reason,
        "score": score["final_score"] / 100,
        "severity": "HIGH" if score["final_score"] >= 70 else ("MEDIUM" if score["final_score"] >= 40 else "LOW"),
        "analysis_mode": "langgraph_agentic",
        "score_breakdown": score,
        "quality_gate_codes": state["verification"]["quality_signals"],
        "hitl_request": hitl_request,
        "tool_results": state["tool_results"],
        "policy_refs": (_find_tool_result(state["tool_results"], "policy_rulebook_probe") or {}).get("facts", {}).get("policy_refs", []) or [],
        "critique": state.get("critique"),
        "hitl_response": (state["body_evidence"].get("hitlResponse") or None),
    }
    return {
        "final_result": final,
        "pending_events": [
            AgentEvent(event_type="NODE_END", node="finalizer", phase="finalize", message="최종 분석 결과가 생성되었습니다.", observation=f"최종 상태={status}", metadata={"status": status}).to_payload(),
        ],
    }


def build_agent_graph():
    workflow = StateGraph(AgentState)
    workflow.add_node("intake", intake_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("execute", execute_node)
    workflow.add_node("critic", critic_node)
    workflow.add_node("verify", verify_node)
    workflow.add_node("reporter", reporter_node)
    workflow.add_node("finalizer", finalizer_node)
    workflow.add_edge(START, "intake")
    workflow.add_edge("intake", "planner")
    workflow.add_edge("planner", "execute")
    workflow.add_edge("execute", "critic")
    workflow.add_edge("critic", "verify")
    workflow.add_edge("verify", "reporter")
    workflow.add_edge("reporter", "finalizer")
    workflow.add_edge("finalizer", END)
    return workflow.compile()


async def run_langgraph_agentic_analysis(
    case_id: str,
    *,
    body_evidence: dict[str, Any],
    intended_risk_type: str | None = None,
):
    graph = build_agent_graph()
    initial_state: AgentState = {
        "case_id": case_id,
        "body_evidence": body_evidence,
        "intended_risk_type": intended_risk_type,
    }
    async for chunk in graph.astream(initial_state, stream_mode="updates"):
        for _node, update in chunk.items():
            for ev in update.get("pending_events", []) or []:
                if ev.get("event_type") == "SCORE_BREAKDOWN":
                    yield "confidence", {
                        "label": "RISK_SCORE_BREAKDOWN",
                        "detail": ev.get("message"),
                        "score_breakdown": (ev.get("metadata") or {}),
                    }
                else:
                    yield "AGENT_EVENT", ev
            final = update.get("final_result")
            if final is not None:
                yield "completed", final
