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
    risk = "MEDIUM" if mcc else "UNKNOWN"
    if str(mcc) in {"5813", "7992"}:
        risk = "HIGH"
    return {
        "skill": "merchant_risk_probe",
        "ok": True,
        "facts": {
            "mccCode": mcc,
            "merchantName": merchant,
            "merchantRisk": risk,
        },
        "summary": "거래처/MCC 기반 위험도를 평가했습니다.",
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


async def policy_rulebook_probe(context: dict[str, Any]) -> dict[str, Any]:
    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as db:
        refs = search_policy_chunks(db, context["body_evidence"], limit=5)
    return {
        "skill": "policy_rulebook_probe",
        "ok": True,
        "facts": {
            "policy_refs": refs,
            "ref_count": len(refs),
        },
        "summary": f"규정집에서 관련 조항 {len(refs)}건을 조회했습니다.",
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


# Transitional: Phase A에서 LangChain tool schema로 승격. Phase C까지 registry direct dispatch 사용 후 ToolNode로 전환. (docs/phase0-prep.md)
SKILL_REGISTRY: dict[str, AgentSkill] = {
    "holiday_compliance_probe": AgentSkill(
        name="holiday_compliance_probe",
        description="휴일/휴무/연차 사용 정황을 검증한다.",
        handler=holiday_compliance_probe,
    ),
    "budget_risk_probe": AgentSkill(
        name="budget_risk_probe",
        description="예산 초과 여부와 금액 신호를 검증한다.",
        handler=budget_risk_probe,
    ),
    "merchant_risk_probe": AgentSkill(
        name="merchant_risk_probe",
        description="거래처와 MCC 기반 위험도를 검증한다.",
        handler=merchant_risk_probe,
    ),
    "document_evidence_probe": AgentSkill(
        name="document_evidence_probe",
        description="전표 라인아이템과 문서 증거를 수집한다.",
        handler=document_evidence_probe,
    ),
    "policy_rulebook_probe": AgentSkill(
        name="policy_rulebook_probe",
        description="내부 규정집에서 관련 조항을 조회한다.",
        handler=policy_rulebook_probe,
    ),
    "legacy_aura_deep_audit": AgentSkill(
        name="legacy_aura_deep_audit",
        description="기존 Aura 심층 분석을 전문 감사 툴로 호출한다.",
        handler=legacy_aura_deep_audit,
    ),
}


def _make_langchain_tool(skill_name: str) -> StructuredTool:
    """Phase A: LangChain StructuredTool로 스킬을 감싼다. 입력/출력 스키마 부여."""
    skill = SKILL_REGISTRY[skill_name]

    async def _invoke(inp: SkillContextInput) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "case_id": inp.case_id,
            "body_evidence": inp.body_evidence,
            "intended_risk_type": inp.intended_risk_type,
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
