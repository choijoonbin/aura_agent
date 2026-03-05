from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from langchain_core.tools import StructuredTool
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent.aura_bridge import run_legacy_aura_analysis
from agent.tool_schemas import SkillContextInput
from services.policy_service import search_policy_chunks
from utils.config import settings


SkillFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class AgentSkill:
    name: str
    description: str
    handler: SkillFn
    display_summary_ko: str | None = None  # 발표용 한글 요약 (입력/출력 1~2문장). 없으면 자동 생성 fallback


async def holiday_compliance_probe(context: dict[str, Any]) -> dict[str, Any]:
    body = context["body_evidence"]
    occurred_at = body.get("occurredAt")
    is_holiday = bool(body.get("isHoliday"))
    hr_status = body.get("hrStatus") or body.get("hrStatusRaw")
    return {
        "skill": "holiday_compliance_probe",
        "ok": True,
        "facts": {
            "occurredAt": occurred_at,
            "isHoliday": is_holiday,
            "hrStatus": hr_status,
            "holidayRisk": bool(is_holiday or hr_status in {"LEAVE", "OFF", "VACATION"}),
        },
        "summary": "휴일/휴무/연차와 결제 시점을 교차 검증했습니다.",
    }


async def budget_risk_probe(context: dict[str, Any]) -> dict[str, Any]:
    body = context["body_evidence"]
    amount = body.get("amount") or 0
    exceeded = bool(body.get("budgetExceeded"))
    return {
        "skill": "budget_risk_probe",
        "ok": True,
        "facts": {
            "amount": amount,
            "budgetExceeded": exceeded,
        },
        "summary": "예산 초과 플래그와 금액을 확인했습니다.",
    }


async def merchant_risk_probe(context: dict[str, Any]) -> dict[str, Any]:
    body = context["body_evidence"]
    mcc = body.get("mccCode")
    merchant = body.get("merchantName")

    high_mcc = {"5813", "7992", "5912", "7997", "5999"}
    medium_mcc = {"5812", "5814", "7011", "4722"}
    mcc_str = str(mcc or "")
    if mcc_str in high_mcc:
        base_risk = "HIGH"
    elif mcc_str in medium_mcc or mcc_str:
        base_risk = "MEDIUM"
    else:
        base_risk = "UNKNOWN"

    prior = context.get("prior_tool_results") or []
    holiday_facts = next(
        ((result.get("facts") or {}) for result in prior if result.get("skill") == "holiday_compliance_probe"),
        {},
    )
    holiday_risk = bool(holiday_facts.get("holidayRisk"))
    compound_flags: list[str] = []

    if holiday_risk and base_risk == "HIGH":
        risk = "CRITICAL"
        compound_flags.append("휴일+고위험업종 복합")
    elif holiday_risk and base_risk == "MEDIUM":
        risk = "HIGH"
        compound_flags.append("휴일+중간위험업종 복합")
    else:
        risk = base_risk
    return {
        "skill": "merchant_risk_probe",
        "ok": True,
        "facts": {
            "mccCode": mcc,
            "merchantName": merchant,
            "merchantRisk": risk,
            "compoundRiskFlags": compound_flags,
            "holidayRiskConsidered": holiday_risk,
        },
        "summary": (
            "거래처/가맹점 업종 코드(MCC) 기반 위험도를 평가했습니다."
            + (f" 복합 위험 감지: {', '.join(compound_flags)}" if compound_flags else "")
        ),
    }


async def document_evidence_probe(context: dict[str, Any]) -> dict[str, Any]:
    doc = (context["body_evidence"].get("document") or {})
    items = doc.get("items") or []
    return {
        "skill": "document_evidence_probe",
        "ok": True,
        "facts": {
            "lineItemCount": len(items),
            "lineItems": items,
        },
        "summary": f"전표 라인아이템 {len(items)}건을 검토했습니다.",
    }


def _adoption_reason_for_ref(ref: dict[str, Any], body_evidence: dict[str, Any]) -> str:
    """규칙 + retrieval context 기반 채택 이유 한 줄 (발표용)."""
    case_type = str(body_evidence.get("case_type") or body_evidence.get("intended_risk_type") or "")
    article = ref.get("article") or ""
    parent_title = str(ref.get("parent_title") or "")[:40]
    if case_type and article:
        return f"{case_type} 조건과 직접 일치하는 조항({article})이어서 채택"
    if parent_title:
        return f"규정 '{parent_title}'과 관련되어 채택"
    return "규정 근거로 채택"


async def policy_rulebook_probe(context: dict[str, Any]) -> dict[str, Any]:
    prior = context.get("prior_tool_results") or []
    enriched_body = dict(context["body_evidence"])

    for result in prior:
        skill = result.get("skill", "")
        facts = result.get("facts") or {}
        if skill == "holiday_compliance_probe" and facts.get("holidayRisk"):
            enriched_body["_enriched_holidayRisk"] = True
            if facts.get("hrStatus"):
                enriched_body.setdefault("hrStatus", facts.get("hrStatus"))
        elif skill == "merchant_risk_probe":
            m_risk = str(facts.get("merchantRisk") or "").upper()
            if m_risk in {"CRITICAL", "HIGH"}:
                enriched_body["_enriched_merchantRisk"] = m_risk
                enriched_body["_extra_keywords"] = ["고위험", "업종", "강화승인"]

    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as db:
        # Phase F/8: top-20 후보 확보 후 상위 5건만 채택. candidates는 별도 payload 저장용
        candidates = search_policy_chunks(db, enriched_body, limit=20)
        refs = []
        for r in candidates[:5]:
            ref = dict(r)
            ref["adoption_reason"] = _adoption_reason_for_ref(ref, enriched_body)
            refs.append(ref)
        # 나머지 candidates도 adoption_reason 붙여서 저장 (후보 표시용)
        candidates_with_reason = []
        for r in candidates:
            ref = dict(r)
            ref.setdefault("adoption_reason", _adoption_reason_for_ref(ref, enriched_body))
            candidates_with_reason.append(ref)
    return {
        "skill": "policy_rulebook_probe",
        "ok": True,
        "facts": {
            "policy_refs": refs,
            "ref_count": len(refs),
            "retrieval_candidates": candidates_with_reason,
            "enriched_from_prior": [r.get("skill") for r in prior],
        },
        "summary": (
            f"규정집에서 관련 조항 {len(refs)}건을 조회했습니다."
            + (f" (prior {len(prior)}개 결과 반영)" if prior else "")
        ),
    }


async def legacy_aura_deep_audit(context: dict[str, Any]) -> dict[str, Any]:
    if not settings.enable_legacy_aura_specialist:
        return {
            "skill": "legacy_aura_deep_audit",
            "ok": True,
            "facts": {
                "disabled": True,
                "reason": "PoC 독립 실행 모드에서 legacy Aura specialist를 비활성화했습니다.",
            },
            "trace": [],
            "summary": "legacy Aura 심층 감사 호출이 비활성화되어 생략되었습니다.",
        }
    case_id = context["case_id"]
    body_evidence = context["body_evidence"]
    intended_risk_type = context.get("intended_risk_type")
    final_payload: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = []
    async for ev_type, payload in run_legacy_aura_analysis(
        case_id,
        body_evidence=body_evidence,
        intended_risk_type=intended_risk_type,
    ):
        trace.append({"event_type": ev_type, "payload": payload, "at": datetime.utcnow().isoformat()})
        if ev_type in {"completed", "failed"}:
            final_payload = payload
            break
    return {
        "skill": "legacy_aura_deep_audit",
        "ok": final_payload is not None,
        "facts": final_payload or {},
        "trace": trace,
        "summary": "기존 Aura 심층 감사 분석을 호출했습니다.",
    }


# Transitional: Phase A에서 LangChain tool schema로 승격. Phase C까지 registry direct dispatch 사용 후 ToolNode로 전환. (docs/work_info/phase0-prep.md)
SKILL_REGISTRY: dict[str, AgentSkill] = {
    "holiday_compliance_probe": AgentSkill(
        name="holiday_compliance_probe",
        description="휴일/휴무/연차 사용 정황을 검증한다.",
        handler=holiday_compliance_probe,
        display_summary_ko="입력: 전표 발생시각, 금액, 근태 상태. 출력: 휴일 여부, 판정 사유, 적용 규정 후보.",
    ),
    "budget_risk_probe": AgentSkill(
        name="budget_risk_probe",
        description="예산 초과 여부와 금액 지표를 검증한다.",
        handler=budget_risk_probe,
        display_summary_ko="입력: 전표 금액·예산 초과 플래그. 출력: 예산 초과 여부, 금액 지표.",
    ),
    "merchant_risk_probe": AgentSkill(
        name="merchant_risk_probe",
        description="거래처와 가맹점 업종 코드(MCC) 기반 위험도를 검증한다.",
        handler=merchant_risk_probe,
        display_summary_ko="입력: 가맹점 업종 코드(MCC), 거래처 정보. 출력: 업종 위험도, 판정 근거.",
    ),
    "document_evidence_probe": AgentSkill(
        name="document_evidence_probe",
        description="전표 라인아이템과 문서 증거를 수집한다.",
        handler=document_evidence_probe,
        display_summary_ko="입력: 전표·문서. 출력: 라인 수, 라인아이템 요약.",
    ),
    "policy_rulebook_probe": AgentSkill(
        name="policy_rulebook_probe",
        description="내부 규정집에서 관련 조항을 조회한다.",
        handler=policy_rulebook_probe,
        display_summary_ko="입력: 케이스·키워드. 출력: 규정 후보, 채택 조항, adoption_reason.",
    ),
    "legacy_aura_deep_audit": AgentSkill(
        name="legacy_aura_deep_audit",
        description="기존 Aura 심층 분석을 전문 감사 툴로 호출한다.",
        handler=legacy_aura_deep_audit,
        display_summary_ko="입력: 케이스·body_evidence. 출력: legacy 심층 분석 결과(조건부 호출).",
    ),
}


def _make_langchain_tool(skill_name: str) -> StructuredTool:
    """Phase A: LangChain StructuredTool로 스킬을 감싼다. 입력/출력 스키마 부여."""
    skill = SKILL_REGISTRY[skill_name]

    async def _invoke(**kwargs: Any) -> dict[str, Any]:
        inp = SkillContextInput.model_validate(kwargs)
        ctx: dict[str, Any] = {
            "case_id": inp.case_id,
            "body_evidence": inp.body_evidence,
            "intended_risk_type": inp.intended_risk_type,
            "prior_tool_results": inp.prior_tool_results,
        }
        return await skill.handler(ctx)

    return StructuredTool(
        name=skill.name,
        description=skill.description,
        args_schema=SkillContextInput,
        coroutine=_invoke,
    )


def get_langchain_tools() -> list[StructuredTool]:
    """Phase A: 등록된 모든 스킬을 LangChain tool 목록으로 반환. Phase C에서 ToolNode 바인딩용."""
    return [_make_langchain_tool(name) for name in SKILL_REGISTRY]
