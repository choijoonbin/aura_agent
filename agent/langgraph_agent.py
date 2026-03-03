from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent.event_schema import AgentEvent
from agent.hitl import build_hitl_request
from agent.reasoning_notes import generate_working_note
from agent.screener import run_screening
from agent.skills import SKILL_REGISTRY
from utils.config import settings


class AgentState(TypedDict, total=False):
    case_id: str
    body_evidence: dict[str, Any]
    intended_risk_type: str | None
    # Screening result populated by screener_node (before intake)
    screening_result: dict[str, Any] | None
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
    if settings.enable_legacy_aura_specialist:
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


async def screener_node(state: AgentState) -> AgentState:
    """
    Phase 0 — Screening.
    Runs deterministic signal analysis on raw body_evidence to classify
    the case_type BEFORE the agent begins deep analysis.
    This mirrors the original aura-platform /detect/screen flow.
    """
    body = state["body_evidence"]

    # If case_type was already screened (AgentCase exists), skip re-screening
    # but still emit the SCREENING_RESULT event so it shows in the stream.
    pre_classified = body.get("case_type") or body.get("intended_risk_type")
    if pre_classified and pre_classified not in ("UNKNOWN", "NORMAL_BASELINE", ""):
        screening = {
            "case_type": pre_classified,
            "severity": body.get("severity") or "MEDIUM",
            "score": body.get("screening_score") or 0,
            "reasons": ["기존 스크리닝 결과를 사용합니다."],
            "reason_text": f"기존 분류: {pre_classified}",
        }
    else:
        screening = run_screening(body)

    # Propagate screened case_type into body_evidence so downstream nodes use it
    updated_body = {**body, "case_type": screening["case_type"], "intended_risk_type": screening["case_type"]}

    label_map = {
        "HOLIDAY_USAGE": "휴일/휴무 중 사용 의심",
        "LIMIT_EXCEED": "한도 초과 의심",
        "PRIVATE_USE_RISK": "사적 사용 위험",
        "UNUSUAL_PATTERN": "비정상 패턴",
        "NORMAL_BASELINE": "정상 범위",
    }
    label = label_map.get(screening["case_type"], screening["case_type"])
    severity = screening.get("severity", "MEDIUM")
    score = screening.get("score", 0)
    reason_text = screening.get("reason_text", "")

    return {
        "body_evidence": updated_body,
        "intended_risk_type": screening["case_type"],
        "screening_result": screening,
        "pending_events": [
            AgentEvent(
                event_type="NODE_START",
                node="screener",
                phase="screen",
                message=f"전표 신호 분석을 시작합니다.",
                thought="원시 전표 데이터에서 위반 유형을 식별합니다.",
                action="결정론적 신호 추출 및 스코어링",
                observation="hr_status, mcc_code, budat, cputm 등 신호를 분석합니다.",
                metadata={"role": "screener"},
            ).to_payload(),
            AgentEvent(
                event_type="SCREENING_RESULT",
                node="screener",
                phase="screen",
                message=f"스크리닝 완료: [{label}] — 중요도 {severity} / 점수 {score}",
                thought=f"데이터 신호 기반으로 '{screening['case_type']}' 유형으로 분류되었습니다.",
                action="케이스 유형 분류 완료",
                observation=reason_text,
                metadata={
                    "case_type": screening["case_type"],
                    "severity": severity,
                    "score": score,
                    "reasons": screening.get("reasons", []),
                },
            ).to_payload(),
        ],
    }


async def intake_node(state: AgentState) -> AgentState:
    flags = _derive_flags(state["body_evidence"])
    start_note = await generate_working_note(
        node="intake",
        role="intake_agent",
        context={
            "case_id": state["case_id"],
            "occurredAt": state["body_evidence"].get("occurredAt"),
            "merchantName": state["body_evidence"].get("merchantName"),
            "amount": state["body_evidence"].get("amount"),
            "raw_body_keys": sorted(state["body_evidence"].keys()),
        },
        fallback_message="입력 데이터를 정규화합니다.",
        fallback_thought="전표 입력값에서 핵심 위험 신호를 추출해야 합니다.",
        fallback_action="입력 필드와 파생 신호를 정규화합니다.",
        fallback_observation="정규화 전 입력 점검을 시작했습니다.",
    )
    end_note = await generate_working_note(
        node="intake",
        role="intake_agent",
        context={
            "case_id": state["case_id"],
            "flags": flags,
        },
        fallback_message="입력 정규화가 완료되었습니다.",
        fallback_thought="추출된 신호를 다음 계획 단계로 넘길 수 있습니다.",
        fallback_action="핵심 위험 신호를 구조화했습니다.",
        fallback_observation="핵심 위험 신호를 추출했습니다.",
    )
    return {
        "flags": flags,
        "pending_events": [
            AgentEvent(
                event_type="NODE_START",
                node="intake",
                phase="analyze",
                message=start_note["message"],
                thought=start_note["thought"],
                action=start_note["action"],
                observation=start_note["observation"],
                metadata={"role": "intake_agent", "note_source": start_note.get("source", "fallback"), "note_model": start_note.get("note_model")},
            ).to_payload(),
            AgentEvent(
                event_type="NODE_END",
                node="intake",
                phase="analyze",
                message=end_note["message"],
                thought=end_note["thought"],
                action=end_note["action"],
                observation=end_note["observation"],
                metadata={"role": "intake_agent", "note_source": end_note.get("source", "fallback"), "note_model": end_note.get("note_model"), **flags},
            ).to_payload(),
        ],
    }


async def planner_node(state: AgentState) -> AgentState:
    plan = _plan_from_flags(state["flags"])
    start_note = await generate_working_note(
        node="planner",
        role="planner_agent",
        context={
            "case_id": state["case_id"],
            "flags": state["flags"],
        },
        fallback_message="조사 계획을 수립합니다.",
        fallback_thought="위험 신호별로 어떤 조사 순서가 효율적인지 정해야 합니다.",
        fallback_action="위험 신호별 조사 순서를 계산합니다.",
        fallback_observation="계획 수립에 필요한 신호를 검토합니다.",
    )
    plan_ready_note = await generate_working_note(
        node="planner",
        role="planner_agent",
        context={
            "case_id": state["case_id"],
            "flags": state["flags"],
            "plan": plan,
        },
        fallback_message="조사 계획이 확정되었습니다.",
        fallback_thought="수집 우선순위와 생략 가능한 경로를 함께 정리했습니다.",
        fallback_action="도구 선택 순서와 조사 의도를 결정했습니다.",
        fallback_observation="도구 선택 순서와 조사 의도를 결정했습니다.",
    )
    return {
        "plan": plan,
        "pending_events": [
            AgentEvent(
                event_type="NODE_START",
                node="planner",
                phase="plan",
                message=start_note["message"],
                thought=start_note["thought"],
                action=start_note["action"],
                observation=start_note["observation"],
                metadata={"role": "planner_agent", "note_source": start_note.get("source", "fallback"), "note_model": start_note.get("note_model")},
            ).to_payload(),
            AgentEvent(
                event_type="PLAN_READY",
                node="planner",
                phase="plan",
                message=plan_ready_note["message"],
                thought=plan_ready_note["thought"],
                action=plan_ready_note["action"],
                observation=plan_ready_note["observation"],
                metadata={"role": "planner_agent", "note_source": plan_ready_note.get("source", "fallback"), "note_model": plan_ready_note.get("note_model"), "plan": plan},
            ).to_payload(),
        ],
    }


async def execute_node(state: AgentState) -> AgentState:
    tool_results: list[dict[str, Any]] = []
    pending_events: list[dict[str, Any]] = []
    for step in state["plan"]:
        skip, reason = _should_skip_skill(step, state=state, tool_results=tool_results)
        if skip:
            skip_note = await generate_working_note(
                node="execute",
                role="specialist_agent",
                context={
                    "case_id": state["case_id"],
                    "tool": step["tool"],
                    "reason": step["reason"],
                    "existing_tool_results": tool_results,
                    "skip_reason": reason,
                },
                fallback_message=f"도구 생략: {step['tool']}",
                fallback_thought="이미 확보된 증거로 다음 판단을 이어갈 수 있습니다.",
                fallback_action="추가 specialist 호출을 생략합니다.",
                fallback_observation=reason or "기존 증거가 충분합니다.",
            )
            skipped_skill = SKILL_REGISTRY.get(step["tool"])
            pending_events.append(
                AgentEvent(
                    event_type="TOOL_SKIPPED",
                    node="executor",
                    phase="execute",
                    tool=step["tool"],
                    message=skip_note["message"],
                    thought=skip_note["thought"],
                    action=skip_note["action"],
                    observation=skip_note["observation"],
                    metadata={
                        "reason": reason,
                        "role": "specialist_agent",
                        "owner": step.get("owner"),
                        "note_source": skip_note.get("source", "fallback"),
                        "note_model": skip_note.get("note_model"),
                        "tool_description": skipped_skill.description if skipped_skill else None,
                    },
                ).to_payload()
            )
            continue
        skill = SKILL_REGISTRY[step["tool"]]
        call_note = await generate_working_note(
            node="execute",
            role="specialist_agent",
            context={
                "case_id": state["case_id"],
                "tool": skill.name,
                "reason": step["reason"],
                "flags": state.get("flags"),
                "tool_results_so_far": [r.get("skill") for r in tool_results],
            },
            fallback_message=f"도구 호출: {skill.name}",
            fallback_thought=f"{step['reason']}에 대한 사실을 확보해야 합니다.",
            fallback_action=f"{skill.name} 실행",
            fallback_observation="도구 실행 전 상태를 정리했습니다.",
        )
        pending_events.append(
            AgentEvent(
                event_type="TOOL_CALL",
                node="executor",
                phase="execute",
                tool=skill.name,
                message=call_note["message"],
                thought=call_note["thought"],
                action=call_note["action"],
                observation=call_note["observation"],
                metadata={
                    "reason": step["reason"],
                    "role": "specialist_agent",
                    "owner": step.get("owner"),
                    "note_source": call_note.get("source", "fallback"),
                    "note_model": call_note.get("note_model"),
                    "tool_description": skill.description,
                },
            ).to_payload()
        )
        result = await skill.handler({
            "case_id": state["case_id"],
            "body_evidence": state["body_evidence"],
            "intended_risk_type": state.get("intended_risk_type"),
        })
        tool_results.append(result)
        result_note = await generate_working_note(
            node="execute",
            role="specialist_agent",
            context={
                "case_id": state["case_id"],
                "tool": skill.name,
                "reason": step["reason"],
                "tool_result": result,
            },
            fallback_message=result.get("summary") or f"도구 완료: {skill.name}",
            fallback_thought="수집한 사실이 다음 판단 단계에 어떤 영향을 주는지 정리합니다.",
            fallback_action=f"{skill.name} 결과를 반영합니다.",
            fallback_observation=(result.get("summary") or "도구 결과 수집 완료"),
        )
        pending_events.append(
            AgentEvent(
                event_type="TOOL_RESULT",
                node="executor",
                phase="execute",
                tool=skill.name,
                message=result_note["message"],
                thought=result_note["thought"],
                action=result_note["action"],
                observation=result_note["observation"],
                metadata={
                    **result,
                    "role": "specialist_agent",
                    "note_source": result_note.get("source", "fallback"),
                    "note_model": result_note.get("note_model"),
                    "tool_description": skill.description,
                },
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
    start_note = await generate_working_note(
        node="critic",
        role="critic_agent",
        context={
            "case_id": state["case_id"],
            "tool_results": state.get("tool_results", []),
            "missing_fields": missing,
        },
        fallback_message="전문 도구 결과와 입력 품질을 교차 검토합니다.",
        fallback_thought="입력 누락과 근거 부족을 점검해 과잉 주장을 막아야 합니다.",
        fallback_action="과잉 주장 가능성과 누락 필드를 점검합니다.",
        fallback_observation="비판적 재검토를 시작했습니다.",
    )
    end_note = await generate_working_note(
        node="critic",
        role="critic_agent",
        context={
            "case_id": state["case_id"],
            "critique": critique,
        },
        fallback_message="비판적 재검토가 완료되었습니다.",
        fallback_thought="확정 주장 전에 추가 보류 조건을 정리했습니다.",
        fallback_action="과잉 주장 위험과 추가 검토 필요 여부를 정리했습니다.",
        fallback_observation="과잉 주장 위험과 추가 검토 필요 여부를 정리했습니다.",
    )
    return {
        "critique": critique,
        "pending_events": [
            AgentEvent(
                event_type="NODE_START",
                node="critic",
                phase="reflect",
                message=start_note["message"],
                thought=start_note["thought"],
                action=start_note["action"],
                observation=start_note["observation"],
                metadata={"role": "critic_agent", "note_source": start_note.get("source", "fallback"), "note_model": start_note.get("note_model")},
            ).to_payload(),
            AgentEvent(
                event_type="NODE_END",
                node="critic",
                phase="reflect",
                message=end_note["message"],
                thought=end_note["thought"],
                action=end_note["action"],
                observation=end_note["observation"],
                metadata={"role": "critic_agent", "note_source": end_note.get("source", "fallback"), "note_model": end_note.get("note_model"), **critique},
            ).to_payload(),
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
    start_note = await generate_working_note(
        node="verify",
        role="verifier_agent",
        context={
            "case_id": state["case_id"],
            "critique": state.get("critique"),
            "tool_results": state.get("tool_results", []),
        },
        fallback_message="근거 정합성과 추가 검토 필요 여부를 확인합니다.",
        fallback_thought="사람 검토가 필요한 조건인지 게이트를 판정해야 합니다.",
        fallback_action="검증 게이트와 HITL 조건을 평가합니다.",
        fallback_observation="검증 게이트 적용 전 상태를 정리했습니다.",
    )
    gate_note = await generate_working_note(
        node="verify",
        role="verifier_agent",
        context={
            "case_id": state["case_id"],
            "verification": verification,
            "hitl_request": hitl_request,
        },
        fallback_message="검증 게이트 적용이 완료되었습니다.",
        fallback_thought="자동 진행 여부와 사람 검토 전환 여부를 결정했습니다.",
        fallback_action="검증 게이트 결과를 확정했습니다.",
        fallback_observation=("사람 검토 필요" if needs_hitl else "자동 진행 가능"),
    )
    events = [
        AgentEvent(
            event_type="NODE_START",
            node="verifier",
            phase="verify",
            message=start_note["message"],
            thought=start_note["thought"],
            action=start_note["action"],
            observation=start_note["observation"],
            metadata={"role": "verifier_agent", "note_source": start_note.get("source", "fallback"), "note_model": start_note.get("note_model")},
        ).to_payload(),
        AgentEvent(
            event_type="GATE_APPLIED",
            node="verifier",
            phase="verify",
            message=gate_note["message"],
            decision_code="HITL_REQUIRED" if needs_hitl else "READY",
            thought=gate_note["thought"],
            action=gate_note["action"],
            observation=gate_note["observation"],
            metadata={"role": "verifier_agent", "note_source": gate_note.get("source", "fallback"), "note_model": gate_note.get("note_model"), **verification},
        ).to_payload(),
    ]
    if hitl_request:
        hitl_note = await generate_working_note(
            node="verify",
            role="verifier_agent",
            context={
                "case_id": state["case_id"],
                "hitl_request": hitl_request,
            },
            fallback_message="사람 검토가 필요한 케이스로 분류되었습니다.",
            fallback_thought="현재 자동 확정은 위험해 사람 검토를 우선해야 합니다.",
            fallback_action="재무 검토자에게 소명 요청을 생성합니다.",
            fallback_observation="HITL 요청이 생성되었습니다.",
        )
        events.append(
            AgentEvent(
                event_type="HITL_REQUESTED",
                node="verifier",
                phase="verify",
                message=hitl_note["message"],
                decision_code="HITL_REQUIRED",
                thought=hitl_note["thought"],
                action=hitl_note["action"],
                observation=hitl_note["observation"],
                metadata={**hitl_request, "note_source": hitl_note.get("source", "fallback"), "note_model": hitl_note.get("note_model")},
            ).to_payload()
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
    start_note = await generate_working_note(
        node="report",
        role="reporter_agent",
        context={
            "case_id": state["case_id"],
            "score_breakdown": score,
            "policy_refs": _top_policy_refs(state.get("tool_results", []), limit=2),
        },
        fallback_message="사용자에게 제시할 보고 문안을 구성합니다.",
        fallback_thought="핵심 사실과 규정 근거를 짧고 명확한 보고 문장으로 바꿔야 합니다.",
        fallback_action="보고용 설명 문장을 구성합니다.",
        fallback_observation="보고 문안 생성을 시작했습니다.",
    )
    end_note = await generate_working_note(
        node="report",
        role="reporter_agent",
        context={
            "case_id": state["case_id"],
            "summary": summary,
        },
        fallback_message=summary,
        fallback_thought="사용자에게 전달할 보고 요약이 준비되었습니다.",
        fallback_action="최종 요약 문장을 정리했습니다.",
        fallback_observation="사용자용 요약 문안이 준비되었습니다.",
    )
    return {
        "pending_events": [
            AgentEvent(
                event_type="NODE_START",
                node="reporter",
                phase="report",
                message=start_note["message"],
                thought=start_note["thought"],
                action=start_note["action"],
                observation=start_note["observation"],
                metadata={"role": "reporter_agent", "note_source": start_note.get("source", "fallback"), "note_model": start_note.get("note_model")},
            ).to_payload(),
            AgentEvent(
                event_type="NODE_END",
                node="reporter",
                phase="report",
                message=end_note["message"],
                thought=end_note["thought"],
                action=end_note["action"],
                observation=end_note["observation"],
                metadata={"role": "reporter_agent", "summary": summary, "note_source": end_note.get("source", "fallback"), "note_model": end_note.get("note_model")},
            ).to_payload(),
        ],
    }


async def finalizer_node(state: AgentState) -> AgentState:
    score = state["score_breakdown"]
    hitl_request = state.get("hitl_request")
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
    # Phase 0: Screening (case type classification from raw signals)
    workflow.add_node("screener", screener_node)
    # Phase 1-7: Deep analysis
    workflow.add_node("intake", intake_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("execute", execute_node)
    workflow.add_node("critic", critic_node)
    workflow.add_node("verify", verify_node)
    workflow.add_node("reporter", reporter_node)
    workflow.add_node("finalizer", finalizer_node)
    workflow.add_edge(START, "screener")
    workflow.add_edge("screener", "intake")
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
    run_id: str | None = None,
):
    from utils.config import get_langfuse_handler

    graph = build_agent_graph()
    initial_state: AgentState = {
        "case_id": case_id,
        "body_evidence": body_evidence,
        "intended_risk_type": intended_risk_type,
    }
    config: dict[str, Any] = {}
    if run_id:
        handler = get_langfuse_handler(session_id=run_id)
        if handler:
            config = {
                "callbacks": [handler],
                "configurable": {"thread_id": run_id},
                "tags": ["matertask", "analysis", f"case:{case_id}"],
            }
    async for chunk in graph.astream(initial_state, stream_mode="updates", config=config):
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
