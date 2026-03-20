from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from agent.event_schema import AgentEvent
from agent.output_models import PlanStep, PlannerOutput
from utils.config import settings
from utils.llm_azure import completion_kwargs_for_azure

logger = logging.getLogger(__name__)

# 위험 신호 키 — screener._extract_signals와 동일 집합
_DEEP_RISK_SIGNAL_KEYS = (
    "is_holiday",
    "is_leave",
    "is_night",
    "budget_exceeded",
    "mcc_high_risk",
    "mcc_leisure",
)


def _should_promote_to_deep(fast_result: dict[str, Any]) -> tuple[bool, str]:
    """Fast(hybrid) 결과를 보고 Deep lane 승격 여부를 결정한다.

    문서 B안 승격 기준:
      1. rule_case_type != llm_case_type
      2. llm_confidence < min_override_conf (기본 0.75)
      3. final_score ∈ [45, 65]  (경계 점수 구간)
      4. NORMAL_BASELINE + 위험 신호 ≥ 2

    LLM이 호출되지 않은 경우(rule-only / hybrid_fallback_rule)는 승격하지 않는다.
    Returns (should_promote: bool, reason: str).
    """
    mode = fast_result.get("screening_mode", "")
    source = fast_result.get("screening_source", "")

    # LLM이 실제로 호출된 hybrid 결과에만 승격 판단을 적용한다.
    if mode != "hybrid" or source == "hybrid_fallback_rule":
        return False, "fast_rule_only_no_promote"

    rule_case = fast_result.get("case_type", "")
    llm_case = fast_result.get("llm_case_type")
    llm_confidence = fast_result.get("llm_confidence")
    score = int(fast_result.get("score", 0))
    signals = fast_result.get("signals") or {}
    min_conf = float(getattr(settings, "screening_llm_override_min_confidence", 0.75))

    # 조건 1: LLM-규칙 불일치
    if llm_case and rule_case != llm_case:
        return True, f"rule_llm_mismatch(rule={rule_case},llm={llm_case})"

    # 조건 2: LLM 저신뢰도
    if llm_confidence is not None and llm_confidence < min_conf:
        return True, f"llm_low_confidence({llm_confidence:.2f}<{min_conf})"

    # 조건 3: 경계 점수 구간
    if 45 <= score <= 65:
        return True, f"boundary_score({score})"

    # 조건 4: NORMAL_BASELINE + 위험 신호 과다
    if rule_case == "NORMAL_BASELINE":
        risk_count = sum(1 for k in _DEEP_RISK_SIGNAL_KEYS if signals.get(k))
        if risk_count >= 2:
            return True, f"normal_baseline_with_risk_signals({risk_count})"

    return False, "fast_sufficient"


async def start_router_node_impl(
    state: dict[str, Any],
    *,
    is_valid_screening_case_type: Callable[[Any], bool],
    build_prescreened_result: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """
    사전 스크리닝된 전표(case_type 있음)면 screening_result를 주입하고 다음에 intake로 직행하고,
    아니면 빈 업데이트만 반환해 다음에 screener로 보낸다.
    """
    body = state.get("body_evidence") or {}
    pre_classified = body.get("case_type") or body.get("intended_risk_type")
    if not is_valid_screening_case_type(pre_classified):
        logger.info("[start_router] 사전 스크리닝 없음 → screener 노드로 이동")
        return {}
    logger.info("[start_router] 사전 스크리닝 감지(case_type=%s) → intake 직행", pre_classified)
    screening = build_prescreened_result(body)
    updated_body = {**body, "case_type": screening["case_type"], "intended_risk_type": screening["case_type"]}
    return {
        "screening_result": screening,
        "intended_risk_type": screening["case_type"],
        "body_evidence": updated_body,
    }


async def screener_node_impl(
    state: dict[str, Any],
    *,
    is_valid_screening_case_type: Callable[[Any], bool],
    run_screening: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """
    Phase 0 — Screening (Dual-Lane Agentic Screening).

    Fast lane: 현재 hybrid(규칙+LLM 보정) 스크리닝을 asyncio.to_thread로 실행.
    Deep lane: 승격 조건 충족 시 LangGraph 4노드 서브그래프를 ainvoke로 실행.
               실패/타임아웃 시 Fast 결과로 즉시 fallback.

    승격 조건 (문서 B안):
      1. rule_case_type != llm_case_type
      2. llm_confidence < min_override_conf (0.75)
      3. final_score ∈ [45, 65]
      4. NORMAL_BASELINE + 위험 신호 ≥ 2
    """
    from agent.screening_subgraph import get_deep_screening_graph

    body = state["body_evidence"]

    # 이미 유효한 스크리닝 결과가 있으면 재스크리닝 없이 그대로 사용
    pre_classified = body.get("case_type") or body.get("intended_risk_type")
    if is_valid_screening_case_type(pre_classified) and str(pre_classified).upper() != "NORMAL_BASELINE":
        screening: dict[str, Any] = {
            "case_type": pre_classified,
            "severity": body.get("severity") or "MEDIUM",
            "score": body.get("screening_score") or 0,
            "reasons": ["기존 스크리닝 결과를 사용합니다."],
            "reason_text": f"기존 분류: {pre_classified}",
            "screening_mode": "prescreened",
            "screening_source": "prescreened",
        }
        lane_used = "prescreened"
    else:
        # ── Fast lane ──────────────────────────────────────────────────────
        fast_result: dict[str, Any] = await asyncio.to_thread(run_screening, body)
        screening = fast_result
        lane_used = "fast"

        # ── Deep 승격 판단 ──────────────────────────────────────────────────
        should_promote, promotion_reason = _should_promote_to_deep(fast_result)
        logger.info(
            "screener_node: fast_case_type=%s score=%s llm_confidence=%s "
            "promote=%s promotion_reason=%s",
            fast_result.get("case_type"),
            fast_result.get("score"),
            fast_result.get("llm_confidence"),
            should_promote,
            promotion_reason,
        )

        if should_promote:
            # ── Deep lane ───────────────────────────────────────────────────
            # 전체 타임아웃 = LLM 타임아웃 + 2 s (노드 오버헤드 여유)
            _llm_timeout = float(getattr(settings, "screening_llm_timeout_seconds", 8.0))
            _deep_timeout = _llm_timeout + 2.0
            try:
                deep_graph = get_deep_screening_graph()
                deep_input: dict[str, Any] = {
                    "body_evidence": body,
                    "fast_result": fast_result,
                    "promotion_reason": promotion_reason,
                    "signals": fast_result.get("signals") or {},
                    "alt_hypotheses": [],
                    "guardrail_decision": {},
                    "final_result": {},
                    "decision_path": [],
                }
                deep_output = await asyncio.wait_for(
                    deep_graph.ainvoke(deep_input),
                    timeout=_deep_timeout,
                )
                deep_final = deep_output.get("final_result") or {}
                if deep_final.get("case_type"):
                    screening = deep_final
                    lane_used = "deep"
                    logger.info(
                        "screener_node: deep lane completed: "
                        "final_case_type=%s severity=%s score=%s promotion_reason=%s",
                        screening.get("case_type"),
                        screening.get("severity"),
                        screening.get("score"),
                        promotion_reason,
                    )
                else:
                    logger.warning(
                        "screener_node: deep lane returned empty final_result → fast fallback"
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    "screener_node: deep lane timeout (%.1fs) → fast fallback "
                    "promotion_reason=%s fast_case_type=%s",
                    _deep_timeout, promotion_reason, fast_result.get("case_type"),
                )
            except Exception as exc:
                logger.warning(
                    "screener_node: deep lane failed (%s) → fast fallback", exc
                )

    updated_body = {
        **body,
        "case_type": screening["case_type"],
        "intended_risk_type": screening["case_type"],
    }

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
    screening_meta = screening.get("screening_meta")  # Deep lane에서만 채워짐

    return {
        "body_evidence": updated_body,
        "intended_risk_type": screening["case_type"],
        "screening_result": screening,
        "pending_events": [
            AgentEvent(
                event_type="NODE_START",
                node="screener",
                phase="screen",
                message="전표 데이터를 분석해 케이스(위반 유형)를 분류합니다.",
                thought="원시 전표 데이터에서 위반 유형을 식별합니다.",
                action="전표 데이터 추출 및 점수 산정",
                observation="근태 상태, 업종 코드, 전표 기준일, 입력 시각 등 핵심 필드를 분석합니다.",
                metadata={"role": "screener", "lane": lane_used},
            ).to_payload(),
            AgentEvent(
                event_type="SCREENING_RESULT",
                node="screener",
                phase="screen",
                message=f"스크리닝 완료 [{lane_used.upper()}]: [{label}] — 중요도 {severity} / 점수 {score}",
                thought=f"전표 데이터를 바탕으로 '{screening['case_type']}' 유형으로 분류되었습니다.",
                action="케이스 유형 분류 완료",
                observation=reason_text,
                metadata={
                    "case_type": screening["case_type"],
                    "severity": severity,
                    "score": score,
                    "reasons": screening.get("reasons", []),
                    "reasonText": reason_text,
                    "reason_text": reason_text,
                    "lane": lane_used,
                    "screening_mode": screening.get("screening_mode"),
                    "screening_source": screening.get("screening_source"),
                    "llm_case_type": screening.get("llm_case_type"),
                    "llm_confidence": screening.get("llm_confidence"),
                    "hybrid_case_align_reason": screening.get("hybrid_case_align_reason"),
                    "screening_meta": screening_meta,
                },
            ).to_payload(),
            AgentEvent(
                event_type="NODE_END",
                node="screener",
                phase="screen",
                message="스크리닝 단계가 완료되었습니다.",
                metadata={
                    "case_type": screening["case_type"],
                    "severity": severity,
                    "score": score,
                    "lane": lane_used,
                },
            ).to_payload(),
        ],
    }


async def intake_node_impl(
    state: dict[str, Any],
    *,
    derive_flags: Callable[[dict[str, Any]], dict[str, Any]],
    stream_reasoning_events_with_llm: Callable[[str, str], Awaitable[tuple[str, list[dict[str, Any]], str]]],
) -> dict[str, Any]:
    flags = derive_flags(state["body_evidence"])
    pending: list[dict[str, Any]] = []

    screening_result = state.get("screening_result")
    if screening_result:
        label_map = {
            "HOLIDAY_USAGE": "휴일/휴무 중 사용 의심",
            "LIMIT_EXCEED": "한도 초과 의심",
            "PRIVATE_USE_RISK": "사적 사용 위험",
            "UNUSUAL_PATTERN": "비정상 패턴",
            "NORMAL_BASELINE": "정상 범위",
        }
        label = label_map.get(screening_result.get("case_type", ""), screening_result.get("case_type", ""))
        severity = screening_result.get("severity", "MEDIUM")
        score = screening_result.get("score", 0)
        reason_text = screening_result.get("reason_text", "")
        pending.append(
            AgentEvent(
                event_type="SCREENING_RESULT",
                node="screener",
                phase="screen",
                message=f"스크리닝 완료(사전 적용): [{label}] — 중요도 {severity} / 점수 {score}",
                thought="테스트 데이터 생성 시점에 적용된 스크리닝 결과를 사용합니다.",
                action="사전 스크리닝 결과 반영",
                observation=reason_text,
                metadata={
                    "case_type": screening_result.get("case_type"),
                    "severity": severity,
                    "score": score,
                    "reasons": screening_result.get("reasons", []),
                    "reasonText": reason_text,
                },
            ).to_payload(),
        )

    reasoning_parts = ["전표 입력값에서 핵심 위험 지표를 추출했다."]
    if flags.get("isHoliday"):
        reasoning_parts.append("휴일 사용 정황이 감지되었다.")
    if flags.get("budgetExceeded"):
        reasoning_parts.append("예산 초과 플래그가 있다.")
    if flags.get("mccCode"):
        reasoning_parts.append("MCC 업종 코드가 있어 업종 위험 검증이 필요하다.")
    reasoning_text = " ".join(reasoning_parts)
    last_node_summary = f"intake 완료: {reasoning_text[:60]}…" if len(reasoning_text) > 60 else f"intake 완료: {reasoning_text}"
    pending.append(
        AgentEvent(event_type="NODE_START", node="intake", phase="analyze", message="입력 데이터를 정규화합니다.", metadata=dict(flags)).to_payload(),
    )
    intake_context = {
        "flags": flags,
        "last_node_summary": state.get("last_node_summary", "없음"),
    }
    reasoning_text, reasoning_events, note_source = await stream_reasoning_events_with_llm("intake", reasoning_text, context=intake_context)
    pending.extend(reasoning_events)
    pending.append(
        AgentEvent(
            event_type="NODE_END",
            node="intake",
            phase="analyze",
            message="입력 정규화가 완료되었습니다.",
            metadata={"reasoning": reasoning_text, "note_source": note_source, **flags},
        ).to_payload(),
    )
    # Sprint 2: 증빙 이미지 분석 결과가 있으면 visual_audit_results로 전달 (API 재호출 없음)
    visual_audit_results: list[dict[str, Any]] = list(state["body_evidence"].get("extracted_entities") or [])
    if visual_audit_results:
        logger.info("intake_node: visual_audit_results 로드 (%d 엔티티)", len(visual_audit_results))

    return {
        "flags": flags,
        "visual_audit_results": visual_audit_results,
        "last_node_summary": last_node_summary,
        "pending_events": pending,
    }


def available_planner_tools_impl(
    *,
    get_tools_by_name: Callable[[], dict[str, Any]],
) -> list[dict[str, str]]:
    """LLM Planner에 제공할 도구 목록. 레지스트리와 동기화."""
    tools_by_name = get_tools_by_name()
    templates = [
        ("holiday_compliance_probe", "isHoliday=True 또는 hrStatus가 LEAVE/OFF/VACATION일 때"),
        ("budget_risk_probe", "budgetExceeded=True일 때"),
        ("merchant_risk_probe", "mccCode가 있을 때"),
        ("document_evidence_probe", "항상 실행 (전표 증거 수집)"),
        ("policy_rulebook_probe", "항상 실행 (규정 조항 조회)"),
        ("legacy_aura_deep_audit", "enable_legacy_aura_specialist=True이고 증거가 부족할 때"),
    ]
    return [{"name": name, "when": when} for name, when in templates if name in tools_by_name]


async def invoke_llm_planner_impl(
    flags: dict[str, Any],
    screening: dict[str, Any],
    replan_context: dict[str, Any] | None,
    available_tools: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """LLM으로 계획 JSON 생성. 실패 시 빈 리스트 반환."""
    system_prompt = (
        "당신은 기업 경비 감사 에이전트의 Planner다.\n"
        "아래 케이스 정보를 분석하여 최적의 도구 실행 순서를 결정하라.\n"
        "규칙:\n"
        "1. 공통 사항: document_evidence_probe(전표 증거 수집)와 policy_rulebook_probe(규정 조항 조회)는 반드시 계획에 포함하라.\n"
        "   단, 증빙 의무 문맥은 비정상 케이스에 우선 적용하고 NORMAL_BASELINE에는 자동 강제하지 마라.\n"
        "2. 케이스 유형(휴일/한도/업종 등)에 따라 holiday_compliance_probe, budget_risk_probe, merchant_risk_probe 등을 추가로 포함하라.\n"
        "3. 앞 도구 결과가 뒷 도구에 영향을 준다면 순서를 고려하라.\n"
        "4. 반드시 JSON 배열로만 응답하라. 각 항목: {\"tool\": string, \"reason\": string}\n"
        "5. 배열 외 텍스트, 마크다운 금지.\n"
    )
    user_prompt = (
        f"케이스 유형: {screening.get('case_type', 'UNKNOWN')}\n"
        f"심각도: {screening.get('severity', 'MEDIUM')}\n"
        f"플래그: isHoliday={flags.get('isHoliday')}, "
        f"hrStatus={flags.get('hrStatus')}, "
        f"budgetExceeded={flags.get('budgetExceeded')}, "
        f"mccCode={flags.get('mccCode')}, "
        f"isNight={flags.get('isNight')}, "
        f"amount={flags.get('amount')}\n"
    )
    if replan_context:
        _diag = str(replan_context.get("diagnostic_log") or "").strip()
        _rule_s = replan_context.get("rule_score")
        _llm_s = replan_context.get("llm_score")
        _fidel = replan_context.get("fidelity")
        _score_hint = ""
        if _rule_s is not None and _llm_s is not None:
            _score_hint = f"규칙 점수={_rule_s} / LLM 점수={_llm_s}"
            if _fidel is not None:
                _score_hint += f" / fidelity={_fidel}"
            _score_hint += "\n"
        user_prompt += (
            "\n[재계획 모드]\n"
            f"이전 실행 도구: {replan_context.get('previous_tool_results', [])}\n"
            f"Critic 피드백: {replan_context.get('critic_feedback', '')}\n"
            + (f"LLM 진단 로그: {_diag}\n" if _diag else "")
            + (_score_hint if _score_hint else "")
            + f"누락 필드: {replan_context.get('missing_fields', [])}\n"
            "위 피드백과 진단 로그를 참고해 이전과 다른 도구 또는 순서를 선택하라. "
            "이미 실행된 도구는 꼭 필요한 경우에만 재포함하라."
        )
    user_prompt += f"\n\n사용 가능한 도구:\n{json.dumps(available_tools, ensure_ascii=False)}"

    valid_names = {t["name"] for t in available_tools}
    if not getattr(settings, "openai_api_key", None):
        return []
    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI

        base_url = (getattr(settings, "openai_base_url") or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client = AsyncAzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_ep,
                api_version=getattr(settings, "openai_api_version", "2024-02-15-preview"),
            )
        else:
            kw: dict[str, Any] = {"api_key": settings.openai_api_key}
            if base_url:
                kw["base_url"] = base_url
            client = AsyncOpenAI(**kw)

        tok_kw = {"max_completion_tokens": 1200} if is_azure else {"max_tokens": 1200}
        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                **tok_kw,
            ),
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        raw_plan = parsed if isinstance(parsed, list) else (parsed.get("plan") or [])
        plan = [
            {"tool": step["tool"], "reason": step.get("reason", ""), "owner": "llm_planner"}
            for step in raw_plan
            if isinstance(step, dict) and step.get("tool") in valid_names
        ]
        return plan
    except Exception:
        return []


async def planner_node_impl(
    state: dict[str, Any],
    *,
    plan_from_flags: Callable[[dict[str, Any]], list[dict[str, Any]]],
    available_planner_tools: Callable[[], list[dict[str, str]]],
    invoke_llm_planner: Callable[[dict[str, Any], dict[str, Any], dict[str, Any] | None, list[dict[str, str]]], Awaitable[list[dict[str, Any]]]],
    stream_reasoning_events_with_llm: Callable[[str, str], Awaitable[tuple[str, list[dict[str, Any]], str]]],
) -> dict[str, Any]:
    replan_context = state.get("replan_context")
    flags = state["flags"]
    available_tools = available_planner_tools()
    valid_tool_names = {t["name"] for t in available_tools}

    plan: list[dict[str, Any]] = []
    plan_source = "rule"
    if getattr(settings, "enable_llm_planner", True) and valid_tool_names:
        plan = await invoke_llm_planner(flags, state.get("screening_result") or {}, replan_context, available_tools)
        if plan:
            plan_source = "llm"
            logger.info(
                "[planner] LLM 계획 수립 | %d개 도구: %s",
                len(plan), " → ".join(s.get("tool", "") for s in plan),
            )
        else:
            logger.info("[planner] LLM 계획 미사용 → 규칙 기반 탐침 계획 적용")

    if not plan:
        base_plan = plan_from_flags(flags)
        if replan_context:
            already_run = set(replan_context.get("previous_tool_results") or [])
            always_rerun = {"policy_rulebook_probe", "document_evidence_probe"}
            plan = [step for step in base_plan if step["tool"] not in already_run or step["tool"] in always_rerun]
            if not plan:
                plan = base_plan
        else:
            plan = base_plan
        if getattr(settings, "enable_llm_planner", True):
            plan_source = "fallback_rule"

    plan_tools = {step.get("tool") for step in plan}
    if "document_evidence_probe" not in plan_tools:
        plan.append({"tool": "document_evidence_probe", "reason": "공통: 전표 증빙(라인/항목) 수집", "owner": "common"})
    if "policy_rulebook_probe" not in plan_tools:
        plan.append({"tool": "policy_rulebook_probe", "reason": "공통: 규정 조항 조회", "owner": "common"})

    def _build_planner_reasoning() -> str:
        lines: list[str] = []
        if plan_source == "llm":
            lines.append("LLM이 현재 신호와 재계획 문맥을 바탕으로 도구 실행 순서를 구성했습니다.")
        elif plan_source == "fallback_rule":
            lines.append("LLM 계획을 사용할 수 없어 규칙 기반 기본 경로로 계획을 구성했습니다.")
        else:
            lines.append("규칙 기반 기본 경로로 계획을 구성했습니다.")
        for idx, step in enumerate(plan, start=1):
            tool_name = step.get("tool", "unknown_tool")
            reason = str(step.get("reason") or "핵심 위험 신호 확인을 위해 포함")
            lines.append(f"{idx}) {tool_name}: {reason}")
        if replan_context:
            lines.append("비판 단계 피드백과 이전 실행 결과를 반영해 재계획했습니다.")
        return " ".join(lines)

    steps = [
        PlanStep(
            tool_name=step["tool"],
            purpose=step.get("reason", ""),
            required=True,
            skip_condition=None,
            owner=step.get("owner"),
        )
        for step in plan
    ]
    tool_sequence = [s["tool"] for s in plan]
    rationale = (
        "LLM이 도구 실행 순서를 결정했다."
        if plan_source == "llm"
        else "위험 신호 기반 규칙으로 도구 실행 순서를 결정했다."
    )
    reasoning_text = _build_planner_reasoning()
    planner_output = PlannerOutput(
        objective="위험 유형별 조사 순서에 따라 증거를 수집하고 규정 근거를 확보한다.",
        steps=steps,
        stop_after_sufficient_evidence=True,
        tool_budget=len(plan),
        rationale=rationale,
        reasoning=reasoning_text,
    )
    last_node_summary = f"planner 완료: {reasoning_text[:80]}…" if len(reasoning_text) > 80 else f"planner 완료: {reasoning_text}"
    if replan_context:
        logger.info(
            "[planner] 재계획 모드 (loop=%s) | critic 피드백: %s",
            replan_context.get("loop_count", "?"),
            str(replan_context.get("critic_feedback", ""))[:120],
        )
    logger.info(
        "[planner] ✔ 계획 확정 (source=%s) | %s",
        plan_source,
        " → ".join(tool_sequence),
    )
    pending: list[dict[str, Any]] = [
        AgentEvent(
            event_type="NODE_START",
            node="planner",
            phase="plan",
            message="조사 계획을 수립합니다.",
            metadata={"plan": plan, "plan_source": plan_source},
        ).to_payload(),
    ]
    planner_context = {
        "selected_tools": tool_sequence,
        "flags": state.get("flags") or {},
        "plan_source": plan_source,
        "last_node_summary": state.get("last_node_summary", "없음"),
    }
    reasoning_text, reasoning_events, note_source = await stream_reasoning_events_with_llm("planner", reasoning_text, context=planner_context)
    pending.extend(reasoning_events)
    pending.append(
        AgentEvent(
            event_type="PLAN_READY",
            node="planner",
            phase="plan",
            message=reasoning_text or "조사 계획이 확정되었습니다.",
            metadata={"plan": plan, "reasoning": reasoning_text, "note_source": note_source, "plan_source": plan_source},
        ).to_payload(),
    )
    pending.append(
        AgentEvent(
            event_type="NODE_END",
            node="planner",
            phase="plan",
            message="조사 계획 수립이 완료되었습니다.",
            metadata={"plan_size": len(plan)},
        ).to_payload()
    )
    return {
        "plan": plan,
        "planner_output": planner_output.model_dump(),
        "replan_context": None,
        "last_node_summary": last_node_summary,
        "pending_events": pending,
    }
