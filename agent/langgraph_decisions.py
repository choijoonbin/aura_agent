from __future__ import annotations

import json
import logging
import re
from typing import Any

from agent.langgraph_domain import _find_tool_result, _format_occurred_at, _top_policy_refs, _voucher_summary_for_context
from agent.langgraph_hitl_helpers import _is_verify_ready_without_hitl
from agent.langgraph_scoring import _score_with_hitl_adjustment
from utils.config import settings
from utils.llm_azure import completion_kwargs_for_azure

logger = logging.getLogger(__name__)


def _strip_hold_prefix(text: str) -> str:
    """HOLD 문구 앞머리 중복 제거."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    patterns = [
        r"^\s*담당자\s*검토\s*결과\s*보류합니다\.?\s*",
        r"^\s*보류합니다\.?\s*",
    ]
    cleaned = raw
    for p in patterns:
        cleaned = re.sub(p, "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


async def _select_policy_refs_by_relevance(
    state: dict[str, Any],
    refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    LLM에게 케이스 유형·업종에 맞는 규정만 골라 인용하도록 요청한다.
    로직 강제가 아니라 LLM 판단으로, retrieval 결과 중 이 케이스에 적용 가능한 조문만 남긴다.
    """
    if not refs or not settings.openai_api_key:
        return refs
    body = state.get("body_evidence") or {}
    case_type = str(body.get("case_type") or state.get("intended_risk_type") or "").strip()
    merchant = str(body.get("merchantName") or "").strip() or "거래처 미상"
    mcc = str(body.get("mccCode") or body.get("mccName") or "").strip()
    ref_lines = []
    for i, r in enumerate(refs):
        art = str(r.get("article") or r.get("regulation_article") or "").strip()
        title = str(r.get("parent_title") or "").strip()
        ref_lines.append(f"[{i}] {art} {title}")
    ref_block = "\n".join(ref_lines)
    system = (
        "당신은 감사 보고서에 인용할 규정을 선택하는 역할이다. "
        "주어진 케이스 유형·업종(가맹점, MCC)에 실제로 적용 가능한 규정 조문만 골라야 한다. "
        "예: 휴일·식대 건이면 휴일/식대/심야 관련 조문만 채택하고, 차량유지비·차량 관련 조문은 이 케이스에 해당하지 않으면 제외한다. "
        "JSON만 출력: {\"applicable_indices\": [0, 1, ...]} (해당하는 인덱스만 나열). 하나도 해당 없으면 빈 배열."
    )
    user = (
        f"케이스 유형: {case_type or '미분류'}\n가맹점: {merchant}\nMCC/업종: {mcc or '-'}\n\n"
        f"규정 후보:\n{ref_block}\n\n"
        "이 케이스에 적용 가능한 규정의 인덱스만 applicable_indices 배열로 출력하라."
    )
    try:
        from openai import AsyncAzureOpenAI, AsyncOpenAI

        base_url = (getattr(settings, "openai_base_url", None) or "").strip()
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
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **tok_kw,
            ),
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        indices = parsed.get("applicable_indices")
        if isinstance(indices, list) and indices:
            return [refs[i] for i in indices if 0 <= i < len(refs)]
        return refs
    except Exception:
        return refs


async def _llm_decide_hitl_verdict(
    *,
    hitl_request: dict[str, Any],
    hitl_response: dict[str, Any],
    evidence_result: dict[str, Any] | None,
) -> tuple[str, str]:
    """
    담당자 검토(HITL) 응답에 대해 확정 vs 재검토를 룰이 아닌 LLM 판단으로 결정한다.
    검토 필요로 올라온 건에 대해 필요한 답변/근거 없이 승인만 온 경우 재검토가 나와야 정상이지만,
    그 판단은 LLM이 맥락을 보고 하도록 함. 하드코딩/룰베이스 금지.
    반환: (verdict, reason) — verdict는 COMPLETED_AFTER_HITL 또는 REVIEW_REQUIRED.
    """
    logger.info("[VERDICT_LLM] _llm_decide_hitl_verdict 진입 (확정 vs 재검토 LLM 판단)")
    why = str(hitl_request.get("why_hitl") or hitl_request.get("blocking_reason") or "").strip()
    reasons = hitl_request.get("reasons") or hitl_request.get("auto_finalize_blockers") or []
    questions = hitl_request.get("review_questions") or hitl_request.get("questions") or []
    required_inputs = hitl_request.get("required_inputs") or []
    comment = str(hitl_response.get("comment") or "").strip()
    approved = hitl_response.get("approved") is True
    extra = hitl_response.get("extra_facts") or {}
    evidence_passed = None
    if isinstance(evidence_result, dict) and evidence_result:
        evidence_passed = evidence_result.get("passed") is True
        if evidence_passed is False:
            llm_reason = await _llm_summarize_evidence_review_reason(
                evidence_result=evidence_result,
                reviewer_approved=approved,
                reviewer_comment=comment,
            )
            reason = llm_reason or "첨부 증빙 대조 결과에서 불일치가 확인되어 추가 확인이 필요합니다."
            logger.info(
                "[VERDICT_LLM] 증빙 불일치 강제 재검토: approved=%s evidence_passed=%s reasons=%s",
                approved,
                evidence_passed,
                (evidence_result.get("reasons") or [])[:3],
            )
            return ("REVIEW_REQUIRED", reason)

    context = {
        "why_hitl": why,
        "reasons": reasons[:10],
        "review_questions": questions[:3],
        "required_inputs": [r.get("field") for r in required_inputs[:5] if r.get("field")],
        "reviewer_approved": approved,
        "reviewer_comment_length": len(comment),
        "reviewer_comment_preview": (comment[:200] + "…") if len(comment) > 200 else comment,
        "reviewer_extra_facts_keys": list(extra.keys())[:10] if isinstance(extra, dict) else [],
        "evidence_upload_passed": evidence_passed,
        "evidence_reasons": (evidence_result.get("reasons") if isinstance(evidence_result, dict) else []) or [],
    }
    system = (
        "당신은 엔터프라이즈 감사 에이전트의 판단자다. "
        "담당자 검토(HITL)가 필요했던 전표에 대해 담당자가 응답을 제출했다. "
        "이 응답만 보고 '최종 확정(COMPLETED_AFTER_HITL)' vs '재검토 필요(REVIEW_REQUIRED)' 중 하나를 판단하라. "
        "필요한 답변·근거 없이 승인만 한 경우(의견 없음 또는 극히 짧은 무의미한 문구) 정상적으로는 재검토가 나와야 한다. "
        "단, 담당자가 승인하고 검토 의견에 자신의 판단을 명시한 경우(예: 문제 없음, 특이 사항, 승인 가능, 이번 건 괜찮음 등)에는 "
        "질문 문장과 완전히 같은 표현이 없어도 '질문에 대한 답변으로 해석 가능한 판단'으로 보아 COMPLETED_AFTER_HITL을 우선 고려하라. "
        "규정·업무 맥락상 담당자 판단을 존중하는 방향으로 맥락을 보고 판단하라. "
        "룰이나 키워드로만 치지 말고, 검토 요청 사유·질문과 담당자 응답 내용을 비교해 판단하라. "
        "JSON만 출력한다. 키: verdict(반드시 COMPLETED_AFTER_HITL 또는 REVIEW_REQUIRED), reason(한 문장 이유, 한국어)."
    )
    user = (
        "[검토 요청 맥락]\n"
        f"검토 필요 사유: {why or '(없음)'}\n"
        f"요청 사유 목록: {json.dumps(reasons, ensure_ascii=False)}\n"
        f"검토 시 확인할 질문: {json.dumps(questions, ensure_ascii=False)}\n"
        f"필수 입력 항목: {context['required_inputs']}\n\n"
        "[담당자 응답]\n"
        f"승인 여부: {approved}\n"
        f"검토 의견 길이: {len(comment)}자\n"
        f"의견 미리보기: {context['reviewer_comment_preview'] or '(없음)'}\n"
        f"추가 사실 키: {context['reviewer_extra_facts_keys']}\n"
        f"증빙 업로드 검증 통과: {evidence_passed}\n\n"
        f"증빙 비교 사유: {json.dumps(context['evidence_reasons'], ensure_ascii=False)}\n\n"
        "위 맥락을 바탕으로 verdict와 reason을 JSON으로 출력하라."
    )
    logger.info(
        "[VERDICT_LLM] 호출 직전 approved=%s comment_len=%s comment_preview=%s",
        approved,
        len(comment),
        (comment[:80] + "…") if len(comment) > 80 else (comment or "(없음)"),
    )
    raw = ""
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
        completion_tokens_kw = {"max_completion_tokens": 5000} if is_azure else {"max_tokens": 5000}
        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **completion_tokens_kw,
            ),
        )
        choice = response.choices[0] if response.choices else None
        raw = (choice.message.content if choice and choice.message else None) or ""
        raw = raw.strip()
        usage = getattr(response, "usage", None)
        logger.info(
            "[VERDICT_LLM] 응답 수신 content_len=%s finish_reason=%s usage=%s",
            len(raw),
            getattr(choice, "finish_reason", None) if choice else None,
            (f"prompt={getattr(usage, 'prompt_tokens', None)} completion={getattr(usage, 'completion_tokens', None)}") if usage else None,
        )
        if not raw and choice:
            finish = getattr(choice, "finish_reason", None)
            refusal = getattr(choice.message, "refusal", None) if choice.message else None
            finish_details = getattr(choice, "finish_details", None)
            logger.warning(
                "[VERDICT_LLM] 응답 본문 없음 — finish_reason=%s refusal=%s finish_details=%s (content_filter/length 등 원인 확인용)",
                finish,
                refusal,
                finish_details,
            )
            if approved:
                logger.info(
                    "[VERDICT_LLM] fallback: 응답 비어 있음, approved=True → COMPLETED_AFTER_HITL (답변 길이 제한 없음)",
                )
                return ("COMPLETED_AFTER_HITL", "담당자 승인 및 검토 의견 반영으로 확정(LLM 판단 보조 실패 시 적용).")
            logger.warning(
                "[VERDICT_LLM] fallback: 응답 비어 있음, approved=False → REVIEW_REQUIRED",
            )
            return ("REVIEW_REQUIRED", "판단 LLM 응답이 비어 있어 재검토 필요 처리.")
        parsed = json.loads(raw)
        v = str(parsed.get("verdict") or "").upper()
        if v not in ("COMPLETED_AFTER_HITL", "REVIEW_REQUIRED"):
            v = "REVIEW_REQUIRED"
        reason = str(parsed.get("reason") or "").strip() or "담당자 검토 응답을 기준으로 판단함."
        logger.info(
            "[VERDICT_LLM] LLM 정상 응답 (검토 필요 여부는 LLM 판단) verdict=%s reason=%s raw_preview=%s",
            v,
            (reason[:80] + "…") if len(reason) > 80 else reason,
            (raw[:200] + "…") if len(raw) > 200 else raw,
        )
        return (v, reason)
    except Exception as e:
        raw_preview = (raw[:300] + "…") if len(raw) > 300 else (raw or "(empty or not available)")
        if approved:
            logger.info(
                "[VERDICT_LLM] fallback: LLM 호출 실패 (%s) approved=True → COMPLETED_AFTER_HITL (답변 길이 제한 없음)",
                type(e).__name__,
            )
            return ("COMPLETED_AFTER_HITL", "담당자 승인 및 검토 의견 반영으로 확정(LLM 판단 실패 시 적용).")
        logger.warning(
            "[VERDICT_LLM] fallback: LLM 호출 실패 (%s) approved=False → REVIEW_REQUIRED. raw_preview=%s",
            type(e).__name__,
            raw_preview,
        )
        return ("REVIEW_REQUIRED", "판단 LLM 호출 실패로 재검토 필요 처리.")


async def _llm_summarize_hold_reason(hitl_response: dict[str, Any]) -> str:
    """
    담당자가 보류를 선택했을 때, HITL 의견(comment 등)을 LLM이 한 문장으로 요약해 판단요약에 쓴다.
    하드코딩 '담당자 사유: {comment}' 대신 LLM 해석 문장을 적용.
    """
    comment = str(hitl_response.get("comment") or "").strip()
    business_purpose = str(hitl_response.get("business_purpose") or "").strip()
    attendees = hitl_response.get("attendees")
    if isinstance(attendees, list):
        attendees_str = ", ".join(str(x) for x in attendees[:5] if x)
    else:
        attendees_str = str(attendees or "").strip()
    parts = [p for p in [comment, business_purpose, attendees_str] if p]
    if not parts:
        return ""
    text = "\n".join(f"- {p[:300]}" for p in parts)
    system = (
        "당신은 감사 판단요약 문구를 작성하는 보조자다. "
        "담당자가 검토 보류를 선택했고, 아래와 같은 의견/사유를 제출했다. "
        "이 내용을 '담당자 검토 결과 보류합니다.' 뒤에 붙일 한 문장(한국어, 80자 내외)으로 요약하라. "
        "원문을 그대로 나열하지 말고, 요지만 정리한 한 문장으로 출력하라. 다른 설명 없이 그 한 문장만 출력하라."
    )
    user = f"[담당자 제출 내용]\n{text}"
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
        completion_tokens_kw = {"max_completion_tokens": 256} if is_azure else {"max_tokens": 256}
        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=settings.reasoning_llm_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **completion_tokens_kw,
            )
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw:
            first = raw.split("\n")[0].strip()
            if len(first) > 300:
                first = first[:297] + "…"
            logger.info("[VERDICT_LLM] HOLD 사유 LLM 요약: %s", first[:100] + "…" if len(first) > 100 else first)
            return first
    except Exception as e:
        logger.warning("[VERDICT_LLM] HOLD 사유 LLM 요약 실패: %s", type(e).__name__)
    return ""


async def _llm_summarize_evidence_review_reason(
    *,
    evidence_result: dict[str, Any],
    reviewer_approved: bool,
    reviewer_comment: str,
) -> str:
    """증빙 비교 실패 사유를 REVIEW_REQUIRED용 한 문장으로 요약."""
    reasons = evidence_result.get("reasons") or []
    mismatches = evidence_result.get("mismatches") or []
    detail = evidence_result.get("comparison_detail") or {}
    context = {
        "reasons": reasons[:5],
        "mismatches": mismatches[:5],
        "comparison_detail": detail,
        "reviewer_choice": "approve" if reviewer_approved else "hold",
        "reviewer_comment": (reviewer_comment or "")[:300],
    }
    system = (
        "당신은 감사 결과 요약자다. "
        "증빙-전표 비교 결과를 바탕으로 왜 추가 확인이 필요한지 한국어 한 문장(90자 내외)으로 작성하라. "
        "문장에는 반드시 담당자가 승인/보류 중 어떤 선택으로 제출했는지 맥락을 자연스럽게 포함하라. "
        "예: '담당자는 승인으로 제출했으나 ...', '담당자는 보류로 제출했고 ...'. "
        "판정 단어(REVIEW_REQUIRED/HOLD/승인)를 직접 쓰지 말고, 사실 중심으로 작성하라. "
        "문장 1개만 출력한다."
    )
    user = f"[증빙 비교 결과]\n{json.dumps(context, ensure_ascii=False)}"
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
        completion_tokens_kw = {"max_completion_tokens": 220} if is_azure else {"max_tokens": 220}
        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=settings.reasoning_llm_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **completion_tokens_kw,
            )
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw:
            first = raw.split("\n")[0].strip()
            if len(first) > 280:
                first = first[:277] + "…"
            return first
    except Exception as e:
        logger.warning("[VERDICT_LLM] evidence review reason 요약 실패: %s", type(e).__name__)
    return ""


async def _llm_summarize_completed_reason(state: dict[str, Any]) -> str:
    """
    자동 확정(COMPLETED)인 경우, 수집된 증거·규정·검증 결과를 바탕으로
    '왜 확정인지' 한 문장을 LLM이 생성해 판단요약 꼬리에 쓴다.
    하드코딩 '현재 수집된 증거 기준으로 자동 확정되었습니다.' 대신 증거 기반 사유를 노출.
    """
    body = state.get("body_evidence") or {}
    tool_results = state.get("tool_results") or []
    verification = state.get("verification") or {}
    reporter_output = state.get("reporter_output") or {}
    score = _score_with_hitl_adjustment(state.get("score_breakdown") or {}, state.get("flags") or {})

    voucher_line = _voucher_summary_for_context(body)
    case_type = str(body.get("case_type") or state.get("intended_risk_type") or "").strip() or "미분류"

    refs = _top_policy_refs(tool_results, limit=5)
    ref_labels = []
    for r in refs:
        art = (r.get("article") or r.get("regulation_article") or "").strip()
        title = (r.get("parent_title") or "").strip()
        ref_labels.append(f"{art} ({title})" if title else art)
    policy_line = "규정: " + ", ".join(ref_labels) if ref_labels else "규정: (없음)"

    doc_result = _find_tool_result(tool_results, "document_evidence_probe")
    line_count = int(((doc_result or {}).get("facts") or {}).get("lineItemCount") or 0)
    policy_result = _find_tool_result(tool_results, "policy_rulebook_probe")
    ref_count = int(((policy_result or {}).get("facts") or {}).get("ref_count") or 0)
    evidence_line = f"규정 조항 {ref_count}건, 전표 라인 {line_count}건 확보."

    holiday_result = _find_tool_result(tool_results, "holiday_compliance_probe")
    merchant_result = _find_tool_result(tool_results, "merchant_risk_probe")
    risk_parts = []
    if holiday_result:
        h_risk = (holiday_result.get("facts") or {}).get("holidayRisk")
        if h_risk is not True:
            risk_parts.append("휴일 위험 없음")
    if merchant_result:
        m_risk = str((merchant_result.get("facts") or {}).get("merchantRisk") or "UNKNOWN").upper()
        if m_risk in ("LOW", "UNKNOWN"):
            risk_parts.append(f"가맹점 위험도 {m_risk}")
    risk_line = " ".join(risk_parts) if risk_parts else ""

    verifier_output = state.get("verifier_output") or {}
    gate = str(getattr(verifier_output.get("gate"), "value", verifier_output.get("gate")) or "").upper()
    if gate.startswith("VERIFIERGATE."):
        gate = gate.split(".", 1)[1]
    quality = verification.get("quality_signals") or []
    quality_line = f"검증 게이트: {gate}, 품질: {quality}" if gate or quality else ""

    summary = (reporter_output.get("summary") or "").strip()
    context_parts = [
        f"[전표] {voucher_line}",
        f"[케이스유형] {case_type}",
        policy_line,
        evidence_line,
        f"[점수] 정책 {score['policy_score']}점, 근거 {score['evidence_score']}점",
    ]
    if risk_line:
        context_parts.append(f"[위험점검] {risk_line}")
    if quality_line:
        context_parts.append(quality_line)
    if summary:
        context_parts.append(f"[보고요약] {summary[:300]}")
    text = "\n".join(context_parts)

    system = (
        "당신은 감사 판단요약 문구를 작성하는 보조자다. "
        "아래 분석 결과는 이미 '자동 확정(COMPLETED)'으로 판단된 전표이다. "
        "수집된 증거·규정 근거·검증 결과를 바탕으로, 사용자가 '왜 확정인지' 이해할 수 있게 한 문장(한국어, 100자 내외)으로 요약하라. "
        "예: 평일 업무 시간대 일반 식대 사용, 규정 위험 없음, 증빙·규정 조항 충족 등 수집된 증거상 위반 소지가 없어 자동 확정하였습니다. "
        "다른 설명 없이 그 한 문장만 출력하라."
    )
    user = f"[분석 결과]\n{text}"
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
        completion_tokens_kw = {"max_completion_tokens": 256} if is_azure else {"max_tokens": 256}
        response = await client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=settings.reasoning_llm_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **completion_tokens_kw,
            )
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw:
            first = raw.split("\n")[0].strip()
            if len(first) > 350:
                first = first[:347] + "…"
            logger.info("[VERDICT_LLM] COMPLETED 확정 사유 LLM 요약: %s", first[:100] + "…" if len(first) > 100 else first)
            return first
    except Exception as e:
        logger.warning("[VERDICT_LLM] COMPLETED 확정 사유 LLM 요약 실패: %s", type(e).__name__)
    return ""


def _build_grounded_reason(state: dict[str, Any], completed_tail: str | None = None) -> tuple[str, str]:
    score = _score_with_hitl_adjustment(state["score_breakdown"], state["flags"])
    hitl_request = state.get("hitl_request")
    body = state["body_evidence"]
    prior_hitl_request = (body.get("hitlRequest") or {}) if isinstance(body.get("hitlRequest"), dict) else {}
    occurred_at = _format_occurred_at(body.get("occurredAt"))
    merchant = body.get("merchantName") or "거래처 미상"
    refs = _top_policy_refs(state.get("tool_results", []), limit=5)
    _article_in_merged_re = re.compile(r"제\s*\d+\s*조")
    articles_in_merged: set[str] = set()
    for ref in refs:
        pt = (ref.get("parent_title") or "").strip()
        if " ~ " in pt:
            for m in _article_in_merged_re.finditer(pt):
                articles_in_merged.add(m.group(0).strip())
    ref_labels: list[str] = []
    for ref in refs:
        article = ref.get("article") or ref.get("regulation_article") or "조항 미상"
        parent_title = (ref.get("parent_title") or "").strip()
        art_stripped = (article or "").strip()
        if art_stripped and art_stripped in articles_in_merged and " ~ " not in parent_title:
            continue
        if not parent_title:
            label = article
        else:
            if art_stripped and (
                parent_title == art_stripped
                or parent_title.startswith(art_stripped + " ")
                or parent_title.startswith(art_stripped + "(")
            ):
                label = parent_title
            else:
                label = f"{article} ({parent_title})"
        ref_labels.append(label)

    intro = f"전표는 {occurred_at} 시점 {merchant} 사용 건으로 분석되었습니다. "
    score_text = f"정책점수 {score['policy_score']}점, 근거점수 {score['evidence_score']}점이 반영되었습니다. "
    if ref_labels:
        grounding = f"관련 규정 근거는 {', '.join(ref_labels)}입니다. "
    else:
        grounding = "현재 직접 연결된 규정 근거는 제한적입니다. "

    if hitl_request:
        status = "HITL_REQUIRED"
        tail = "현재는 담당자 검토가 필요한 상태로 분류되었으며, 추가 소명 확인 후 최종 확정이 필요합니다."
    else:
        if state["flags"].get("hasHitlResponse"):
            if state["flags"].get("hitlApproved") is True:
                reporter_verdict = (state.get("reporter_output") or {}).get("verdict")
                hitl_reason = (state.get("reporter_output") or {}).get("hitl_verdict_reason") or ""
                if reporter_verdict in ("COMPLETED_AFTER_HITL", "REVIEW_REQUIRED"):
                    status = reporter_verdict
                    tail = hitl_reason if hitl_reason else (
                        "담당자 검토 결과 승인 가능으로 확인되어 최종 판단을 확정했습니다." if status == "COMPLETED_AFTER_HITL" else "담당자 검토 기준 미충족으로 재검토가 필요합니다."
                    )
                    if status == "REVIEW_REQUIRED":
                        logger.info("[FINAL_STATUS] status=REVIEW_REQUIRED (출처: verdict LLM 반환 — reporter_verdict=REVIEW_REQUIRED)")
                else:
                    status = "REVIEW_REQUIRED"
                    tail = hitl_reason if hitl_reason else "담당자 검토 기준 미충족으로 재검토가 필요합니다."
                    logger.info("[FINAL_STATUS] status=REVIEW_REQUIRED (출처: hasHitlResponse=True이나 reporter_verdict 미사용, finalizer fallback)")
            else:
                status = "HOLD_AFTER_HITL"
                hitl_reason = (state.get("reporter_output") or {}).get("hitl_verdict_reason") or ""
                if hitl_reason:
                    cleaned = _strip_hold_prefix(hitl_reason)
                    tail = f"담당자 검토 결과 보류합니다. {cleaned}" if cleaned else "담당자 검토 결과 보류합니다."
                else:
                    hitl_response = (body.get("hitlResponse") or {}) if isinstance(body.get("hitlResponse"), dict) else {}
                    hold_reason = (hitl_response.get("comment") or hitl_response.get("business_purpose") or "").strip()
                    if hold_reason:
                        tail = f"담당자 검토 결과 보류합니다. 담당자 사유: {hold_reason[:400]}{'…' if len(hold_reason) > 400 else ''}"
                    else:
                        tail = "담당자 검토 결과 보류/추가 검토가 필요해 자동 확정을 중단합니다."
        else:
            reporter_verdict = str((state.get("reporter_output") or {}).get("verdict") or "").upper()
            verify_ready = _is_verify_ready_without_hitl(state)
            if verify_ready and reporter_verdict in {"", "READY", "COMPLETED", "AUTO_APPROVED"}:
                status = "COMPLETED"
                tail = (completed_tail or "현재 수집된 증거 기준으로 자동 확정되었습니다.").strip() or "현재 수집된 증거 기준으로 자동 확정되었습니다."
            else:
                status = "REVIEW_REQUIRED"
                tail = "현재 수집된 증거 기준으로 우선 검토 대상입니다."
                logger.info(
                    "[FINAL_STATUS] status=REVIEW_REQUIRED (출처: finalizer 룰 — verify_ready=%s reporter_verdict=%s, verdict LLM 미호출)",
                    verify_ready,
                    reporter_verdict,
                )
    return intro + grounding + score_text + tail, status
