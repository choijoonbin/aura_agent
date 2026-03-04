from __future__ import annotations

from typing import Any


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
        if result.get("skill") != "policy_rulebook_probe":
            continue
        facts = result.get("facts") or {}
        refs = facts.get("policy_refs") or facts.get("retrieval_candidates") or []
        return list(refs[:limit])
    return []


def _extract_document_facts(tool_results: list[dict[str, Any]]) -> dict[str, Any]:
    for result in tool_results:
        if result.get("skill") == "document_evidence_probe":
            return result.get("facts") or {}
    return {}


def build_hitl_request(
    body_evidence: dict[str, Any],
    tool_results: list[dict[str, Any]],
    *,
    critique: dict[str, Any] | None = None,
    verification_summary: dict[str, Any] | None = None,
    screening_result: dict[str, Any] | None = None,
    score_breakdown: dict[str, Any] | None = None,
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

    if missing_fields:
        missing_evidence.extend([f"입력 필드 누락: {field}" for field in missing_fields])
        required_inputs.append({"field": "missing_fields", "reason": "누락된 전표 입력값 보완이 필요합니다."})

    if not document_items:
        missing_evidence.append("전표 라인아이템이 없어 거래 상세 증빙 확인이 불가합니다.")
        required_inputs.append({"field": "document_items", "reason": "라인아이템 또는 적요 등 거래 상세 근거가 필요합니다."})

    if body_evidence.get("isHoliday"):
        required_inputs.append({"field": "business_purpose", "reason": "휴일 사용의 업무상 필요성 판단이 필요합니다."})
        required_inputs.append({"field": "attendees", "reason": "식대/접대비 성격이면 참석자 정보 확인이 필요합니다."})
        review_questions.append("휴일 사용이 업무상 예외 사유에 해당하는지 확인해 주세요.")
    if body_evidence.get("hrStatus") in {"VACATION", "LEAVE", "OFF"}:
        review_questions.append(f"근태 상태가 {_label_hr_status(body_evidence.get('hrStatus'))}인 시점의 거래가 정당한지 확인해 주세요.")
    if body_evidence.get("budgetExceeded"):
        review_questions.append("예산 초과 사유와 승인 여부를 확인해 주세요.")

    if not review_questions:
        review_questions.extend(
            [
                "자동 확정을 막은 핵심 사유가 해소되었는지 검토해 주세요.",
                "추가 증빙 없이 최종 확정 가능한지 판단해 주세요.",
            ]
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

    if not blocking_reasons and not unresolved_claims and not missing_evidence:
        return None

    why_parts = []
    if blocking_reasons:
        why_parts.append(blocking_reasons[0])
    if missing_evidence:
        why_parts.append(missing_evidence[0])
    if unresolved_claims:
        why_parts.append(unresolved_claims[0])

    return {
        "required": True,
        "handoff": "FINANCE_REVIEWER",
        "why_hitl": " ".join(why_parts) if why_parts else "자동 확정 근거가 충분하지 않아 사람 검토가 필요합니다.",
        "blocking_gate": str(gate_policy or "HITL_REQUIRED"),
        "blocking_reason": blocking_reasons[0] if blocking_reasons else "자동 확정을 중단한 상세 사유를 검토해 주세요.",
        "reasons": blocking_reasons or ["사람 검토가 필요한 상태입니다."],
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
