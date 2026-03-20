from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
MAX_HITL_QUESTIONS = 2
MAX_HITL_INPUTS = 2  # 필수 입력 항목 최대 수 (UI 과밀 방지 + 검토 집중도 향상)


_CASE_TYPE_LABELS = {
    "HOLIDAY_USAGE": "휴일 사용 의심",
    "LIMIT_EXCEED": "한도 초과 의심",
    "PRIVATE_USE_RISK": "사적 사용 위험",
    "UNUSUAL_PATTERN": "비정상 패턴",
    "NORMAL_BASELINE": "정상 범위",
    "UNSCREENED": "미분류",
}

_SEVERITY_LABELS = {
    "LOW": "낮음",
    "MEDIUM": "중간",
    "HIGH": "높음",
    "CRITICAL": "심각",
}

_HR_STATUS_LABELS = {
    "WORKING": "근무",
    "WORK": "근무",
    "VACATION": "휴가",
    "LEAVE": "휴가/결근",
    "OFF": "휴무",
    "BUSINESS_TRIP": "출장",
}


def _label_case_type(value: Any) -> str:
    return _CASE_TYPE_LABELS.get(str(value or "").upper(), str(value or "-"))


def _label_severity(value: Any) -> str:
    return _SEVERITY_LABELS.get(str(value or "").upper(), str(value or "-"))


def _label_hr_status(value: Any) -> str:
    return _HR_STATUS_LABELS.get(str(value or "").upper(), str(value or "-"))


def _extract_policy_refs(tool_results: list[dict[str, Any]], *, limit: int = 4) -> list[dict[str, Any]]:
    for result in tool_results:
        if (result.get("tool") or result.get("skill")) != "policy_rulebook_probe":
            continue
        facts = result.get("facts") or {}
        refs = facts.get("policy_refs") or facts.get("retrieval_candidates") or []
        return list(refs[:limit])
    return []


def _extract_document_facts(tool_results: list[dict[str, Any]]) -> dict[str, Any]:
    for result in tool_results:
        if (result.get("tool") or result.get("skill")) == "document_evidence_probe":
            return result.get("facts") or {}
    return {}


# 자동 확정 미달 조건(하나라도 해당 시 HITL 발생):
# 1. 검증 게이트: gate_policy (hold/caution/regenerate_citations 등)
# 2. 근거 연결률 100% 미만: verification_summary covered/total, coverage_ratio < 1.0
# 3. 비판 검토 보류 권고: critique.recommend_hold
# 4. 규정 위반 유형 + 고심각도: case_type in HOLIDAY_USAGE/LIMIT_EXCEED/…, severity in MEDIUM/HIGH/CRITICAL
# 5. 전표 필드 누락: missing_fields
# 6. 전표 라인 없음: document_items 비어 있음
# 7. 규정 기반 필수 입력/질문: regulation_driven required_inputs 또는 review_questions
def build_hitl_request(
    body_evidence: dict[str, Any],
    tool_results: list[dict[str, Any]],
    *,
    critique: dict[str, Any] | None = None,
    verification_summary: dict[str, Any] | None = None,
    screening_result: dict[str, Any] | None = None,
    score_breakdown: dict[str, Any] | None = None,
    regulation_driven: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    critique = critique or {}
    verification_summary = verification_summary or {}
    screening_result = screening_result or {}
    score_breakdown = score_breakdown or {}

    missing_fields = ((body_evidence.get("dataQuality") or {}).get("missingFields") or [])
    document = body_evidence.get("document") or {}
    document_items = document.get("items") or []
    policy_refs = _extract_policy_refs(tool_results)
    document_facts = _extract_document_facts(tool_results)

    case_type = screening_result.get("case_type") or screening_result.get("caseType") or body_evidence.get("case_type")
    severity = screening_result.get("severity") or body_evidence.get("severity")
    logger.debug(
        "[HITL_BUILD] case_type source: screening_result=%s body_evidence=%s → %s",
        screening_result.get("case_type") or screening_result.get("caseType"),
        body_evidence.get("case_type"),
        case_type,
    )
    score = screening_result.get("score")
    if score is None:
        score = score_breakdown.get("final_score")

    unresolved_claims: list[str] = []
    blocking_reasons: list[str] = []
    missing_evidence: list[str] = []
    review_questions: list[str] = []
    required_inputs: list[dict[str, str]] = []
    evidence_snapshot: list[dict[str, str]] = []

    if screening_result.get("reasonText"):
        unresolved_claims.append(str(screening_result["reasonText"]))

    gate_policy = verification_summary.get("gate_policy")
    if gate_policy:
        blocking_reasons.append(f"검증 게이트가 {gate_policy} 상태로 판정되었습니다.")

    covered = verification_summary.get("covered")
    total = verification_summary.get("total")
    if total:
        ratio = verification_summary.get("coverage_ratio")
        # 연결률이 100% 미만일 때만 "미달" 사유로 넣음 (100%면 자동 확정 기준 충족)
        if ratio is not None and ratio < 1.0:
            ratio_text = f"{(ratio or 0) * 100:.0f}%"
            blocking_reasons.append(f"근거 연결률이 {covered}/{total} ({ratio_text})로 자동 확정 기준에 미달했습니다.")

    missing_citations = verification_summary.get("missing_citations") or []
    for sentence in missing_citations[:3]:
        unresolved_claims.append(f"미검증 주장: {sentence}")

    if critique.get("recommend_hold"):
        rationale = critique.get("rationale")
        blocking_reasons.append(str(rationale or "비판적 검토에서 추가 확인이 필요하다고 판단되었습니다."))

    if critique.get("missing_counter_evidence"):
        for item in critique.get("missing_counter_evidence", [])[:5]:
            missing_evidence.append(str(item))

    # 규정 위반·심각도 높은 케이스는 담당자 검토(HITL) 필요. 증빙 업로드가 아닌 통과/미통과 판단이 목적.
    _POLICY_RISK_TYPES = {"HOLIDAY_USAGE", "LIMIT_EXCEED", "PRIVATE_USE_RISK", "UNUSUAL_PATTERN"}
    _HIGH_SEVERITY = {"MEDIUM", "HIGH", "CRITICAL"}
    if case_type and str(case_type).upper() in _POLICY_RISK_TYPES:
        sev = str(severity or "").upper()
        if sev in _HIGH_SEVERITY:
            blocking_reasons.append("규정 위반 가능성이 있어 담당자 검토(통과/미통과 판단)가 필요합니다.")

    if missing_fields:
        missing_evidence.extend([f"입력 필드 누락: {field}" for field in missing_fields])
        required_inputs.append({"field": "missing_fields", "reason": "누락된 전표 입력값 보완이 필요합니다."})

    if not document_items:
        missing_evidence.append("전표 라인아이템이 없어 거래 상세 증빙 확인이 불가합니다.")
        required_inputs.append({"field": "document_items", "reason": "라인아이템 또는 적요 등 거래 상세 근거가 필요합니다."})

    # 규정 기반 동적 HITL: 에이전트가 규정 본문에서 추출한 필수 입력/검토 질문을 사용. 하드코딩 규칙 대체.
    reg = regulation_driven or {}
    reg_required = reg.get("required_inputs") or []
    reg_questions = reg.get("review_questions") or []
    if reg_required or reg_questions:
        required_inputs.extend(reg_required)
        review_questions.extend(reg_questions)
    # review_questions는 규정 기반(regulation_driven) 또는 verify_node에서 LLM이 생성한 값만 사용. 하드코딩/폴백 없음.

    is_normal_baseline = str(case_type or "").upper() == "NORMAL_BASELINE"
    critical_required = [r for r in required_inputs if (r.get("field") or "").strip() in ("missing_fields", "document_items")]
    logger.debug(
        "[HITL_BUILD] case_type=%s is_normal_baseline=%s len(missing_evidence)=%s len(required_inputs)=%s len(critical_required)=%s len(blocking_reasons)=%s reg_required=%s",
        case_type,
        is_normal_baseline,
        len(missing_evidence),
        len(required_inputs),
        len(critical_required),
        len(blocking_reasons),
        bool(reg_required),
    )
    if is_normal_baseline and not missing_evidence and not critical_required:
        logger.info("[HITL_BUILD] NORMAL_BASELINE → HITL skip (no missing_evidence, no critical required_inputs; reg_required ignored for 정상 케이스)")
        return None
    if is_normal_baseline and (missing_evidence or critical_required):
        logger.info(
            "[HITL_BUILD] NORMAL_BASELINE but HITL required: missing_evidence=%s critical_fields=%s",
            [m[:60] for m in missing_evidence[:3]],
            [r.get("field") for r in critical_required[:5]],
        )

    if case_type:
        evidence_snapshot.append({"type": "risk", "label": "위험 유형", "value": _label_case_type(case_type)})
    if severity:
        evidence_snapshot.append({"type": "severity", "label": "심각도", "value": _label_severity(severity)})
    if score is not None:
        evidence_snapshot.append({"type": "score", "label": "현재 점수", "value": str(score)})
    if body_evidence.get("merchantName"):
        evidence_snapshot.append({"type": "merchant", "label": "가맹점", "value": str(body_evidence["merchantName"])})
    if body_evidence.get("occurredAt"):
        evidence_snapshot.append({"type": "datetime", "label": "발생 시각", "value": str(body_evidence["occurredAt"])})
    if body_evidence.get("hrStatus"):
        evidence_snapshot.append({"type": "hr_status", "label": "근태 상태", "value": _label_hr_status(body_evidence.get("hrStatus"))})
    if document_facts.get("lineItemCount") is not None:
        evidence_snapshot.append({"type": "doc_fact", "label": "전표 라인", "value": f"{document_facts.get('lineItemCount')}건"})
    for ref in policy_refs[:3]:
        article = ref.get("article") or "-"
        parent_title = ref.get("parent_title") or ref.get("title") or "-"
        evidence_snapshot.append({"type": "policy_ref", "label": "연결 규정", "value": f"{article} / {parent_title}"})

    # 검토 필요(REVIEW_REQUIRED)인데 HITL 요청이 없으면 UI에 담당자 검토 패널이 안 뜸.
    # 규정 적용·위험 유형이 있으면 최소한의 hitl_request를 만들어 run이 hitl_pause에서 멈추도록 함.
    if not blocking_reasons and not unresolved_claims and not missing_evidence:
        is_normal_baseline = str(case_type or "").upper() == "NORMAL_BASELINE"
        risk_case_or_reg = (case_type and not is_normal_baseline) or severity or reg_required or reg_questions
        if policy_refs and risk_case_or_reg:
            blocking_reasons.append("규정 적용 건으로 담당자 검토(통과/미통과 판단)가 필요합니다.")
        else:
            return None

    why_parts = []
    if blocking_reasons:
        why_parts.append(blocking_reasons[0])
    if missing_evidence:
        why_parts.append(missing_evidence[0])
    if unresolved_claims:
        why_parts.append(unresolved_claims[0])

    logger.info(
        "[HITL_BUILD] building hitl_request: case_type=%s why_preview=%s",
        case_type,
        (why_parts[0][:80] + "…") if why_parts else "",
    )
    # 질문은 최대 2개로 제한 (UI 과밀 방지 + 검토 집중도 향상)
    review_questions = [str(q).strip() for q in (review_questions or []) if str(q).strip()][:MAX_HITL_QUESTIONS]
    # 필수 입력 항목도 최대 2개로 제한 — 중요도 높은 순으로 앞에 위치한다고 가정
    required_inputs = required_inputs[:MAX_HITL_INPUTS]

    return {
        "required": True,
        "handoff": "FINANCE_REVIEWER",
        "why_hitl": " ".join(why_parts) if why_parts else "자동 확정 근거가 충분하지 않아 담당자 검토가 필요합니다.",
        "blocking_gate": str(gate_policy or "HITL_REQUIRED"),
        "blocking_reason": blocking_reasons[0] if blocking_reasons else "자동 확정을 중단한 상세 사유를 검토해 주세요.",
        "reasons": blocking_reasons or ["담당자 검토가 필요한 상태입니다."],
        "auto_finalize_blockers": blocking_reasons or ["자동 확정을 중단한 근거가 있습니다."],
        "unresolved_claims": unresolved_claims,
        "missing_evidence": missing_evidence,
        "review_questions": review_questions,
        "questions": review_questions,
        "required_inputs": required_inputs,
        "evidence_snapshot": evidence_snapshot,
        "candidate_outcomes": ["APPROVE_AFTER_HITL", "HOLD_AFTER_HITL"],
        "source_summary": {
            "case_type": case_type,
            "severity": severity,
            "score": score,
            "policy_ref_count": len(policy_refs),
            "document_line_count": document_facts.get("lineItemCount"),
        },
    }
