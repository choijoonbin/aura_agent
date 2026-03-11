"""
Deep Lane Screening Subgraph (Agentic Screening — Phase 1).

Fast(hybrid) lane에서 아래 승격 조건 중 하나라도 충족하면 이 서브그래프가 호출된다.
  1. rule_case_type != llm_case_type  (LLM-규칙 불일치)
  2. llm_confidence < 0.75            (LLM 저신뢰도)
  3. final_score in [45, 65]          (경계 점수 구간)
  4. NORMAL_BASELINE + 위험 신호 ≥ 2  (과탐/누락 방어)

노드 구성 (경량 4노드 — 문서 C안):
  intake_normalize → hypothesis_generate → rule_guardrail → finalize_screening

출력에 screening_meta(optional JSON)를 포함해 Fast 결과와 병합된 최종 결과를 반환한다.
LLM/타임아웃 실패 시 Fast 결과를 그대로 반환(fallback).
"""
from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent.screener import (
    _ALLOWED_CASE_TYPES,
    _align_hybrid_case_type,
    _build_reason_text,
    _derive_severity,
    _extract_json_object,
    _normalize_case_type,
)
from utils.config import settings
from utils.llm_azure import completion_kwargs_for_azure

logger = logging.getLogger(__name__)

# 위험 신호 키 목록 — screener._extract_signals 와 동일 집합
_RISK_SIGNAL_KEYS = (
    "is_holiday",
    "is_leave",
    "is_night",
    "budget_exceeded",
    "mcc_high_risk",
    "mcc_leisure",
)


class DeepScreeningState(TypedDict):
    body_evidence: dict[str, Any]
    fast_result: dict[str, Any]
    promotion_reason: str
    # populated by nodes
    signals: dict[str, Any]
    alt_hypotheses: list[dict[str, Any]]
    guardrail_decision: dict[str, Any]
    final_result: dict[str, Any]
    decision_path: list[str]


# ---------------------------------------------------------------------------
# Node 1: intake_normalize
# ---------------------------------------------------------------------------

async def intake_normalize_node(state: DeepScreeningState) -> dict[str, Any]:
    """입력 신호 추출 및 승격 컨텍스트를 decision_path에 기록한다."""
    fast = state["fast_result"]
    signals = fast.get("signals") or {}
    promotion_reason = state.get("promotion_reason") or "unknown"
    risk_count = sum(1 for k in _RISK_SIGNAL_KEYS if signals.get(k))

    path: list[str] = list(state.get("decision_path") or [])
    entry = (
        f"intake_normalize: promotion_reason={promotion_reason}, "
        f"risk_signal_count={risk_count}, "
        f"fast_case_type={fast.get('case_type')}, "
        f"fast_score={fast.get('score')}, "
        f"fast_llm_confidence={fast.get('llm_confidence')}"
    )
    path.append(entry)
    logger.info("deep_screening intake_normalize: %s", entry)

    return {"signals": signals, "decision_path": path}


# ---------------------------------------------------------------------------
# Node 2: hypothesis_generate
# ---------------------------------------------------------------------------

async def hypothesis_generate_node(state: DeepScreeningState) -> dict[str, Any]:
    """LLM에게 Top-2 가설(case_type + confidence + reason)을 생성하게 한다."""
    fast = state["fast_result"]
    signals = state.get("signals") or {}
    body = state["body_evidence"]
    path: list[str] = list(state.get("decision_path") or [])

    if not getattr(settings, "openai_api_key", None):
        path.append("hypothesis_generate: skipped (no api key) → fallback to fast")
        logger.info("deep_screening hypothesis_generate: no api key, skipped")
        return {"alt_hypotheses": [], "decision_path": path}

    system_prompt = (
        "당신은 기업 경비 전표 사전 스크리닝 분류기다.\n"
        "아래 입력을 분석해 가능성 높은 케이스 유형 Top-2를 신뢰도(confidence) 내림차순으로 반환하라.\n"
        "허용 case_type: HOLIDAY_USAGE, LIMIT_EXCEED, PRIVATE_USE_RISK, UNUSUAL_PATTERN, NORMAL_BASELINE.\n"
        "반드시 JSON 객체만 반환하라. 입력에 없는 사실을 만들지 마라.\n"
        '출력 형식: {"hypotheses": [{"case_type":"...","confidence":0.0~1.0,"reason":"한국어 1문장"}, ...]}'
    )
    payload = {
        "voucher": {
            "occurredAt": body.get("occurredAt"),
            "amount": body.get("amount"),
            "hrStatus": body.get("hrStatus"),
            "hrStatusRaw": body.get("hrStatusRaw"),
            "mccCode": body.get("mccCode"),
            "budgetExceeded": body.get("budgetExceeded"),
            "isHoliday": body.get("isHoliday"),
        },
        "signals": signals,
        "fast_context": {
            "rule_case_type": fast.get("case_type"),
            "llm_case_type": fast.get("llm_case_type"),
            "llm_confidence": fast.get("llm_confidence"),
            "score": fast.get("score"),
            "hybrid_align_reason": fast.get("hybrid_case_align_reason"),
        },
        "instruction": (
            "Fast lane(규칙+LLM 보정) 결과가 제공됩니다. "
            "이를 참고해 가능성 있는 Top-2 케이스 유형을 신뢰도 내림차순으로 제시하라."
        ),
    }
    user_prompt = json.dumps(payload, ensure_ascii=False)

    hypotheses: list[dict[str, Any]] = []
    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI

        base_url = (getattr(settings, "openai_base_url", None) or "").strip()
        is_azure = ".openai.azure.com" in base_url
        timeout = float(getattr(settings, "screening_llm_timeout_seconds", 8.0))
        model = str(
            getattr(settings, "screening_llm_model", None)
            or getattr(settings, "reasoning_llm_model", "gpt-4o-mini")
        )

        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client: Any = AsyncAzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_ep,
                api_version=getattr(settings, "openai_api_version", "2024-12-01-preview"),
                timeout=timeout,
            )
        else:
            kw: dict[str, Any] = {"api_key": settings.openai_api_key, "timeout": timeout}
            if base_url:
                kw["base_url"] = base_url
            client = AsyncOpenAI(**kw)

        max_out = int(getattr(settings, "screening_llm_max_tokens", 400))
        tok_kw = {"max_completion_tokens": max_out} if is_azure else {"max_tokens": max_out}

        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                **tok_kw,
            ),
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = _extract_json_object(raw)
        raw_hypotheses = parsed.get("hypotheses") or []

        for h in raw_hypotheses[:2]:
            if not isinstance(h, dict):
                continue
            ct = _normalize_case_type(h.get("case_type"))
            try:
                conf = float(h.get("confidence", 0.0))
                conf = max(0.0, min(1.0, conf))
            except Exception:
                conf = 0.0
            reason = str(h.get("reason") or "").strip()
            hypotheses.append({"case_type": ct, "confidence": conf, "reason": reason})

        logger.info(
            "deep_screening hypothesis_generate: model=%s hypotheses=%s",
            model,
            [(h["case_type"], round(h["confidence"], 2)) for h in hypotheses],
        )
    except Exception as exc:
        logger.warning("deep_screening hypothesis_generate failed: %s", exc)

    summary = (
        f"top={hypotheses[0]['case_type']}({hypotheses[0]['confidence']:.2f})"
        if hypotheses
        else "none"
    )
    path.append(
        f"hypothesis_generate: produced={len(hypotheses)} {summary}"
    )
    return {"alt_hypotheses": hypotheses, "decision_path": path}


# ---------------------------------------------------------------------------
# Node 3: rule_guardrail
# ---------------------------------------------------------------------------

async def rule_guardrail_node(state: DeepScreeningState) -> dict[str, Any]:
    """결정론 가드레일을 적용해 최종 case_type을 결정한다."""
    fast = state["fast_result"]
    signals = state.get("signals") or {}
    hypotheses = state.get("alt_hypotheses") or []
    path: list[str] = list(state.get("decision_path") or [])

    if not hypotheses:
        path.append("rule_guardrail: no hypotheses → revert to fast result")
        logger.info("deep_screening rule_guardrail: no hypotheses, fast fallback")
        return {
            "guardrail_decision": {
                "case_type": fast["case_type"],
                "align_reason": "no_hypotheses_fast_fallback",
                "source": "fast_fallback",
            },
            "decision_path": path,
        }

    top = hypotheses[0]
    aligned_case_type, align_reason = _align_hybrid_case_type(
        llm_case_type=top["case_type"],
        deterministic_case_type=fast["case_type"],
        deterministic_score=int(fast.get("score", 0)),
        signals=signals,
        llm_confidence=top.get("confidence"),
    )

    path.append(
        f"rule_guardrail: top={top['case_type']}({top.get('confidence', 0):.2f}) "
        f"→ aligned={aligned_case_type} align_reason={align_reason}"
    )
    logger.info(
        "deep_screening rule_guardrail: top=%s(%.2f) → aligned=%s reason=%s",
        top["case_type"],
        top.get("confidence", 0.0),
        aligned_case_type,
        align_reason,
    )
    return {
        "guardrail_decision": {
            "case_type": aligned_case_type,
            "align_reason": align_reason,
            "source": "deep_guardrail",
            "top_hypothesis": top,
        },
        "decision_path": path,
    }


# ---------------------------------------------------------------------------
# Node 4: finalize_screening
# ---------------------------------------------------------------------------

async def finalize_screening_node(state: DeepScreeningState) -> dict[str, Any]:
    """Fast + Deep 결과를 병합하고 screening_meta(optional)를 첨부해 최종 결과를 반환한다."""
    fast = state["fast_result"]
    signals = state.get("signals") or {}
    guardrail = state.get("guardrail_decision") or {}
    hypotheses = state.get("alt_hypotheses") or []
    path: list[str] = list(state.get("decision_path") or [])

    final_case_type: str = guardrail.get("case_type") or fast["case_type"]
    align_reason: str = guardrail.get("align_reason", "deep_lane")
    source: str = guardrail.get("source", "deep_guardrail")

    # reason_text: Deep lane top 가설 이유 우선, 없으면 Fast 결과
    reason_text = ""
    if hypotheses and hypotheses[0]["case_type"] == final_case_type:
        reason_text = hypotheses[0].get("reason", "")
    if not reason_text:
        reason_text = fast.get("reason_text", "")

    # score/severity는 결정론 Fast 결과 유지 (재현성 보장)
    final_score: int = int(fast.get("score", 0))
    final_severity: str = _derive_severity(final_score)

    # uncertainty_reason: Top-2 신뢰도 차이가 0.15 미만이면 불확실성 사유 기록
    uncertainty_reason: str | None = None
    if len(hypotheses) >= 2:
        top_conf = hypotheses[0].get("confidence", 0.0)
        second_conf = hypotheses[1].get("confidence", 0.0)
        if abs(top_conf - second_conf) < 0.15:
            uncertainty_reason = (
                f"Top-2 가설 신뢰도 차이 미미: "
                f"{hypotheses[0]['case_type']}({top_conf:.2f}) vs "
                f"{hypotheses[1]['case_type']}({second_conf:.2f})"
            )

    path.append(
        f"finalize_screening: final_case_type={final_case_type} "
        f"severity={final_severity} score={final_score} "
        f"source={source} uncertainty={uncertainty_reason is not None}"
    )

    screening_meta: dict[str, Any] = {
        "lane": "deep",
        "promotion_reason": state.get("promotion_reason") or "unknown",
        "alt_hypotheses": [
            {
                "case_type": h["case_type"],
                "confidence": h["confidence"],
                "reason": h["reason"],
            }
            for h in hypotheses
        ],
        "decision_path": path,
        "align_reason": align_reason,
        "uncertainty_reason": uncertainty_reason,
        "fast_case_type": fast.get("case_type"),
        "fast_llm_case_type": fast.get("llm_case_type"),
        "fast_llm_confidence": fast.get("llm_confidence"),
        "fast_score": fast.get("score"),
    }

    final_result: dict[str, Any] = {
        **fast,
        "case_type": final_case_type,
        "severity": final_severity,
        "score": final_score,
        "reason_text": reason_text,
        "screening_mode": "deep",
        "screening_source": source,
        "hybrid_case_align_reason": align_reason,
        "screening_meta": screening_meta,
    }

    logger.info(
        "deep_screening finalize: final_case_type=%s severity=%s score=%s lane=deep uncertainty=%s",
        final_case_type,
        final_severity,
        final_score,
        uncertainty_reason is not None,
    )
    return {"final_result": final_result, "decision_path": path}


# ---------------------------------------------------------------------------
# Compiled subgraph (singleton, no checkpointer)
# ---------------------------------------------------------------------------

_DEEP_SCREENING_GRAPH: Any = None


def get_deep_screening_graph() -> Any:
    """Deep lane 스크리닝 서브그래프(싱글톤). checkpointer 없이 컴파일."""
    global _DEEP_SCREENING_GRAPH
    if _DEEP_SCREENING_GRAPH is not None:
        return _DEEP_SCREENING_GRAPH

    workflow: StateGraph = StateGraph(DeepScreeningState)
    workflow.add_node("intake_normalize", intake_normalize_node)
    workflow.add_node("hypothesis_generate", hypothesis_generate_node)
    workflow.add_node("rule_guardrail", rule_guardrail_node)
    workflow.add_node("finalize_screening", finalize_screening_node)

    workflow.add_edge(START, "intake_normalize")
    workflow.add_edge("intake_normalize", "hypothesis_generate")
    workflow.add_edge("hypothesis_generate", "rule_guardrail")
    workflow.add_edge("rule_guardrail", "finalize_screening")
    workflow.add_edge("finalize_screening", END)

    _DEEP_SCREENING_GRAPH = workflow.compile(checkpointer=None)
    logger.info("deep_screening subgraph compiled (4-node: intake_normalize → hypothesis_generate → rule_guardrail → finalize_screening)")
    return _DEEP_SCREENING_GRAPH
