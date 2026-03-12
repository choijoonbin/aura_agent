"""
에이전트 실행 도구(Tool) 등록 및 LangChain StructuredTool 노출.
모든 capability는 LangChain tool로 통일
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any, Awaitable, Callable

from langchain_core.tools import StructuredTool
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent.aura_bridge import run_legacy_aura_analysis
from agent.tool_schemas import ToolContextInput
from services.policy_ref_normalizer import normalize_policy_parent_title
from services.policy_service import search_policy_chunks
from utils.config import get_mcc_sets, settings


ToolFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
logger = logging.getLogger(__name__)


def _tool_key(r: dict[str, Any]) -> str:
    """도구 결과 봉투에서 도구 이름 추출 (tool/skill 하위 호환)."""
    return str(r.get("tool") or r.get("skill") or "")


@dataclass(slots=True)
class AgentTool:
    name: str
    description: str
    handler: ToolFn
    display_summary_ko: str | None = None  # 발표용 한글 요약


async def holiday_compliance_probe(context: dict[str, Any]) -> dict[str, Any]:
    body = context["body_evidence"]
    occurred_at = body.get("occurredAt")
    is_holiday = bool(body.get("isHoliday"))
    hr_status = body.get("hrStatus") or body.get("hrStatusRaw")
    return {
        "tool": "holiday_compliance_probe",
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
        "tool": "budget_risk_probe",
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
    mcc_sets = get_mcc_sets()

    mcc_str = str(mcc or "").strip()
    if mcc_str in mcc_sets["high_risk"]:
        base_risk = "HIGH"
    elif mcc_str in mcc_sets["leisure"] or mcc_str in mcc_sets["medium_risk"]:
        base_risk = "MEDIUM"
    else:
        # high/medium/leisure 어디에도 없으면 위험도 없음(정상 비교군 등)
        base_risk = "LOW"

    prior = context.get("prior_tool_results") or []
    holiday_facts = next(
        ((r.get("facts") or {}) for r in prior if _tool_key(r) == "holiday_compliance_probe"),
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
        "tool": "merchant_risk_probe",
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
        "tool": "document_evidence_probe",
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
    parent_title = normalize_policy_parent_title(article, ref.get("parent_title"))[:40]
    direct_match_case_types = {"HOLIDAY_USAGE", "LIMIT_EXCEED", "PRIVATE_USE_RISK", "UNUSUAL_PATTERN"}
    if case_type in direct_match_case_types and article:
        return f"{case_type} 조건과 직접 일치하는 조항({article})이어서 채택"
    if case_type == "NORMAL_BASELINE" and article:
        return f"정상 비교군 검증에 필요한 기본 조항({article})이어서 채택"
    if parent_title:
        return f"규정 '{parent_title}'과 관련되어 채택"
    return "규정 근거로 채택"


def _article_key(ref: dict[str, Any]) -> str:
    return "".join(str(ref.get("article") or ref.get("regulation_article") or "").split())


def _is_common_evidence_article(ref: dict[str, Any]) -> bool:
    article = _article_key(ref)
    parent_title = "".join(str(ref.get("parent_title") or "").split())
    # 제14조(공통 증빙 의무) 식별
    return "제14조" in article or "제14조" in parent_title


def _ref_log_label(ref: dict[str, Any]) -> str:
    article = str(ref.get("article") or ref.get("regulation_article") or "-").strip() or "-"
    title = normalize_policy_parent_title(article, ref.get("parent_title")) or "-"
    score = ref.get("retrieval_score")
    if isinstance(score, (int, float)):
        return f"{article}/{title}({float(score):.2f})"
    return f"{article}/{title}"


async def policy_rulebook_probe(context: dict[str, Any]) -> dict[str, Any]:
    prior = context.get("prior_tool_results") or []
    enriched_body = dict(context["body_evidence"])
    # 스크리너가 판단한 케이스 유형을 규정 검색에 반영(휴일식대 vs 차량유지비 등 도메인별 적절한 조항만 검색)
    intended = context.get("intended_risk_type") or enriched_body.get("case_type") or enriched_body.get("intended_risk_type")
    if intended:
        enriched_body["case_type"] = intended
        enriched_body["intended_risk_type"] = intended

    for result in prior:
        tkey = _tool_key(result)
        facts = result.get("facts") or {}
        if tkey == "holiday_compliance_probe" and facts.get("holidayRisk"):
            enriched_body["_enriched_holidayRisk"] = True
            if facts.get("hrStatus"):
                enriched_body.setdefault("hrStatus", facts.get("hrStatus"))
        elif tkey == "merchant_risk_probe":
            m_risk = str(facts.get("merchantRisk") or "").upper()
            if m_risk in {"CRITICAL", "HIGH"}:
                enriched_body["_enriched_merchantRisk"] = m_risk
                enriched_body["_extra_keywords"] = ["고위험", "업종", "강화승인"]

    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as db:
        candidates = search_policy_chunks(db, enriched_body, limit=20)
        case_type = str(enriched_body.get("case_type") or "").upper()
        common_in_candidates = any(_is_common_evidence_article(r) for r in candidates)
        logger.info(
            "[POLICY_REF_TRACE] start case_type=%s candidates=%s common_in_candidates=%s candidate_preview=%s",
            case_type or "-",
            len(candidates),
            common_in_candidates,
            [_ref_log_label(r) for r in candidates[:8]],
        )
        # 채택 인용은 조문(article) 단위로 하나만 노출: 동일 제23조가 ARTICLE+CLAUSE 등 여러 청크로 검색되면 중복 표시되므로, 조문별 최고점 청크 1개만 채택
        ranked_unique_refs: list[dict[str, Any]] = []
        seen_articles: set[str] = set()
        for r in candidates:
            ref = dict(r)
            article_key = _article_key(ref)
            if not article_key or article_key in seen_articles:
                continue
            seen_articles.add(article_key)
            ref["adoption_reason"] = _adoption_reason_for_ref(ref, enriched_body)
            ranked_unique_refs.append(ref)

        need_common_evidence_first = case_type not in {"", "NORMAL_BASELINE"}
        common_evidence_ref = next((ref for ref in ranked_unique_refs if _is_common_evidence_article(ref)), None)
        max_ref_count = 4 if (need_common_evidence_first and common_evidence_ref is not None) else 3
        logger.info(
            "[POLICY_REF_TRACE] dedup case_type=%s unique_refs=%s need_common_evidence_first=%s common_found=%s common_ref=%s max_ref_count=%s",
            case_type or "-",
            len(ranked_unique_refs),
            need_common_evidence_first,
            common_evidence_ref is not None,
            _ref_log_label(common_evidence_ref) if common_evidence_ref is not None else "-",
            max_ref_count,
        )
        if need_common_evidence_first and common_evidence_ref is None:
            logger.warning(
                "[POLICY_REF_TRACE] common evidence article(제14조) not found in ranked_unique_refs for case_type=%s; common_in_candidates=%s",
                case_type or "-",
                common_in_candidates,
            )

        refs: list[dict[str, Any]] = []
        picked_keys: set[str] = set()
        if common_evidence_ref is not None and need_common_evidence_first:
            refs.append(common_evidence_ref)
            picked_keys.add(_article_key(common_evidence_ref))
        for ref in ranked_unique_refs:
            if len(refs) >= max_ref_count:
                break
            key = _article_key(ref)
            if key in picked_keys:
                continue
            refs.append(ref)
            picked_keys.add(key)
        logger.info(
            "[POLICY_REF_TRACE] final case_type=%s selected_refs=%s selected_preview=%s",
            case_type or "-",
            len(refs),
            [_ref_log_label(r) for r in refs],
        )

        candidates_with_reason = []
        for r in candidates:
            ref = dict(r)
            ref.setdefault("adoption_reason", _adoption_reason_for_ref(ref, enriched_body))
            candidates_with_reason.append(ref)
    return {
        "tool": "policy_rulebook_probe",
        "ok": True,
        "facts": {
            "policy_refs": refs,
            "ref_count": len(refs),
            "retrieval_candidates": candidates_with_reason,
            "enriched_from_prior": [_tool_key(r) for r in prior],
        },
        "summary": (
            f"규정집에서 관련 조항 {len(refs)}건을 조회했습니다."
            + (f" (prior {len(prior)}개 결과 반영)" if prior else "")
        ),
    }


async def legacy_aura_deep_audit(context: dict[str, Any]) -> dict[str, Any]:
    if not settings.enable_legacy_aura_specialist:
        return {
            "tool": "legacy_aura_deep_audit",
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
        "tool": "legacy_aura_deep_audit",
        "ok": final_payload is not None,
        "facts": final_payload or {},
        "trace": trace,
        "summary": "기존 Aura 심층 감사 분석을 호출했습니다.",
    }


TOOL_REGISTRY: dict[str, AgentTool] = {
    "holiday_compliance_probe": AgentTool(
        name="holiday_compliance_probe",
        description="휴일/휴무/연차 사용 정황을 검증한다.",
        handler=holiday_compliance_probe,
        display_summary_ko="입력: 전표 발생시각, 금액, 근태 상태. 출력: 휴일 여부, 판정 사유, 적용 규정 후보.",
    ),
    "budget_risk_probe": AgentTool(
        name="budget_risk_probe",
        description="예산 초과 여부와 금액 지표를 검증한다.",
        handler=budget_risk_probe,
        display_summary_ko="입력: 전표 금액·예산 초과 플래그. 출력: 예산 초과 여부, 금액 지표.",
    ),
    "merchant_risk_probe": AgentTool(
        name="merchant_risk_probe",
        description="거래처와 가맹점 업종 코드(MCC) 기반 위험도를 검증한다.",
        handler=merchant_risk_probe,
        display_summary_ko="입력: 가맹점 업종 코드(MCC), 거래처 정보. 출력: 업종 위험도, 판정 근거.",
    ),
    "document_evidence_probe": AgentTool(
        name="document_evidence_probe",
        description="전표 라인아이템과 문서 증거를 수집한다.",
        handler=document_evidence_probe,
        display_summary_ko="입력: 전표·문서. 출력: 라인 수, 라인아이템 요약.",
    ),
    "policy_rulebook_probe": AgentTool(
        name="policy_rulebook_probe",
        description="내부 규정집에서 관련 조항을 조회한다.",
        handler=policy_rulebook_probe,
        display_summary_ko="입력: 케이스·키워드. 출력: 규정 후보, 채택 조항, adoption_reason.",
    ),
    "legacy_aura_deep_audit": AgentTool(
        name="legacy_aura_deep_audit",
        description="기존 Aura 심층 분석을 전문 감사 툴로 호출한다.",
        handler=legacy_aura_deep_audit,
        display_summary_ko="입력: 케이스·body_evidence. 출력: legacy 심층 분석 결과(조건부 호출).",
    ),
}


def _make_langchain_tool(tool_name: str) -> StructuredTool:
    """LangChain StructuredTool로 등록된 도구를 감싼다. 입력/출력 스키마 부여."""
    entry = TOOL_REGISTRY[tool_name]

    async def _invoke(**kwargs: Any) -> dict[str, Any]:
        inp = ToolContextInput.model_validate(kwargs)
        ctx: dict[str, Any] = {
            "case_id": inp.case_id,
            "body_evidence": inp.body_evidence,
            "intended_risk_type": inp.intended_risk_type,
            "prior_tool_results": inp.prior_tool_results,
        }
        return await entry.handler(ctx)

    return StructuredTool(
        name=entry.name,
        description=entry.description,
        args_schema=ToolContextInput,
        coroutine=_invoke,
    )


def get_langchain_tools() -> list[StructuredTool]:
    """등록된 모든 도구를 LangChain tool 목록으로 반환. execute 노드에서 이름으로 조회해 호출."""
    return [_make_langchain_tool(name) for name in TOOL_REGISTRY]
