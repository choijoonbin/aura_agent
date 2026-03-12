from __future__ import annotations

import json
import logging
from typing import Any

from agent.langgraph_domain import _find_tool_result
from utils.config import settings
from utils.llm_azure import completion_kwargs_for_azure

logger = logging.getLogger(__name__)

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
    present_keys = {
        "occurredat": _is_present_value(body.get("occurredAt")),
        "transaction_datetime": _is_present_value(body.get("occurredAt")),
        "merchantname": _is_present_value(body.get("merchantName")),
        "amount": _is_present_value(body.get("amount")),
        "mcccode": _is_present_value(body.get("mccCode")),
        "budgetexceeded": _is_present_value(body.get("budgetExceeded")),
        "hrstatus": _is_present_value(body.get("hrStatus")),
    }
    missing: list[dict[str, str]] = []
    for req in required_inputs:
        field = str(req.get("field", "")).strip()
        key = field.replace("_", "").lower()
        if key and present_keys.get(key):
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
    return any(kw.lower() in lowered for kw in _EXCLUDED_REQUIRED_INPUT_KEYWORDS)


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
        "각 항목은 {\"field\": \"영문식별자\", \"reason\": \"규정에서 요구하는 이유 한 줄\", \"guide\": \"사용자에게 보여줄 가이드 문구\"} 형태로.\n"
        "2. 규정에서 예외·승인·검토 시 확인하라고 한 내용을 review_questions로 짧은 질문 문장으로 나열하라. "
        "질문은 최대 3개만. 유사·중복 질문은 제외하고, 판단에 가장 중요한 핵심만 선별하라.\n"
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
        required_inputs = [x for x in required_inputs if not _is_excluded_required_input(x)]
        review_questions = [str(q).strip() for q in review_questions if str(q).strip()][:3]
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
                filtered_required_inputs = [x for x in filtered_required_inputs if not _is_excluded_required_input(x)]
            logger.info(
                "derive_hitl_from_regulation required_inputs filtered: before=%s after=%s",
                len(required_inputs),
                len(filtered_required_inputs),
            )
        except Exception:
            filtered_required_inputs = _filter_required_inputs_by_presence_fallback(required_inputs, body)
            filtered_required_inputs = [x for x in filtered_required_inputs if not _is_excluded_required_input(x)]
            logger.info(
                "derive_hitl_from_regulation required_inputs fallback filter applied: before=%s after=%s",
                len(required_inputs),
                len(filtered_required_inputs),
            )

        return {"required_inputs": filtered_required_inputs, "review_questions": review_questions}
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
        "2. review_questions: 검토자가 검토의견에 답해야 할 질문을 2~3개만 작성하라. 중복·유사 질문 없이, 판단에 가장 중요한 핵심만 선별. 분석 결과와 연결된 구체적 질문으로. 예: '휴일 사용 사전 승인 여부를 확인했는가?'\n"
        "반드시 JSON만 응답: {\"review_reasons\": [\"...\", ...], \"review_questions\": [\"...\", ...]}\n"
        "review_reasons는 최소 1개, review_questions는 2~3개(최대 3개) 필수."
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

    baseline = {"review_reasons": dedup_reasons[:5], "review_questions": dedup_questions[:3]}

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
        return {"review_reasons": merged_reasons[:5], "review_questions": merged_questions[:3]}
    except Exception:
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
        "2. review_questions: 검토자가 검토의견에 답해야 할 질문을 2~3개만 작성하라. 중복·유사 없이 핵심만. 분석 결과와 연결된 구체적 질문으로.\n"
        "3. empty_explanation: (선택) 위 두 항목이 비어 있었을 수 있는 이유를 한 줄로.\n"
        "반드시 JSON만 응답: {\"review_reasons\": [\"...\"], \"review_questions\": [\"...\"], \"empty_explanation\": \"...\"}\n"
        "review_reasons는 최소 1개, review_questions는 2~3개(최대 3개) 필수."
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
        return {"review_reasons": reasons[:5], "review_questions": questions[:3]}
    except Exception:
        return {"review_reasons": [], "review_questions": []}
