from __future__ import annotations

import json
import logging
from typing import Any

from agent.langgraph_domain import _find_tool_result
from services.policy_case_alignment import has_business_trip_context, has_entertainment_context
from utils.config import settings
from utils.llm_azure import completion_kwargs_for_azure

logger = logging.getLogger(__name__)
MAX_HITL_QUESTIONS = 2
MAX_HITL_INPUTS = 2  # 필수 입력 항목 최대 수 (UI 과밀 방지 + 검토 집중도 향상)

_CLAIM_PRIORITY: dict[str, int] = {
    "night_violation": 10,
    "holiday_hr_conflict": 9,
    "merchant_high_risk": 8,
    "budget_exceeded": 7,
    "amount_approval_tier": 6,
    "policy_ref_direct": 5,
}

# 전 케이스 공통 제외 대상(required_inputs로 올리지 않음)
# 요청 항목: 거래일시, 가맹점/사업자번호, 품목/서비스, 공급가액·세액·총액, 결제수단, 업무 관련성
_EXCLUDED_REQUIRED_INPUT_KEYWORDS = (
    "공통 증빙 의무",
    "모든 경비 지출은",
    "증빙을 구비",
    "법적·내부 기준",
    "증빙",
    "거래일시",
    "발생일시",
    "발생 시각",
    "거래처명",
    "가맹점",
    "사업자등록번호",
    "품목",
    "서비스",
    "내역",
    "공급가액",
    "세액",
    "합계금액",
    "총 결제 금액",
    "결제수단",
    "법인카드",
    "계좌이체",
    "현금",
    "업무 목적",
    "업무관련성",
    "프로젝트",
    "코스트센터",
    "참석자 수",
    "참석자",
    "내부/외부 구분",
    "외부 참석자",
    "외부 참석자 소속",
    "소속 정보",
    "접대 목적",
)

_ENTERTAINMENT_ONLY_HITL_KEYWORDS = (
    "접대비",
    "업무추진비",
    "참석자 명단",
    "참석자 수",
    "참석자",
    "내부/외부",
    "내부 외부",
    "내부참석자",
    "외부참석자",
    "외부 참석자",
    "외부 참석자 소속",
    "접대 목적",
    "외부 이해관계자",
)

_TRAVEL_ONLY_HITL_KEYWORDS = (
    "출장",
    "출장비",
    "출장명령",
    "출장계획",
    "출장번호",
    "출장기간",
    "출장지",
    "교통비",
    "숙박비",
    "일비",
    "교통/숙박",
)


def _is_present_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    return True


def _filter_required_inputs_by_presence_fallback(required_inputs: list[dict[str, str]], body: dict[str, Any]) -> list[dict[str, str]]:
    """LLM 실패 시 최소 안전 필터: body_evidence에서 명백히 존재하는 항목은 제거."""
    if not required_inputs:
        return []
    doc = body.get("document") or {}
    attendees = body.get("attendees") or doc.get("attendees") or []
    present_keys = {
        "occurredat": _is_present_value(body.get("occurredAt")),
        "transaction_datetime": _is_present_value(body.get("occurredAt")),
        "merchantname": _is_present_value(body.get("merchantName")),
        "amount": _is_present_value(body.get("amount")),
        "mcccode": _is_present_value(body.get("mccCode")),
        "budgetexceeded": _is_present_value(body.get("budgetExceeded")),
        "hrstatus": _is_present_value(body.get("hrStatus")),
        "businesspurpose": _is_present_value(body.get("businessPurpose") or doc.get("businessPurpose")),
        "attendees": _is_present_value(attendees),
        "attendeecount": _is_present_value(body.get("attendeeCount") or doc.get("attendeeCount")),
        "location": _is_present_value(body.get("location") or doc.get("location")),
        "paymentmethod": _is_present_value(body.get("paymentMethod") or doc.get("paymentMethod")),
        "evidenceprovided": _is_present_value(body.get("evidenceProvided") or doc.get("receiptQualified")),
    }
    missing: list[dict[str, str]] = []
    for req in required_inputs:
        field = str(req.get("field", "")).strip()
        key = field.replace("_", "").replace(" ", "").lower()
        combined = " ".join(
            [
                field,
                str(req.get("reason", "")).strip(),
                str(req.get("guide", "")).strip(),
            ]
        ).lower()
        if key and present_keys.get(key):
            continue
        # 한국어 표현 기반 보강 매핑 (필드명이 자유 텍스트일 때 대비)
        if "참석자" in combined and present_keys["attendees"]:
            continue
        if ("업무 목적" in combined or "업무목적" in combined) and present_keys["businesspurpose"]:
            continue
        if "장소" in combined and present_keys["location"]:
            continue
        if ("결제수단" in combined or "법인카드" in combined or "계좌이체" in combined) and present_keys["paymentmethod"]:
            continue
        if "증빙" in combined and present_keys["evidenceprovided"]:
            continue
        missing.append(req)
    return missing


def _is_excluded_required_input(req: dict[str, str]) -> bool:
    text = " ".join(
        [
            str(req.get("field", "")).strip(),
            str(req.get("reason", "")).strip(),
            str(req.get("guide", "")).strip(),
        ]
    )
    if not text:
        return False
    lowered = text.lower()
    lowered_compact = "".join(lowered.split())
    return any(
        (kw.lower() in lowered) or ("".join(kw.lower().split()) in lowered_compact)
        for kw in _EXCLUDED_REQUIRED_INPUT_KEYWORDS
    )


def _is_holiday_non_entertainment_case(body: dict[str, Any]) -> bool:
    case_type = str(body.get("case_type") or body.get("intended_risk_type") or "").upper()
    return case_type == "HOLIDAY_USAGE" and not has_entertainment_context(body)


def _is_holiday_non_trip_case(body: dict[str, Any]) -> bool:
    case_type = str(body.get("case_type") or body.get("intended_risk_type") or "").upper()
    return case_type == "HOLIDAY_USAGE" and not has_business_trip_context(body)


def _is_entertainment_only_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(token.lower() in lowered for token in _ENTERTAINMENT_ONLY_HITL_KEYWORDS)


def _is_travel_only_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(token.lower() in lowered for token in _TRAVEL_ONLY_HITL_KEYWORDS)


def _filter_hitl_content_by_case(
    *,
    body: dict[str, Any],
    required_inputs: list[dict[str, str]],
    review_questions: list[str],
) -> tuple[list[dict[str, str]], list[str]]:
    if not _is_holiday_non_entertainment_case(body):
        return required_inputs, review_questions
    block_travel = _is_holiday_non_trip_case(body)

    filtered_required_inputs: list[dict[str, str]] = []
    for req in required_inputs:
        combined = " ".join(
            [str(req.get("field", "")), str(req.get("reason", "")), str(req.get("guide", ""))]
        )
        if _is_entertainment_only_text(combined):
            continue
        if block_travel and _is_travel_only_text(combined):
            continue
        filtered_required_inputs.append(req)

    filtered_questions = []
    for q in review_questions:
        text = str(q or "").strip()
        if not text:
            continue
        if _is_entertainment_only_text(text):
            continue
        if block_travel and _is_travel_only_text(text):
            continue
        # HOLIDAY_USAGE(비접대)에서는 참석자/내·외부 구분 질문을 추가 차단
        lowered = text.lower().replace(" ", "")
        if (
            ("참석자" in text)
            or ("참석자수" in lowered)
            or ("내부/외부" in text)
            or ("내부외부" in lowered)
            or ("외부소속" in lowered)
        ):
            continue
        filtered_questions.append(text)
    return filtered_required_inputs, filtered_questions


def _build_verification_targets(state: dict[str, Any]) -> list[str]:
    """
    Verifier가 검증할 구체적·반박 가능한 주장 문장 최대 4개 생성.

    설계 원칙:
    1) 전표 사실(시간·금액·근태·MCC)을 주장에 직접 삽입
    2) 특정 조항 번호(제XX조 ③항)까지 명시
    3) "적용될 수 있음" 대신 "해당한다 / 위반 가능성" 수준의 주장
    4) _chunk_supports_claim()이 단순 단어 중복만으로 통과하지 못하도록 충분히 구체화
    5) tool_results의 실제 facts 값을 반드시 참조
    """
    body = state["body_evidence"]
    flags = state.get("flags") or {}
    tool_results = state.get("tool_results") or []

    occurred_at = str(body.get("occurredAt") or "")
    date_part = occurred_at[:10] if len(occurred_at) >= 10 else "날짜 미상"
    time_part = occurred_at[11:16] if len(occurred_at) >= 16 else ""
    amount = body.get("amount")
    amount_str = f"{int(amount):,}원" if amount else "금액 미상"
    merchant = body.get("merchantName") or "거래처 미상"
    mcc_code = body.get("mccCode") or flags.get("mccCode") or ""
    mcc_name = body.get("mccName") or ""
    hr_status = str(flags.get("hrStatus") or body.get("hrStatus") or "").upper()
    is_holiday = bool(flags.get("isHoliday") or body.get("isHoliday"))
    is_night = bool(flags.get("isNight"))
    budget_exceeded = bool(flags.get("budgetExceeded"))

    holiday_facts = (_find_tool_result(tool_results, "holiday_compliance_probe") or {}).get("facts") or {}
    merchant_facts = (_find_tool_result(tool_results, "merchant_risk_probe") or {}).get("facts") or {}
    policy_facts = (_find_tool_result(tool_results, "policy_rulebook_probe") or {}).get("facts") or {}

    merchant_risk = str(merchant_facts.get("merchantRisk") or "").upper()
    holiday_risk = bool(holiday_facts.get("holidayRisk"))
    policy_refs = policy_facts.get("policy_refs") or []

    claims: list[tuple[int, str]] = []

    if is_night and time_part:
        claims.append((
            _CLAIM_PRIORITY["night_violation"],
            f"{date_part} {time_part} 심야 시간대에 {merchant}에서 {amount_str} 결제가 발생하여 "
            f"제23조 ③-1항 '23:00~06:00 심야 식대 경고 대상' 및 "
            f"제38조 ②항 '심야 시간대 지출 검토 대상'에 해당한다.",
        ))

    if is_holiday and hr_status in {"LEAVE", "OFF", "VACATION"}:
        hr_label = {"LEAVE": "휴가·결근", "OFF": "휴무", "VACATION": "휴가"}.get(hr_status, hr_status)
        claims.append((
            _CLAIM_PRIORITY["holiday_hr_conflict"],
            f"결제일({date_part}) 근태 상태 {hr_status}({hr_label}) 및 휴일 결제가 동시에 확인되어 "
            f"제39조 ①항 주말·공휴일 지출 제한과 "
            f"제23조 ③-2항 '주말/공휴일 식대(예외 승인 없는 경우)' 경고 조건 모두에 해당한다.",
        ))
    elif (is_holiday or holiday_risk) and not hr_status:
        claims.append((
            _CLAIM_PRIORITY["holiday_hr_conflict"] - 1,
            f"결제일({date_part})이 휴일로 확인되나 근태 상태 데이터가 누락되어 "
            f"제39조 주말·공휴일 지출 제한 적용 여부 완전 판단이 불가하다. 근태 보완 후 재검토 필요.",
        ))

    if merchant_risk in {"HIGH", "CRITICAL"} and mcc_code:
        mcc_display = f"MCC {mcc_code}({mcc_name})" if mcc_name else f"MCC {mcc_code}"
        compound = "복합 위험" if merchant_risk == "CRITICAL" else "고위험"
        claims.append((
            _CLAIM_PRIORITY["merchant_high_risk"],
            f"{merchant}({mcc_display})은 제42조 {compound} 업종으로 분류되어 "
            f"금액과 무관하게 강화 승인 대상이며, 제11조 ③항 고위험 업종 거래 강화 승인 조건을 충족한다.",
        ))
    elif merchant_risk == "MEDIUM" and mcc_code:
        claims.append((
            _CLAIM_PRIORITY["merchant_high_risk"] - 2,
            f"{merchant}(MCC {mcc_code}) 업종 위험도 MEDIUM으로 제42조 업종 제한 기준 검토 대상이다.",
        ))

    if budget_exceeded:
        claims.append((
            _CLAIM_PRIORITY["budget_exceeded"],
            f"{amount_str} 결제가 예산 한도를 초과하여 제40조 ①항 금액·누적한도 제약 및 "
            f"제19조 ①항 예산 초과 처리 기준에 따른 상위 승인이 필요하다.",
        ))

    if amount and not budget_exceeded:
        if amount >= 2_000_000:
            claims.append((
                _CLAIM_PRIORITY["amount_approval_tier"],
                f"{amount_str}은 제11조 ②-4항 임원·CFO 승인 구간(200만원 초과)에 해당하며 "
                f"증빙 완결성과 결재권자 확인이 필수이다.",
            ))
        elif amount >= 500_000:
            claims.append((
                _CLAIM_PRIORITY["amount_approval_tier"],
                f"{amount_str}은 제11조 ②-3항 본부장 승인 구간(50만~200만원)에 해당한다.",
            ))
        elif amount >= 100_000:
            claims.append((
                _CLAIM_PRIORITY["amount_approval_tier"] - 1,
                f"{amount_str}은 제11조 ②-2항 부서장 승인 구간(10만~50만원)에 해당한다.",
            ))

    for ref in policy_refs[:2]:
        article = ref.get("article") or ""
        parent_title = (ref.get("parent_title") or "")[:35]
        reason = ref.get("adoption_reason") or ""
        if article:
            reason_part = f" ({reason})" if reason else ""
            claims.append((
                _CLAIM_PRIORITY["policy_ref_direct"],
                f"policy_rulebook_probe 채택 조항 {article}({parent_title}){reason_part}이 "
                f"{merchant} {amount_str} 전표에 직접 적용 가능한 위반 근거를 갖는다.",
            ))

    claims.sort(key=lambda item: item[0], reverse=True)
    if claims:
        return [text for _, text in claims[:4]]

    return [
        f"{merchant} {amount_str} 전표({date_part})가 사내 경비 지출 관리 규정 위반 여부 "
        f"검토 대상으로 판정되었으며 세부 조항 적용 근거 확인이 필요하다."
    ]


async def _derive_hitl_from_regulation(state: dict[str, Any]) -> dict[str, Any]:
    """
    규정 본문(chunk_text)을 바탕으로 에이전트가 필수 입력/증빙과 검토 질문을 추출한다.
    하드코딩된 케이스별 규칙이 아니라, 적용 규정의 '필수 입력/증빙' 등 문구를 읽어 HITL 요청 내용을 만든다.
    """
    refs = (_find_tool_result(state.get("tool_results", []), "policy_rulebook_probe") or {}).get("facts", {}).get("policy_refs") or []
    body = state.get("body_evidence") or {}
    regulation_texts: list[str] = []
    for ref in refs[:5]:
        chunk_text = (ref.get("chunk_text") or "").strip()
        article = ref.get("article") or ref.get("regulation_article") or ""
        parent_title = ref.get("parent_title") or ""
        if chunk_text:
            regulation_texts.append(f"[{article} {parent_title}]\n{chunk_text}")
    if not regulation_texts:
        return {}

    case_summary = (
        f"발생시각: {body.get('occurredAt')} / 가맹점: {body.get('merchantName')} / "
        f"휴일여부: {body.get('isHoliday')} / 근태: {body.get('hrStatus')} / 예산초과: {body.get('budgetExceeded')}"
    )
    system_prompt = (
        "당신은 경비 규정을 적용하는 감사 에이전트다. 아래 '적용 규정 조문'에 적힌 내용만을 근거로, "
        "담당자 검토(HITL) 시 요구할 **필수 입력/증빙** 항목과 **검토 시 확인할 질문**을 추출하라.\n"
        "규칙:\n"
        "1. 규정에 '필수 입력', '필수 증빙', '② 필수' 등으로 열거된 항목을 required_inputs로 나열하라. "
        "각 항목은 {\"field\": \"영문식별자\", \"reason\": \"규정에서 요구하는 이유 한 줄\", \"guide\": \"사용자에게 보여줄 가이드 문구\"} 형태로. "
        "**필수 입력은 최대 2개만.** 여러 항목이 있으면 판단에 가장 핵심적인 것 2개만 중요도 높은 순으로 선별하라. "
        "유사·중복 항목은 하나로 합쳐라.\n"
        "2. 규정에서 예외·승인·검토 시 확인하라고 한 내용을 review_questions로 짧은 질문 문장으로 나열하라. "
        "질문은 최대 2개만. 유사·중복 질문은 제외하고, 판단에 가장 중요한 핵심만 선별하라.\n"
        "3. 현재 케이스(휴일/심야/접대 등)에 실제로 해당하는 조문만 사용하라. 해당 없으면 빈 배열을 반환하라.\n"
        "4. 반드시 JSON만 응답하라: {\"required_inputs\": [...], \"review_questions\": [...]}\n"
    )
    user_prompt = f"현재 케이스 요약: {case_summary}\n\n적용 규정 조문:\n\n" + "\n\n---\n\n".join(regulation_texts)

    if not getattr(settings, "openai_api_key", None):
        return {}
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
            client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base_url or None)

        tok_kw = {"max_completion_tokens": 1500} if is_azure else {"max_tokens": 1500}
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
        required_inputs = parsed.get("required_inputs") or []
        review_questions = parsed.get("review_questions") or []
        if not isinstance(required_inputs, list):
            required_inputs = []
        if not isinstance(review_questions, list):
            review_questions = []
        required_inputs = [
            {"field": str(x.get("field", "")), "reason": str(x.get("reason", "")), "guide": str(x.get("guide", ""))}
            for x in required_inputs if isinstance(x, dict)
        ]
        review_questions = [str(q).strip() for q in review_questions if str(q).strip()][:MAX_HITL_QUESTIONS]
        required_inputs, review_questions = _filter_hitl_content_by_case(
            body=body,
            required_inputs=required_inputs,
            review_questions=review_questions,
        )
        if not required_inputs:
            return {"required_inputs": [], "review_questions": review_questions}

        # 2차 필터(LLM): 추출 체크리스트 중 body_evidence에 이미 값이 존재하는 항목을 제거하고,
        # 실제 누락으로 판단되는 필드만 required_inputs로 남긴다.
        presence_prompt_sys = (
            "당신은 입력값 존재 여부를 판별하는 검증기다. "
            "required_inputs 항목마다 case_data에 이미 값이 존재하는지 판단해, "
            "실제로 누락된 항목만 missing_required_inputs로 반환하라. "
            "값이 명확히 있으면 제외하고, 없거나 불명확하면 포함한다. "
            "반드시 JSON만 응답: {\"missing_required_inputs\": [{\"field\":\"...\",\"reason\":\"...\",\"guide\":\"...\"}]}"
        )
        presence_payload = {
            "required_inputs": required_inputs,
            "case_data": {
                "occurredAt": body.get("occurredAt"),
                "merchantName": body.get("merchantName"),
                "amount": body.get("amount"),
                "mccCode": body.get("mccCode"),
                "budgetExceeded": body.get("budgetExceeded"),
                "hrStatus": body.get("hrStatus"),
                "isHoliday": body.get("isHoliday"),
                "document": body.get("document"),
            },
        }
        filtered_required_inputs = required_inputs
        try:
            response2 = await client.chat.completions.create(
                **completion_kwargs_for_azure(
                    base_url,
                    model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": presence_prompt_sys},
                        {"role": "user", "content": json.dumps(presence_payload, ensure_ascii=False)},
                    ],
                    **tok_kw,
                ),
            )
            raw2 = (response2.choices[0].message.content or "").strip()
            parsed2 = json.loads(raw2)
            missing_required = parsed2.get("missing_required_inputs") or []
            if isinstance(missing_required, list):
                filtered_required_inputs = [
                    {"field": str(x.get("field", "")), "reason": str(x.get("reason", "")), "guide": str(x.get("guide", ""))}
                    for x in missing_required
                    if isinstance(x, dict) and str(x.get("field", "")).strip()
                ]
            logger.info(
                "[hitl] 필수 입력 필터: before=%s after=%s",
                len(required_inputs),
                len(filtered_required_inputs),
            )
        except Exception:
            filtered_required_inputs = _filter_required_inputs_by_presence_fallback(required_inputs, body)
            logger.info(
                "derive_hitl_from_regulation required_inputs fallback filter applied: before=%s after=%s",
                len(required_inputs),
                len(filtered_required_inputs),
            )

        filtered_required_inputs, review_questions = _filter_hitl_content_by_case(
            body=body,
            required_inputs=filtered_required_inputs,
            review_questions=review_questions,
        )

        # 최대 2개로 제한 — 중요도 높은 순으로 앞에 위치한다고 가정
        return {"required_inputs": filtered_required_inputs[:MAX_HITL_INPUTS], "review_questions": review_questions}
    except Exception:
        return {}


async def _generate_hitl_review_content(
    hitl_request: dict[str, Any],
    verification_summary: dict[str, Any],
    claim_results: list[dict[str, Any]],
    reasoning_text: str,
) -> dict[str, Any]:
    """
    담당자 검토가 필요하다고 판단된 맥락을 바탕으로, LLM이 검토 필요 사유와 검토자가 답해야 할 질문을 생성한다.
    반환: {"review_reasons": list[str], "review_questions": list[str]} (각 1개 이상 보장)
    """
    why = (hitl_request.get("why_hitl") or "").strip()
    blockers = hitl_request.get("reasons") or hitl_request.get("auto_finalize_blockers") or []
    missing = hitl_request.get("missing_citations") or []
    covered = verification_summary.get("covered")
    total = verification_summary.get("total")
    coverage_note = f"검증 대상 {total}개 중 {covered}개만 규정 근거와 연결됨." if (total and total > 0) else ""

    claim_lines: list[str] = []
    for r in (claim_results or [])[:6]:
        c = r.get("claim") or ""
        cov = r.get("covered")
        gap = (r.get("gap") or "").strip()
        if c:
            claim_lines.append(f"- {c[:120]}{'…' if len(c) > 120 else ''} | 연결: {'예' if cov else '아니오'}{f' | 부족: {gap}' if gap else ''}")

    system_prompt = (
        "당신은 경비 감사 에이전트다. 이 전표는 담당자 검토(HITL)가 필요한 것으로 판정되었다. "
        "아래 맥락(분석 과정에서 나온 근거)만을 사용하여 다음 두 가지를 반드시 생성하라.\n"
        "1. review_reasons: 검토가 필요한 이유를 담당자가 이해할 수 있는 문장 1~5개. 분석 결과 기반으로 명확히.\n"
        "2. review_questions: 검토자가 검토의견에 답해야 할 질문을 1~2개만 작성하라. 중복·유사 질문 없이, 판단에 가장 중요한 핵심만 선별. 분석 결과와 연결된 구체적 질문으로. 예: '휴일 사용 사전 승인 여부를 확인했는가?'\n"
        "3. 질문 품질 규칙: 각 질문은 케이스 특이값(예: 휴일/근태/심야/가맹점/제N조/승인여부) 중 최소 1개 이상을 포함해야 한다.\n"
        "4. 추상 일반론 금지: '단일 문헌 의존', '추가 자료가 있는가'처럼 맥락 없는 일반 질문은 금지한다.\n"
        "반드시 JSON만 응답: {\"review_reasons\": [\"...\", ...], \"review_questions\": [\"...\", ...]}\n"
        "review_reasons는 최소 1개, review_questions는 1~2개(최대 2개) 필수."
    )
    user_parts = [f"검증 판단 요약: {reasoning_text[:500]}" if reasoning_text else ""]
    if why:
        user_parts.append(f"자동 확정 중단 이유: {why}")
    if blockers:
        user_parts.append("자동 확정 차단 사유: " + "; ".join(str(b) for b in blockers[:5]))
    if coverage_note:
        user_parts.append(coverage_note)
    if missing:
        user_parts.append("근거 미연결 주장: " + " | ".join((m or "")[:80] for m in missing[:3]))
    if claim_lines:
        user_parts.append("주장별 검증 결과:\n" + "\n".join(claim_lines))
    user_prompt = "\n\n".join(p for p in user_parts if p).strip() or "검토 필요로 판정됨. 사유와 질문을 생성하라."

    base_reasons: list[str] = []
    base_questions: list[str] = []
    for s in (hitl_request.get("unresolved_claims") or []):
        t = str(s or "").strip()
        if t:
            base_reasons.append(t)
    if why:
        base_reasons.append(why)
    for s in (hitl_request.get("review_questions") or hitl_request.get("questions") or []):
        t = str(s or "").strip()
        if t:
            base_questions.append(t)
    for req in (hitl_request.get("required_inputs") or []):
        q = str(req.get("guide") or req.get("reason") or "").strip()
        if q:
            base_questions.append(q)
    for r in (claim_results or [])[:4]:
        claim = str(r.get("claim") or "").strip()
        covered_flag = bool(r.get("covered"))
        gap = str(r.get("gap") or "").strip()
        if not covered_flag and claim:
            if gap:
                base_reasons.append(f"미검증 주장: {claim[:120]}{'…' if len(claim) > 120 else ''} ({gap[:100]})")
            else:
                base_reasons.append(f"미검증 주장: {claim[:120]}{'…' if len(claim) > 120 else ''}")
            base_questions.append(f"다음 주장에 대한 근거를 확인할 수 있는가: {claim[:110]}{'…' if len(claim) > 110 else ''}")
    dedup_reasons: list[str] = []
    seen_r: set[str] = set()
    for s in base_reasons:
        k = s.strip()
        if not k or k in seen_r:
            continue
        seen_r.add(k)
        dedup_reasons.append(k)
    dedup_questions: list[str] = []
    seen_q: set[str] = set()
    for s in base_questions:
        k = s.strip()
        if not k or k in seen_q:
            continue
        seen_q.add(k)
        dedup_questions.append(k)
    if not dedup_reasons:
        dedup_reasons = ["자동 판정을 보류한 근거를 담당자 확인이 필요합니다."]
    if not dedup_questions:
        if why:
            dedup_questions = [f"자동 판정 보류 사유를 해소할 근거를 확인할 수 있는가: {why[:180]}{'…' if len(why) > 180 else ''}"]
        else:
            dedup_questions = ["검토 보류 사유를 해소할 추가 근거를 제출할 수 있는가?"]

    baseline = {"review_reasons": dedup_reasons[:5], "review_questions": dedup_questions[:MAX_HITL_QUESTIONS]}

    if not getattr(settings, "openai_api_key", None):
        return baseline
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
            client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base_url or None)

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
        reasons = parsed.get("review_reasons") or []
        questions = parsed.get("review_questions") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)] if reasons else []
        if not isinstance(questions, list):
            questions = [str(questions)] if questions else []
        reasons = [str(s).strip() for s in reasons if str(s).strip()]
        questions = [str(q).strip() for q in questions if str(q).strip()]
        merged_reasons = reasons + [s for s in baseline["review_reasons"] if s not in set(reasons)]
        merged_questions = questions + [s for s in baseline["review_questions"] if s not in set(questions)]
        return {"review_reasons": merged_reasons[:5], "review_questions": merged_questions[:MAX_HITL_QUESTIONS]}
    except Exception:
        return baseline


async def _generate_claim_display_texts(
    claim_results: list[dict[str, Any]],
    body_evidence: dict[str, Any],
) -> list[str]:
    """
    claim_results의 내부 표현을 사용자 친화 문장으로 변환한다.
    반환 길이는 입력 claim_results 길이와 동일하게 맞춘다.
    """
    baseline = [str((r or {}).get("claim") or "").strip() for r in (claim_results or [])]
    baseline = [b if b else "-" for b in baseline]
    if not claim_results:
        logger.info("claim_display_text: skip (empty claim_results)")
        return []
    if not getattr(settings, "openai_api_key", None):
        logger.info("claim_display_text: fallback (openai_api_key missing)")
        return baseline

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
            client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base_url or None)

        merchant = str(body_evidence.get("merchantName") or "거래처 미상")
        occurred = str(body_evidence.get("occurredAt") or "")
        amount = body_evidence.get("amount")
        amount_text = f"{int(amount):,}원" if isinstance(amount, (int, float)) else "금액 미상"

        compact_rows: list[dict[str, Any]] = []
        for r in claim_results[:8]:
            compact_rows.append(
                {
                    "claim": str((r or {}).get("claim") or ""),
                    "covered": bool((r or {}).get("covered")),
                    "supporting_articles": [str(a) for a in ((r or {}).get("supporting_articles") or [])][:3],
                    "gap": str((r or {}).get("gap") or "")[:120],
                }
            )

        system_prompt = (
            "당신은 감사 UI 문구 작성자다. 내부 claim 문장을 사용자 관점에서 이해하기 쉬운 한국어 요약으로 바꿔라.\n"
            "규칙:\n"
            "1) 각 claim마다 1~2문장으로 핵심 의미를 쉽게 설명\n"
            "2) 내부 구현 용어(policy_rulebook_probe, retrieval 등) 금지\n"
            "3) 가능하면 조문번호(제N조)와 판단 포인트(왜 이 규정이 연결됐는지)를 포함\n"
            "4) 판정을 과장하지 말고, 사실 기반으로 설명\n"
            "5) 실무 사용자가 바로 이해할 수 있는 표현 사용(기술 용어 최소화)\n"
            "JSON만 출력: {\"display_texts\": [\"...\", ...]}\n"
        )
        user_prompt = (
            f"전표 요약: 거래처={merchant}, 금액={amount_text}, 발생시각={occurred}\n\n"
            f"claim_results:\n{json.dumps(compact_rows, ensure_ascii=False)}\n\n"
            "입력 순서와 동일한 길이의 display_texts 배열을 반환하라."
        )

        tok_kw = {"max_completion_tokens": 1200} if is_azure else {"max_tokens": 1200}
        logger.info(
            "[verify:claim] LLM 요청 | model=%s claims=%s azure=%s",
            getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
            len(claim_results),
            is_azure,
        )
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
        texts = parsed.get("display_texts") or []
        if not isinstance(texts, list):
            logger.warning("[verify:claim] fallback (display_texts not list)")
            return baseline
        cleaned = [str(t).strip() for t in texts]
        if len(cleaned) != len(claim_results):
            # 길이가 다르면 안전하게 baseline 사용
            logger.warning(
                "[verify:claim] fallback (length mismatch llm=%s claims=%s)",
                len(cleaned),
                len(claim_results),
            )
            return baseline
        logger.info("[verify:claim] 완료 | count=%s", len(cleaned))
        return [c if c else b for c, b in zip(cleaned, baseline)]
    except Exception as ex:
        logger.exception("[verify:claim] fallback (llm error: %s)", ex)
        return baseline


async def _retry_fill_hitl_review_when_empty(
    hitl_request: dict[str, Any],
    verification_summary: dict[str, Any],
    claim_results: list[dict[str, Any]],
    reasoning_text: str,
    *,
    empty_reasons: bool,
    empty_questions: bool,
) -> dict[str, Any]:
    """
    검토 필요로 판정했는데 검토 필요 사유 또는 검토 시 답해야 할 질문이 비어 있을 때,
    LLM에게 분석 결과를 바탕으로 반드시 두 항목을 채우라고 재지시한다.
    """
    why = (hitl_request.get("why_hitl") or "").strip()
    blockers = hitl_request.get("reasons") or hitl_request.get("auto_finalize_blockers") or []
    covered = verification_summary.get("covered")
    total = verification_summary.get("total")
    coverage_note = f"검증 대상 {total}개 중 {covered}개만 규정 근거와 연결됨." if (total and total > 0) else ""

    claim_lines: list[str] = []
    for r in (claim_results or [])[:6]:
        c = r.get("claim") or ""
        cov = r.get("covered")
        gap = (r.get("gap") or "").strip()
        if c:
            claim_lines.append(f"- {c[:120]}{'…' if len(c) > 120 else ''} | 연결: {'예' if cov else '아니오'}{f' | 부족: {gap}' if gap else ''}")

    missing_what = []
    if empty_reasons:
        missing_what.append("검토 필요 사유")
    if empty_questions:
        missing_what.append("검토 시 답해야 할 질문")
    missing_str = ", ".join(missing_what)

    system_prompt = (
        "당신은 경비 감사 에이전트다. 이 전표는 '검토 필요'로 이미 판정된 건이다. "
        f"그런데 현재 {missing_str} 항목이 비어 있다. "
        "아래 분석 결과(검증 판단 요약, 자동 확정 차단 사유, 주장별 검증 결과 등)를 **근거**로 다음을 반드시 수행하라.\n"
        "1. review_reasons: 검토가 필요한 이유를 담당자가 이해할 수 있는 문장 1~5개. 분석 과정에서 나온 근거 기반으로 작성.\n"
        "2. review_questions: 검토자가 검토의견에 답해야 할 질문을 1~2개만 작성하라. 중복·유사 없이 핵심만. 분석 결과와 연결된 구체적 질문으로.\n"
        "3. 질문 품질 규칙: 각 질문은 케이스 특이값(휴일/근태/심야/가맹점/제N조/승인여부 등) 중 최소 1개 이상을 포함하라.\n"
        "4. 추상 일반론 금지: '단일 문헌 의존', '추가 자료가 있는가'처럼 맥락 없는 일반 질문은 금지한다.\n"
        "5. empty_explanation: (선택) 위 두 항목이 비어 있었을 수 있는 이유를 한 줄로.\n"
        "반드시 JSON만 응답: {\"review_reasons\": [\"...\"], \"review_questions\": [\"...\"], \"empty_explanation\": \"...\"}\n"
        "review_reasons는 최소 1개, review_questions는 1~2개(최대 2개) 필수."
    )
    user_parts = [f"검증 판단 요약: {reasoning_text[:600]}" if reasoning_text else ""]
    if why:
        user_parts.append(f"자동 확정 중단 이유: {why}")
    if blockers:
        user_parts.append("자동 확정 차단 사유: " + "; ".join(str(b) for b in blockers[:5]))
    if coverage_note:
        user_parts.append(coverage_note)
    if claim_lines:
        user_parts.append("주장별 검증 결과:\n" + "\n".join(claim_lines))
    user_prompt = "\n\n".join(p for p in user_parts if p).strip() or "분석 결과를 바탕으로 검토 필요 사유와 검토 시 답해야 할 질문을 생성하라."

    if not getattr(settings, "openai_api_key", None):
        return {"review_reasons": [], "review_questions": []}
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
            client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base_url or None)

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
        reasons = parsed.get("review_reasons") or []
        questions = parsed.get("review_questions") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)] if reasons else []
        if not isinstance(questions, list):
            questions = [str(questions)] if questions else []
        reasons = [str(s).strip() for s in reasons if str(s).strip()]
        questions = [str(q).strip() for q in questions if str(q).strip()]
        return {"review_reasons": reasons[:5], "review_questions": questions[:MAX_HITL_QUESTIONS]}
    except Exception:
        return {"review_reasons": [], "review_questions": []}


_QA_MATCH_FALLBACK: list[dict[str, Any]] = []

_BASIS_FIELD_LABELS: dict[str, str] = {
    "bktxt": "적요",
    "user_reason": "사용 사유",
    "sgtxt": "비고",
}


async def _match_questions_to_prior_answers(
    questions: list[str],
    body_evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    HITL 질문 목록과 전표 제출자의 기존 답변(bktxt/user_reason/sgtxt)을
    LLM으로 의미 매칭하여 각 질문의 커버리지를 판단한다.

    Returns:
        list of {
            "question": str,          # 원본 질문
            "covered": bool,          # 기존 답변으로 충분히 답변됨 여부
            "matched_answer": str,    # 매칭된 답변 발췌 (없으면 "")
            "basis_field": str,       # 출처 필드명 ("bktxt"/"user_reason"/"sgtxt" 또는 "")
        }

    LLM 호출 실패 시 모든 질문을 covered=False로 fallback 반환.
    """
    if not questions:
        return []

    memo = (body_evidence.get("memo") or {})
    prior: dict[str, str] = {
        "bktxt": str(memo.get("bktxt") or "").strip(),
        "user_reason": str(memo.get("user_reason") or "").strip(),
        "sgtxt": str(memo.get("sgtxt") or "").strip(),
    }
    # 기존 답변이 전혀 없으면 매칭 불가 — 모두 미확인으로 즉시 반환
    if not any(prior.values()):
        return [
            {"question": q, "covered": False, "matched_answer": "", "basis_field": ""}
            for q in questions
        ]

    prior_lines = "\n".join(
        f"- {_BASIS_FIELD_LABELS[k]}({k}): {v}"
        for k, v in prior.items()
        if v
    )
    q_lines = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))

    system_prompt = (
        "당신은 경비 감사 에이전트입니다. "
        "전표 제출자가 사전에 작성한 내용과 담당자 검토 질문을 비교하여, "
        "각 질문이 기존 작성 내용으로 충분히 답변되었는지 판단합니다.\n\n"
        "판단 기준:\n"
        "- covered=true: 기존 작성 내용 중 해당 질문에 실질적으로 답하는 내용이 있을 때\n"
        "- covered=false: 기존 내용에서 해당 질문과 관련된 언급이 없거나 불충분할 때\n\n"
        "반드시 아래 JSON 배열만 반환하세요 (다른 텍스트 없이):\n"
        '[\n'
        '  {\n'
        '    "index": 0,\n'
        '    "covered": true,\n'
        '    "matched_answer": "매칭된 원문 발췌 (없으면 빈 문자열)",\n'
        '    "basis_field": "bktxt 또는 user_reason 또는 sgtxt (없으면 빈 문자열)"\n'
        '  }\n'
        ']'
    )
    user_prompt = (
        f"## 전표 제출자 작성 내용\n{prior_lines}\n\n"
        f"## 담당자 검토 질문 목록\n{q_lines}\n\n"
        "각 질문(0-based index)에 대해 JSON 배열을 반환하세요."
    )

    fallback = [
        {"question": q, "covered": False, "matched_answer": "", "basis_field": ""}
        for q in questions
    ]

    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI

        base_url = (getattr(settings, "openai_base_url", None) or "").strip()
        api_key = getattr(settings, "openai_api_key", None) or ""
        api_version = getattr(settings, "openai_api_version", "2024-12-01-preview")
        timeout_sec = 10.0

        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client: Any = AsyncAzureOpenAI(
                api_key=api_key,
                azure_endpoint=azure_ep,
                api_version=api_version,
                timeout=timeout_sec,
            )
        else:
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url or None,
                timeout=timeout_sec,
            )

        model = getattr(settings, "reasoning_llm_model", "gpt-4o-mini")
        tok_kw = completion_kwargs_for_azure(
            base_url,
            temperature=0.0,
            max_tokens=512,
        )
        response = await client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            **tok_kw,
        )
        raw = (response.choices[0].message.content or "").strip()
        # LLM이 배열을 json_object 래퍼 안에 반환할 수 있으므로 처리
        parsed_root = json.loads(raw)
        if isinstance(parsed_root, list):
            items = parsed_root
        elif isinstance(parsed_root, dict):
            # {"matches": [...]} 또는 {"results": [...]} 등 래퍼 처리
            items = next(
                (v for v in parsed_root.values() if isinstance(v, list)),
                [],
            )
        else:
            items = []

        results: list[dict[str, Any]] = []
        for i, q in enumerate(questions):
            item = next((it for it in items if isinstance(it, dict) and it.get("index") == i), None)
            if item is None:
                results.append({"question": q, "covered": False, "matched_answer": "", "basis_field": ""})
            else:
                results.append({
                    "question": q,
                    "covered": bool(item.get("covered", False)),
                    "matched_answer": str(item.get("matched_answer") or "").strip(),
                    "basis_field": str(item.get("basis_field") or "").strip(),
                })
        logger.debug(
            "[verify] 사전 답변 매칭: %d/%d covered",
            sum(1 for r in results if r["covered"]),
            len(results),
        )
        return results
    except Exception as exc:
        logger.warning("[verify] 사전 답변 매칭 실패 (error: %s)", exc)
        return fallback
