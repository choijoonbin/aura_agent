from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

from agent.event_schema import AgentEvent
from agent.output_models import (
    ClaimVerificationResult,
    CriticOutput,
    UnsupportedClaimIssue,
    VerifierGate,
    VerifierOutput,
)

logger = logging.getLogger(__name__)
_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")
_ARTICLE_RE = re.compile(r"제\s*(\d+)\s*조")
_VISUAL_MIN_CONFIDENCE = 0.6  # 이 임계값 미만 엔티티는 비교 생략
MAX_HITL_QUESTIONS = 2

_UNSUPPORTED_TAXONOMY_BLOCKING = {
    "no_citation",
    "contradictory_evidence",
}

_EXCLUDED_REQUIRED_INPUT_KEYWORDS = (
    "공통 증빙 의무",
    "모든 경비 지출은",
    "증빙을 구비",
    "법적·내부 기준",
    "증빙",
    "거래일시",
    "거래의 일시",
    "거래의일시",
    "거래 일시",
    "거래의 일시를",
    "거래일시를",
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


def _parse_amount(text: Any) -> float | None:
    """금액 문자열을 float으로 파싱. 콤마·₩·원·공백 제거."""
    if text is None:
        return None
    try:
        cleaned = re.sub(r"[,₩원\s]", "", str(text))
        return float(cleaned) if cleaned else None
    except (ValueError, TypeError):
        return None


def _normalize_merchant(text: Any) -> str:
    """가맹점명 정규화: 공백 제거 + 소문자."""
    return re.sub(r"\s+", "", str(text or "")).lower()


def _extract_date_part(text: Any) -> str:
    """날짜 문자열에서 YYYY-MM-DD 부분만 추출."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(text or ""))
    return m.group(0) if m else ""


def _check_visual_consistency(
    body_evidence: dict[str, Any],
    visual_audit_results: list[dict[str, Any]],
) -> tuple[int, list["UnsupportedClaimIssue"]]:
    """
    body_evidence 텍스트 필드와 이미지에서 추출된 엔티티를 교차 비교.

    비교 항목:
      - amount_total  vs body_evidence["amount"]     → 5% 초과 편차 시 critical mismatch
      - merchant_name vs body_evidence["merchantName"] → 상호명이 서로 포함되지 않으면 mismatch
      - date_occurrence vs body_evidence["occurredAt"]  → 날짜 부분 불일치 시 mismatch

    신뢰도(confidence) < _VISUAL_MIN_CONFIDENCE 인 엔티티는 비교를 건너뜁니다.

    Returns:
        (visual_consistency_score: int 0~100, issues: list[UnsupportedClaimIssue])
        score=100: 이상 없음 / 50: 부분 불일치 / 0: 금액 불일치(critical)
    """
    if not visual_audit_results:
        return 100, []

    # label 별 첫 번째 엔티티 선택 (confidence 기준 정렬 후)
    by_label: dict[str, dict[str, Any]] = {}
    for ent in visual_audit_results:
        label = str(ent.get("label") or "")
        if label not in by_label or float(ent.get("confidence") or 0) > float(by_label[label].get("confidence") or 0):
            by_label[label] = ent

    issues: list[UnsupportedClaimIssue] = []
    score = 100
    amount_mismatch = False

    # ── 금액 비교 ──────────────────────────────────────────────────────────────
    ent_amount = by_label.get("amount_total")
    if ent_amount and float(ent_amount.get("confidence") or 0) >= _VISUAL_MIN_CONFIDENCE:
        extracted = _parse_amount(ent_amount.get("text"))
        body_amt = _parse_amount(body_evidence.get("amount"))
        if extracted is not None and body_amt is not None and extracted > 0:
            ratio = abs(extracted - body_amt) / extracted
            if ratio > 0.05:
                amount_mismatch = True
                issues.append(
                    UnsupportedClaimIssue(
                        claim=f"금액 불일치: 이미지 추출={ent_amount.get('text')} / 입력값={body_evidence.get('amount')}",
                        taxonomy="contradictory_evidence",
                        reason=(
                            f"이미지에서 추출한 금액({ent_amount.get('text')})과 "
                            f"입력 금액({body_evidence.get('amount')})의 편차가 "
                            f"{ratio*100:.1f}%로 허용 범위(5%)를 초과합니다."
                        ),
                        severity="HIGH",
                        covered=False,
                        citation_count=0,
                        supporting_articles=[],
                    )
                )

    # ── 가맹점명 비교 ──────────────────────────────────────────────────────────
    ent_merchant = by_label.get("merchant_name")
    if ent_merchant and float(ent_merchant.get("confidence") or 0) >= _VISUAL_MIN_CONFIDENCE:
        extracted_m = _normalize_merchant(ent_merchant.get("text"))
        body_m = _normalize_merchant(body_evidence.get("merchantName") or body_evidence.get("merchant_name"))
        if extracted_m and body_m:
            if extracted_m not in body_m and body_m not in extracted_m:
                issues.append(
                    UnsupportedClaimIssue(
                        claim=f"가맹점명 불일치: 이미지 추출={ent_merchant.get('text')} / 입력값={body_evidence.get('merchantName')}",
                        taxonomy="contradictory_evidence",
                        reason=(
                            f"이미지에서 추출한 가맹점명({ent_merchant.get('text')})이 "
                            f"입력 가맹점명({body_evidence.get('merchantName')})과 일치하지 않습니다."
                        ),
                        severity="MEDIUM",
                        covered=False,
                        citation_count=0,
                        supporting_articles=[],
                    )
                )

    # ── 날짜 비교 ──────────────────────────────────────────────────────────────
    ent_date = by_label.get("date_occurrence")
    if ent_date and float(ent_date.get("confidence") or 0) >= _VISUAL_MIN_CONFIDENCE:
        extracted_d = _extract_date_part(ent_date.get("text"))
        body_d = _extract_date_part(body_evidence.get("occurredAt") or body_evidence.get("date_occurrence"))
        if extracted_d and body_d and extracted_d != body_d:
            issues.append(
                UnsupportedClaimIssue(
                    claim=f"거래일자 불일치: 이미지 추출={extracted_d} / 입력값={body_d}",
                    taxonomy="contradictory_evidence",
                    reason=(
                        f"이미지에서 추출한 날짜({extracted_d})와 "
                        f"입력 날짜({body_d})가 일치하지 않습니다."
                    ),
                    severity="MEDIUM",
                    covered=False,
                    citation_count=0,
                    supporting_articles=[],
                )
            )

    # ── 점수 산정 ──────────────────────────────────────────────────────────────
    if amount_mismatch:
        score = 0
    elif issues:
        score = 50

    return score, issues


def _is_excluded_required_input(req: dict[str, Any]) -> bool:
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
    lowered_compact = re.sub(r"\s+", "", lowered)
    return any(
        (kw.lower() in lowered) or (re.sub(r"\s+", "", kw.lower()) in lowered_compact)
        for kw in _EXCLUDED_REQUIRED_INPUT_KEYWORDS
    )


def _extract_article_tokens(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for m in _ARTICLE_RE.findall(text):
        token = f"제{m}조"
        if token not in out:
            out.append(token)
    return out


def _extract_chunk_id(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def _is_required_input_satisfied(body_evidence: dict[str, Any], req: dict[str, Any]) -> bool:
    """규정 추출 required_input이 실제 body_evidence에서 이미 충족되는지 판단."""
    doc = body_evidence.get("document") or {}
    attendees = body_evidence.get("attendees") or doc.get("attendees") or []
    text = " ".join(
        [
            str(req.get("field", "")).strip(),
            str(req.get("reason", "")).strip(),
            str(req.get("guide", "")).strip(),
        ]
    ).lower()
    compact = re.sub(r"\s+", "", text)

    checks: list[bool] = []
    if any(k in compact for k in ("거래일시", "거래의일시", "발생일시", "일시")):
        checks.append(_has_value(body_evidence.get("occurredAt")))
    if any(k in compact for k in ("가맹점", "거래처명", "사업자등록번호")):
        checks.append(_has_value(body_evidence.get("merchantName")))
    if any(k in compact for k in ("공급가액", "세액", "합계금액", "총결제금액", "금액")):
        checks.append(_has_value(body_evidence.get("amount")))
    if any(k in compact for k in ("결제수단", "법인카드", "계좌이체", "현금")):
        checks.append(_has_value(body_evidence.get("paymentMethod") or doc.get("paymentMethod")))
    if "업무목적" in compact:
        checks.append(_has_value(body_evidence.get("businessPurpose") or doc.get("businessPurpose")))
    if "참석자" in compact:
        checks.append(_has_value(attendees) or _has_value(body_evidence.get("attendeeCount") or doc.get("attendeeCount")))
    if "장소" in compact:
        checks.append(_has_value(body_evidence.get("location") or doc.get("location")))
    if "증빙" in compact:
        checks.append(_has_value(body_evidence.get("evidenceProvided") or doc.get("receiptQualified")))

    if checks:
        return all(checks)
    # 매핑 불가 항목은 미충족으로 취급 (보수적)
    return False


def _should_re_evaluate(score_breakdown: dict[str, Any], retry_count: int, max_retries: int) -> bool:
    if retry_count >= max_retries:
        return False
    rule_score = int(score_breakdown.get("rule_score") or score_breakdown.get("final_score") or 0)
    llm_score = int(score_breakdown.get("llm_score") or rule_score)
    fidelity = int(score_breakdown.get("fidelity") or 0)
    high_gap = abs(rule_score - llm_score) >= 20
    low_fidelity = fidelity < 40
    return bool(high_gap or low_fidelity)


def _build_review_audit_payload(
    *,
    state: dict[str, Any],
    verification_targets: list[str],
    retrieved_chunks: list[dict[str, Any]],
    cited_article_clauses: list[dict[str, str]],
    unsupported_claims: list[dict[str, Any]],
    verification_summary: dict[str, Any],
) -> dict[str, Any]:
    plan_steps = state.get("plan") or []
    tool_results = state.get("tool_results") or []
    execute_out = state.get("execute_output") or {}
    score = state.get("score_breakdown") or {}
    flags = state.get("flags") or {}
    quality = state.get("verification") or {}

    retrieved_evidence_ids: list[int] = []
    for chunk in retrieved_chunks:
        cid = _extract_chunk_id(chunk.get("chunk_id"))
        if cid is None:
            chunk_ids = chunk.get("chunk_ids") or []
            cid = _extract_chunk_id(chunk_ids[0] if chunk_ids else None)
        if cid is not None and cid not in retrieved_evidence_ids:
            retrieved_evidence_ids.append(cid)

    executed_tool_results: list[dict[str, Any]] = []
    for r in tool_results:
        facts = r.get("facts") or {}
        tool_name = str((r.get("tool") or r.get("skill") or "")).strip()
        executed_tool_results.append(
            {
                "tool": tool_name,
                "status": "failed" if tool_name in (execute_out.get("failed_tools") or []) else "ok",
                "fact_keys": sorted([str(k) for k in facts.keys()])[:12],
            }
        )

    confidence_risk_signals = {
        "severity": score.get("severity"),
        "policy_score": score.get("policy_score"),
        "evidence_score": score.get("evidence_score"),
        "final_score": score.get("final_score"),
        "compound_multiplier": score.get("compound_multiplier"),
        "coverage_ratio": verification_summary.get("coverage_ratio"),
        "covered_claims": verification_summary.get("covered"),
        "total_claims": verification_summary.get("total"),
        "gate_policy": verification_summary.get("gate_policy"),
        "quality_signals": quality.get("quality_signals") or [],
        "failed_tools": execute_out.get("failed_tools") or [],
        "missing_fields": ((state.get("body_evidence") or {}).get("dataQuality") or {}).get("missingFields") or [],
        "has_hitl_response": bool(flags.get("hasHitlResponse")),
        "verification_target_count": len(verification_targets),
    }

    return {
        "plan": plan_steps if isinstance(plan_steps, list) else [],
        "executed_tool_results": executed_tool_results,
        "retrieved_evidence_ids": retrieved_evidence_ids,
        "cited_article_clauses": cited_article_clauses,
        "unsupported_claims": unsupported_claims,
        "confidence_risk_signals": confidence_risk_signals,
    }


def _classify_unsupported_claims(
    *,
    verification_targets: list[str],
    verification_summary: dict[str, Any],
    claim_results: list[ClaimVerificationResult],
    retrieved_chunks: list[dict[str, Any]],
    missing_fields: list[str],
    required_inputs: list[dict[str, Any]],
    execute_failed_tools: list[str],
    body_evidence: dict[str, Any],
) -> list[UnsupportedClaimIssue]:
    issues: list[UnsupportedClaimIssue] = []
    details = verification_summary.get("details") or []

    # claim 단위 분류: no/weak/wrong_scope
    for i, claim in enumerate(verification_targets or []):
        detail = details[i] if i < len(details) and isinstance(details[i], dict) else {}
        cr = claim_results[i] if i < len(claim_results) else None
        covered = bool(detail.get("covered")) if detail else bool(getattr(cr, "covered", False))
        citation_count = int(detail.get("citation_count") or 0) if detail else 0
        supporting_articles = list((getattr(cr, "supporting_articles", None) or [])) if cr else []
        claim_article_tokens = _extract_article_tokens(claim)
        supporting_norm = {str(a).replace(" ", "") for a in supporting_articles}

        if not covered or citation_count <= 0:
            issues.append(
                UnsupportedClaimIssue(
                    claim=claim,
                    taxonomy="no_citation",
                    reason="주장에 대응되는 citation을 찾지 못했습니다.",
                    severity="HIGH",
                    covered=False,
                    citation_count=citation_count,
                    supporting_articles=supporting_articles,
                )
            )
            continue

        if citation_count == 1:
            issues.append(
                UnsupportedClaimIssue(
                    claim=claim,
                    taxonomy="weak_citation",
                    reason="단일 citation만 연결되어 근거 강도가 약합니다.",
                    severity="MEDIUM",
                    covered=True,
                    citation_count=citation_count,
                    supporting_articles=supporting_articles,
                )
            )

        if claim_article_tokens and supporting_articles:
            mismatch = all(t.replace(" ", "") not in supporting_norm for t in claim_article_tokens)
            if mismatch:
                issues.append(
                    UnsupportedClaimIssue(
                        claim=claim,
                        taxonomy="wrong_scope_citation",
                        reason=f"주장 조문({', '.join(claim_article_tokens)})과 연결 조문 범위가 다릅니다.",
                        severity="HIGH",
                        covered=True,
                        citation_count=citation_count,
                        supporting_articles=supporting_articles,
                    )
                )

    # 필수 증빙/입력 누락
    for field in (missing_fields or []):
        issues.append(
            UnsupportedClaimIssue(
                claim=f"필수 입력 누락: {field}",
                taxonomy="missing_mandatory_evidence",
                reason="필수 입력값 누락으로 근거 검증을 완료할 수 없습니다.",
                severity="HIGH",
                covered=False,
                citation_count=0,
                supporting_articles=[],
            )
        )
    for req in (required_inputs or []):
        if _is_required_input_satisfied(body_evidence, req):
            continue
        f = str(req.get("field") or "").strip()
        if not f:
            continue
        issues.append(
            UnsupportedClaimIssue(
                claim=f"필수 증빙/입력 필요: {f}",
                taxonomy="missing_mandatory_evidence",
                reason=str(req.get("reason") or "필수 증빙/입력값 보완 필요"),
                severity="HIGH",
                covered=False,
                citation_count=0,
                supporting_articles=[],
            )
        )

    # retrieval 신뢰도 저하
    if not retrieved_chunks:
        issues.append(
            UnsupportedClaimIssue(
                claim="검색된 규정 근거 없음",
                taxonomy="low_retrieval_confidence",
                reason="retrieval 결과가 비어 있어 근거 신뢰도가 낮습니다.",
                severity="HIGH",
                covered=False,
                citation_count=0,
                supporting_articles=[],
            )
        )
    else:
        scores: list[float] = []
        top_chunks = retrieved_chunks[:3]
        cited_articles = {
            str(chunk.get("article") or chunk.get("regulation_article") or "").strip()
            for chunk in retrieved_chunks[:10]
            if str(chunk.get("article") or chunk.get("regulation_article") or "").strip()
        }
        for chunk in top_chunks:
            score_detail = chunk.get("score_detail") or {}
            score_val = (
                score_detail.get("cross_encoder_score")
                or score_detail.get("rrf_score")
                or score_detail.get("bm25_score")
                or score_detail.get("dense_score")
                or chunk.get("retrieval_score")
            )
            try:
                if score_val is not None:
                    scores.append(float(score_val))
            except Exception:
                pass
        if scores:
            avg_top3 = sum(scores) / max(len(scores), 1)
            coverage_ratio = float(verification_summary.get("coverage_ratio") or 0.0)
            cited_article_count = len(cited_articles)
            # 복합 판정: top3 점수 저하 + 커버리지 낮음 + 인용 조문 부족일 때만 저신뢰로 간주
            if avg_top3 < 0.02 and coverage_ratio < 0.45 and cited_article_count < 2:
                issues.append(
                    UnsupportedClaimIssue(
                        claim="retrieval 점수 저하",
                        taxonomy="low_retrieval_confidence",
                        reason=(
                            f"상위 3개 평균 점수({avg_top3:.4f})/커버리지({coverage_ratio:.2f})/"
                            f"인용 조문 수({cited_article_count}) 기준으로 근거 신뢰도가 낮습니다."
                        ),
                        severity="MEDIUM",
                        covered=False,
                        citation_count=0,
                        supporting_articles=[],
                    )
                )

    if execute_failed_tools:
        issues.append(
            UnsupportedClaimIssue(
                claim="도구 실행 실패",
                taxonomy="contradictory_evidence",
                reason=f"핵심 도구 실패: {execute_failed_tools}",
                severity="HIGH",
                covered=False,
                citation_count=0,
                supporting_articles=[],
            )
        )

    # taxonomy/claim 중복 제거
    dedup: list[UnsupportedClaimIssue] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        key = (issue.taxonomy, issue.claim)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(issue)
    return dedup


async def critic_node_impl(
    state: dict[str, Any],
    *,
    max_critic_loop: int,
    tool_result_key: Callable[[dict[str, Any]], str],
    build_verification_targets: Callable[[dict[str, Any]], list[str]],
    call_node_llm_with_consistency_check: Callable[[str, Any, str], tuple[str, Any, bool]],
    stream_reasoning_events_with_llm: Callable[[str, str], Awaitable[tuple[str, list[dict[str, Any]], str]]],
) -> dict[str, Any]:
    legacy = next((r for r in state.get("tool_results", []) if tool_result_key(r) == "legacy_aura_deep_audit"), None)
    missing = ((state["body_evidence"].get("dataQuality") or {}).get("missingFields") or [])
    score = state.get("score_breakdown") or {}
    execute_out = state.get("execute_output") or {}
    tool_results = state.get("tool_results") or []
    failed_tools = execute_out.get("failed_tools") or []
    evidence_score = int(score.get("evidence_score") or 0)
    final_score = int(score.get("final_score") or 0)
    high_risk_compound = bool(state["flags"].get("isHoliday")) and bool(state["flags"].get("mccCode"))
    critical_tool_failed = "policy_rulebook_probe" in failed_tools or "document_evidence_probe" in failed_tools
    tool_failure_rate = len(failed_tools) / max(len(tool_results), 1)
    borderline_score = 48 <= final_score <= 62
    weak_evidence_with_risk = high_risk_compound and evidence_score < 30

    replan_reasons: list[str] = []
    if missing:
        replan_reasons.append(f"누락 필드 {missing} — 과잉 주장 위험")
    if critical_tool_failed:
        replan_reasons.append(
            f"핵심 도구 실패: {[t for t in failed_tools if t in {'policy_rulebook_probe', 'document_evidence_probe'}]}"
        )
    if tool_failure_rate >= 0.5 and len(tool_results) >= 2:
        replan_reasons.append(f"도구 실패율 {tool_failure_rate:.0%} — 증거 신뢰성 저하")
    if borderline_score:
        replan_reasons.append(f"최종점수 {final_score}점이 MEDIUM/HIGH 경계 ±7점 이내 — 추가 증거 필요")
    if weak_evidence_with_risk:
        replan_reasons.append(f"복합 위험(휴일+MCC) 케이스인데 evidence_score={evidence_score} (30점 미만)")

    loop_count = state.get("critic_loop_count") or 0
    replan_required = bool(
        replan_reasons
        and not state["flags"].get("hasHitlResponse")
        and loop_count < max_critic_loop
    )
    replan_reason = " | ".join(replan_reasons) if replan_reasons else ""
    if replan_required:
        logger.info(
            "[critic] 재계획 필요 (loop %d/%d) | %s",
            loop_count + 1, max_critic_loop, replan_reason[:150],
        )
    else:
        logger.info(
            "[critic] 재계획 불필요 | evidence_score=%d final_score=%d loop=%d",
            evidence_score, final_score, loop_count,
        )
    critique = {
        "has_legacy_result": bool(legacy and legacy.get("facts")),
        "missing_fields": missing,
        "risk_of_overclaim": bool(missing) or bool(replan_reasons),
        "recommend_hold": bool(replan_required or (missing and not state["flags"].get("hasHitlResponse"))),
    }
    replan_context: dict[str, Any] | None = None
    if replan_required:
        # score_breakdown이 None이거나 일부 키가 없는 경우를 방어적으로 처리
        _score_bd: dict[str, Any] = state.get("score_breakdown") or {}
        _diagnostic_log = str(_score_bd.get("diagnostic_log") or "").strip()
        _rule_score = _score_bd.get("rule_score") or _score_bd.get("final_score")
        _llm_score = _score_bd.get("llm_score")
        _fidelity = _score_bd.get("fidelity")
        replan_context = {
            "critic_feedback": replan_reason,
            "diagnostic_log": _diagnostic_log,
            "rule_score": _rule_score,
            "llm_score": _llm_score,
            "fidelity": _fidelity,
            "missing_fields": missing,
            "loop_count": loop_count + 1,
            "previous_tool_results": [tool_result_key(r) for r in state.get("tool_results", [])],
        }
    verification_targets = build_verification_targets(state)
    probe_facts = (
        next((r.get("facts") or {} for r in tool_results if tool_result_key(r) == "policy_rulebook_probe"), {})
        or {}
    )
    retrieved_chunks = probe_facts.get("retrieval_candidates") or probe_facts.get("policy_refs") or []
    cited_article_clauses: list[dict[str, str]] = []
    for ch in retrieved_chunks[:20]:
        article = str(ch.get("article") or ch.get("regulation_article") or "").strip()
        clause = str(ch.get("clause") or ch.get("regulation_clause") or "").strip()
        if not article:
            continue
        pair = {"article": article, "clause": clause}
        if pair not in cited_article_clauses:
            cited_article_clauses.append(pair)

    precheck_unsupported: list[dict[str, Any]] = []
    for field in missing:
        precheck_unsupported.append(
            UnsupportedClaimIssue(
                claim=f"필수 입력 누락: {field}",
                taxonomy="missing_mandatory_evidence",
                reason="입력 누락으로 주장 검증이 불완전합니다.",
                severity="HIGH",
                covered=False,
                citation_count=0,
                supporting_articles=[],
            ).model_dump()
        )
    if critical_tool_failed:
        precheck_unsupported.append(
            UnsupportedClaimIssue(
                claim="핵심 도구 실패",
                taxonomy="contradictory_evidence",
                reason=f"핵심 도구 실패: {[t for t in failed_tools if t in {'policy_rulebook_probe', 'document_evidence_probe'}]}",
                severity="HIGH",
                covered=False,
                citation_count=0,
                supporting_articles=[],
            ).model_dump()
        )
    if not retrieved_chunks:
        precheck_unsupported.append(
            UnsupportedClaimIssue(
                claim="retrieval 결과 없음",
                taxonomy="low_retrieval_confidence",
                reason="규정 근거 청크를 찾지 못했습니다.",
                severity="HIGH",
                covered=False,
                citation_count=0,
                supporting_articles=[],
            ).model_dump()
        )

    # ── Sprint 2: 이미지-텍스트 교차 검증 ────────────────────────────────────
    visual_audit_results: list[dict[str, Any]] = state.get("visual_audit_results") or []
    visual_consistency_score = 100
    if visual_audit_results:
        visual_consistency_score, visual_issues = _check_visual_consistency(
            state["body_evidence"], visual_audit_results
        )
        if visual_issues:
            logger.info(
                "critic_node: 이미지-텍스트 모순 감지 %d건, visual_consistency_score=%d",
                len(visual_issues), visual_consistency_score,
            )
            precheck_unsupported.extend(issue.model_dump() for issue in visual_issues)
            if not critique.get("risk_of_overclaim"):
                critique["risk_of_overclaim"] = True
        else:
            logger.info("critic_node: 이미지-텍스트 일치 확인 (visual_consistency_score=100)")

    review_audit = _build_review_audit_payload(
        state=state,
        verification_targets=verification_targets,
        retrieved_chunks=retrieved_chunks,
        cited_article_clauses=cited_article_clauses,
        unsupported_claims=precheck_unsupported,
        verification_summary={},
    )
    hold_required = bool(critique["recommend_hold"])
    human_review_required = bool(hold_required and not state["flags"].get("hasHitlResponse"))
    critic_output = CriticOutput(
        overclaim_risk=critique["risk_of_overclaim"],
        contradictions=[],
        missing_counter_evidence=missing,
        recommend_hold=critique["recommend_hold"],
        rationale=replan_reason[:300] if replan_reason else ("입력 누락 필드가 있으면 과잉 주장 위험이 있어 보류를 권고한다." if missing else "추가 보류 조건 없이 진행 가능하다."),
        has_legacy_result=critique["has_legacy_result"],
        verification_targets=verification_targets,
        replan_required=replan_required,
        replan_reason=replan_reason,
        hold_required=hold_required,
        human_review_required=human_review_required,
        citation_regeneration_required=False,
        risk_of_overclaim=critique["risk_of_overclaim"],
        review_audit=review_audit,
    )
    rationale = critic_output.rationale
    reasoning_parts = [rationale]
    if missing:
        reasoning_parts.append(f"누락 필드: {', '.join(missing[:5])}. 과잉 주장 위험이 있어 보류를 권고한다.")
    if replan_required:
        reasoning_parts.append(replan_reason or "")
    reasoning_text = " ".join(reasoning_parts).strip()
    reasoning_text, check, retried = call_node_llm_with_consistency_check("critic", critic_output.model_dump(), reasoning_text, max_retries=1)
    critic_context = {
        "missing_fields": missing,
        "recommend_hold": critique.get("recommend_hold"),
        "replan_required": replan_required,
        "unsupported_claims": precheck_unsupported[:8],
        "confidence_risk_signals": (review_audit.get("confidence_risk_signals") or {}),
        "last_node_summary": state.get("last_node_summary", "없음"),
    }
    reasoning_text, reasoning_events, note_source = await stream_reasoning_events_with_llm("critic", reasoning_text, context=critic_context)
    critic_output_dict = critic_output.model_dump()
    critic_output_dict["reasoning"] = reasoning_text
    last_node_summary = f"critic 완료: {reasoning_text[:60]}…" if len(reasoning_text) > 60 else f"critic 완료: {reasoning_text}"
    pending: list[dict[str, Any]] = [
        AgentEvent(event_type="NODE_START", node="critic", phase="reflect", message="전문 도구 결과와 입력 품질을 교차 검토합니다.", metadata={}).to_payload(),
    ]
    if retried:
        pending.append(
            AgentEvent(
                event_type="THINKING_RETRY",
                node="critic",
                phase="reflect",
                message="추론 정합성 불일치 감지 — 재검토 후 문구를 보정합니다.",
                metadata={"conflict": check.conflict_description},
            ).to_payload()
        )
    pending.extend(reasoning_events)
    pending.append(
        AgentEvent(
            event_type="NODE_END",
            node="critic",
            phase="reflect",
            message="비판적 재검토가 완료되었습니다.",
            metadata={"reasoning": reasoning_text, "note_source": note_source, **critique},
        ).to_payload(),
    )
    # ── Sprint 2: fidelity = min(기존 fidelity, visual_consistency_score) ────
    result: dict[str, Any] = {
        "critique": critique,
        "critic_output": critic_output_dict,
        "critic_loop_count": loop_count + 1 if replan_required else loop_count,
        "replan_context": replan_context,
        "review_audit": review_audit,
        "last_node_summary": last_node_summary,
        "pending_events": pending,
    }
    if visual_audit_results:
        current_fidelity = int(state.get("fidelity") or 0)
        updated_fidelity = min(current_fidelity, visual_consistency_score)
        if updated_fidelity != current_fidelity:
            logger.info(
                "critic_node: fidelity 하향 조정 %d → %d (visual_consistency_score=%d)",
                current_fidelity, updated_fidelity, visual_consistency_score,
            )
        # score_breakdown 과 최상위 fidelity 동기화
        score_bd_updated = dict(state.get("score_breakdown") or {})
        score_bd_updated["fidelity"] = updated_fidelity
        score_bd_updated["visual_consistency_score"] = visual_consistency_score
        result["fidelity"] = updated_fidelity
        result["score_breakdown"] = score_bd_updated
    return result


async def verify_node_impl(
    state: dict[str, Any],
    *,
    find_tool_result: Callable[[list[dict[str, Any]], str], dict[str, Any] | None],
    derive_hitl_from_regulation: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    generate_claim_display_texts: Callable[[list[dict[str, Any]], dict[str, Any]], Awaitable[list[str]]],
    build_hitl_request: Callable[..., dict[str, Any]],
    generate_hitl_review_content: Callable[[dict[str, Any], dict[str, Any], list[dict[str, Any]], str], Awaitable[dict[str, Any]]],
    retry_fill_hitl_review_when_empty: Callable[..., Awaitable[dict[str, Any]]],
    call_node_llm_with_consistency_check: Callable[[str, Any, str], tuple[str, Any, bool]],
    stream_reasoning_events_with_llm: Callable[[str, str], Awaitable[tuple[str, list[dict[str, Any]], str]]],
) -> dict[str, Any]:
    from services.evidence_verification import (
        EVIDENCE_GATE_HOLD,
        get_dynamic_coverage_thresholds,
        verify_evidence_coverage_claims,
    )

    verification = {"needs_hitl": False, "quality_signals": ["OK"]}
    verification_targets = (state.get("critic_output") or {}).get("verification_targets") or []
    probe_facts = (find_tool_result(state["tool_results"], "policy_rulebook_probe") or {}).get("facts", {}) or {}
    retrieved_chunks = probe_facts.get("retrieval_candidates") or probe_facts.get("policy_refs") or []
    verification_summary: dict[str, Any] = {}
    execute_out = state.get("execute_output") or {}
    execute_failed_tools = execute_out.get("failed_tools") or []
    score_bd = state.get("score_breakdown") or {}
    severity = score_bd.get("severity", "MEDIUM")
    final_score = float(score_bd.get("final_score") or 0)
    compound_multiplier = float(score_bd.get("compound_multiplier") or 1.0)
    hold_threshold, caution_threshold = get_dynamic_coverage_thresholds(
        severity=severity,
        final_score=final_score,
        compound_multiplier=compound_multiplier,
    )
    if verification_targets and retrieved_chunks:
        verification_summary = verify_evidence_coverage_claims(
            verification_targets,
            retrieved_chunks,
            threshold_hold=hold_threshold,
            threshold_caution=caution_threshold,
        )
    elif verification_targets:
        verification_summary = {"covered": 0, "total": len(verification_targets), "coverage_ratio": 0.0, "details": [], "gate_policy": EVIDENCE_GATE_HOLD, "missing_citations": verification_targets}
    verification["verification_summary"] = verification_summary
    retry_count = int(state.get("retry_count") or score_bd.get("retry_count") or 0)
    max_retries = int(state.get("max_retries") or score_bd.get("max_retries") or 2)
    re_evaluate_needed = _should_re_evaluate(score_bd, retry_count, max_retries)
    conflict_warning = bool(score_bd.get("conflict_warning"))
    regulation_driven = await derive_hitl_from_regulation(state)
    hitl_request = build_hitl_request(
        state["body_evidence"],
        state["tool_results"],
        critique=state.get("critique"),
        verification_summary=verification_summary,
        screening_result=state.get("screening_result"),
        score_breakdown=state.get("score_breakdown"),
        regulation_driven=regulation_driven,
    )
    if state["flags"].get("hasHitlResponse"):
        hitl_request = None
    needs_hitl = bool(hitl_request)
    verification["needs_hitl"] = needs_hitl
    verification["quality_signals"] = ["HITL_REQUIRED"] if needs_hitl else ["OK"]
    gate = VerifierGate.HITL_REQUIRED if needs_hitl else VerifierGate.READY
    verify_replan_reason = ""

    stop_words = {
        "이", "가", "을", "를", "의", "에", "으로", "로", "와", "과",
        "이다", "있음", "있다", "수", "하며", "하여", "해당", "필요",
        "대상", "조항", "기준", "한다", "되어", "위반", "가능성",
    }
    claim_results: list[ClaimVerificationResult] = []
    for detail in (verification_summary.get("details") or []):
        idx = detail.get("index", 0)
        claim_text = verification_targets[idx] if idx < len(verification_targets) else ""
        is_covered = bool(detail.get("covered"))

        supporting: list[str] = []
        if is_covered and retrieved_chunks:
            claim_words = {word for word in _WORD_RE.findall(claim_text.lower()) if word not in stop_words}
            for chunk in retrieved_chunks:
                chunk_combined = " ".join([
                    str(chunk.get("chunk_text") or ""),
                    str(chunk.get("parent_title") or ""),
                    str(chunk.get("article") or chunk.get("regulation_article") or ""),
                ])
                chunk_words = {word for word in _WORD_RE.findall(chunk_combined.lower()) if word not in stop_words}
                if len(claim_words & chunk_words) >= 3:
                    article = chunk.get("article") or chunk.get("regulation_article")
                    if article and article not in supporting:
                        supporting.append(article)

        gap_text = ""
        if not is_covered:
            if "심야" in claim_text or "23:00" in claim_text:
                gap_text = "심야 시간대 규정 조항(제23조/제38조)이 retrieval 결과에 포함되지 않음"
            elif "LEAVE" in claim_text or "휴일" in claim_text or "근태" in claim_text:
                gap_text = "근태·휴일 지출 연계 규정 청크 부족"
            elif "MCC" in claim_text or "업종" in claim_text:
                gap_text = "고위험 업종 관련 조항(제42조)이 retrieval 결과에 부재"
            elif "예산" in claim_text:
                gap_text = "예산 초과 관련 조항(제40조/제19조) 청크 미확보"
            else:
                gap_text = "해당 주장을 뒷받침할 규정 청크를 retrieval에서 찾지 못함"

        claim_results.append(ClaimVerificationResult(
            claim=claim_text,
            covered=is_covered,
            supporting_articles=supporting[:3],
            gap=gap_text,
        ))

    body_evidence = state.get("body_evidence") or {}
    claim_results_dicts = [c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in claim_results]
    display_texts = await generate_claim_display_texts(claim_results_dicts, body_evidence)
    logger.info(
        "[verify] 주장 검증 | 클레임=%d개, 커버=%s/%s, 커버율=%.0f%%",
        len(claim_results_dicts),
        verification_summary.get("covered", "?"),
        verification_summary.get("total", "?"),
        float(verification_summary.get("coverage_ratio", 0)) * 100,
    )
    logger.info(
        "[verify] claim display_text 생성 결과: claims=%s displays=%s",
        len(claim_results_dicts),
        len(display_texts or []),
    )
    if display_texts:
        for idx, row in enumerate(claim_results_dicts):
            if idx < len(display_texts):
                row["display_text"] = str(display_texts[idx] or "").strip()
    filled_display = sum(1 for row in claim_results_dicts if str(row.get("display_text") or "").strip())
    logger.info(
        "[verify] claim display_text 적용: filled=%s/%s",
        filled_display,
        len(claim_results_dicts),
    )

    required_inputs_from_reg = (regulation_driven.get("required_inputs") or []) if isinstance(regulation_driven, dict) else []
    missing_fields = ((state.get("body_evidence") or {}).get("dataQuality") or {}).get("missingFields") or []
    unsupported_claim_issues = _classify_unsupported_claims(
        verification_targets=verification_targets,
        verification_summary=verification_summary,
        claim_results=claim_results,
        retrieved_chunks=retrieved_chunks,
        missing_fields=missing_fields,
        required_inputs=required_inputs_from_reg,
        execute_failed_tools=execute_failed_tools,
        body_evidence=body_evidence,
    )
    unsupported_claims_dicts = [u.model_dump() for u in unsupported_claim_issues]
    taxonomy_codes = [f"UNSUPPORTED_{u.taxonomy.upper()}" for u in unsupported_claim_issues]
    if taxonomy_codes:
        verification["quality_signals"] = sorted(set(list(verification.get("quality_signals") or []) + taxonomy_codes))

    cited_article_clauses: list[dict[str, str]] = []
    for ch in retrieved_chunks[:20]:
        article = str(ch.get("article") or ch.get("regulation_article") or "").strip()
        clause = str(ch.get("clause") or ch.get("regulation_clause") or "").strip()
        if not article:
            continue
        pair = {"article": article, "clause": clause}
        if pair not in cited_article_clauses:
            cited_article_clauses.append(pair)

    blocking_unsupported_issues = [u for u in unsupported_claim_issues if u.taxonomy in _UNSUPPORTED_TAXONOMY_BLOCKING]
    has_blocking_unsupported = bool(blocking_unsupported_issues)
    citation_regeneration_required = (
        str(verification_summary.get("gate_policy") or "").lower() == "regenerate_citations"
        or any(u.taxonomy in {"weak_citation", "wrong_scope_citation"} for u in unsupported_claim_issues)
    )
    if has_blocking_unsupported:
        logger.warning(
            "[verify] FAIL-CLOSED HITL 강제 | blocking 이슈 %d건: %s",
            len(blocking_unsupported_issues),
            ", ".join(u.taxonomy for u in blocking_unsupported_issues[:3]),
        )
    # Fail-closed: unsupported claim이 blocking taxonomy로 감지되면 HITL로 강제.
    if has_blocking_unsupported and not state["flags"].get("hasHitlResponse"):
        needs_hitl = True
        verification["needs_hitl"] = True
        verification["quality_signals"] = sorted(
            set(["HITL_REQUIRED", "FAIL_CLOSED_UNSUPPORTED"] + list(verification.get("quality_signals") or []))
        )
        gate = VerifierGate.HITL_REQUIRED
        if not hitl_request:
            first_reason = (
                blocking_unsupported_issues[0].reason
                if blocking_unsupported_issues
                else "근거 부족 주장이 감지되었습니다."
            )
            hitl_request = {
                "required": True,
                "handoff": "FINANCE_REVIEWER",
                "why_hitl": f"근거 미검증 주장 검출(unsupported claim)로 자동 확정을 중단했습니다. {first_reason}",
                "blocking_gate": "HITL_REQUIRED",
                "blocking_reason": first_reason,
                "reasons": [u.reason for u in blocking_unsupported_issues[:5] if u.reason],
                "unresolved_claims": [f"[{u.taxonomy}] {u.claim}" for u in blocking_unsupported_issues[:5]],
                "review_questions": ["근거가 약하거나 누락된 주장에 대해 추가 증빙을 제출했는가?"],
                "questions": ["근거가 약하거나 누락된 주장에 대해 추가 증빙을 제출했는가?"],
                "required_inputs": required_inputs_from_reg[:5],
                "evidence_snapshot": [],
                "candidate_outcomes": ["APPROVE_AFTER_HITL", "HOLD_AFTER_HITL"],
                "unsupported_claims": unsupported_claims_dicts,
            }

    # NORMAL_BASELINE은 치명(blocking) 이슈가 없고 핵심 입력 누락/핵심 도구 실패가 없으면 자동 확정 경로를 우선한다.
    screening_case_type = str((state.get("screening_result") or {}).get("case_type") or body_evidence.get("case_type") or "").upper()
    is_normal_baseline = screening_case_type == "NORMAL_BASELINE"
    if (
        is_normal_baseline
        and not has_blocking_unsupported
        and not missing_fields
        and not execute_failed_tools
        and not state["flags"].get("hasHitlResponse")
    ):
        if hitl_request is not None:
            logger.info(
                "verify_node: NORMAL_BASELINE auto-ready override applied "
                "(non-blocking unsupported only)."
            )
            hitl_request = None
            needs_hitl = False
            verification["needs_hitl"] = False
            gate = VerifierGate.READY

    # 하이브리드 점수 편차/낮은 fidelity면 HITL 대신 재평가(planner 재진입) 우선 시도
    if (
        re_evaluate_needed
        and not needs_hitl
        and not state["flags"].get("hasHitlResponse")
        and retry_count < max_retries
    ):
        verify_replan_reason = (
            f"하이브리드 점수 재평가 필요(rule={score_bd.get('rule_score')}, "
            f"llm={score_bd.get('llm_score')}, fidelity={score_bd.get('fidelity')})"
        )
        verification["quality_signals"] = sorted(set(list(verification.get("quality_signals") or []) + ["RE_EVALUATE_REQUIRED"]))
        gate = VerifierGate.READY
        needs_hitl = False
        verification["needs_hitl"] = False
        hitl_request = None

    review_audit = _build_review_audit_payload(
        state=state,
        verification_targets=verification_targets,
        retrieved_chunks=retrieved_chunks,
        cited_article_clauses=cited_article_clauses,
        unsupported_claims=unsupported_claims_dicts,
        verification_summary=verification_summary,
    )

    hold_required = bool(
        str(verification_summary.get("gate_policy") or "").lower() == EVIDENCE_GATE_HOLD
        or has_blocking_unsupported
        or ((state.get("critic_output") or {}).get("recommend_hold") is True)
    )
    if re_evaluate_needed and retry_count < max_retries and not state["flags"].get("hasHitlResponse"):
        hold_required = False
    human_review_required = bool(needs_hitl or hold_required)
    risk_of_overclaim = bool((state.get("critic_output") or {}).get("overclaim_risk")) or has_blocking_unsupported

    coverage_ratio_dbg = float(verification_summary.get("coverage_ratio") or 0.0)
    cited_article_count_dbg = len(cited_article_clauses)
    _hitl_why = (hitl_request.get("why_hitl") or "")[:100] if hitl_request else ""
    if needs_hitl:
        logger.info(
            "[verify] ⚑  HITL 요청 | gate=%s case=%s | 이유: %s",
            gate.value, screening_case_type or "-", _hitl_why,
        )
    else:
        logger.info(
            "[verify] ✔ 자동 확정 가능 | gate=%s case=%s | 커버율=%.0f%% 인용조항=%d건",
            gate.value, screening_case_type or "-",
            coverage_ratio_dbg * 100, cited_article_count_dbg,
        )
    logger.info(
        "[verify] 게이트 결정: gate=%s needs_hitl=%s case_type=%s "
        "blocking=%s coverage=%.2f cited=%s",
        gate.value, needs_hitl, screening_case_type or "-",
        [u.taxonomy for u in blocking_unsupported_issues],
        coverage_ratio_dbg, cited_article_count_dbg,
    )

    rationale = hitl_request.get("why_hitl") if hitl_request else "자동 확정 가능한 상태로 검증이 완료되었습니다."
    if verify_replan_reason:
        rationale = f"{rationale} {verify_replan_reason}".strip()
    if conflict_warning:
        rationale = f"{rationale} 판단 불일치 주의: 규칙 점수와 LLM 점수 편차가 큽니다.".strip()
    verifier_output = VerifierOutput(
        grounded=not needs_hitl,
        needs_hitl=needs_hitl,
        missing_evidence=(hitl_request.get("missing_evidence") or hitl_request.get("reasons") or []) if hitl_request else [],
        gate=gate,
        rationale=rationale,
        quality_signals=verification["quality_signals"],
        claim_results=claim_results_dicts,
        unsupported_claims=unsupported_claim_issues,
        replan_required=bool((state.get("critic_output") or {}).get("replan_required")) or bool(verify_replan_reason),
        hold_required=hold_required,
        human_review_required=human_review_required,
        citation_regeneration_required=citation_regeneration_required,
        risk_of_overclaim=risk_of_overclaim,
        review_audit=review_audit,
    )
    reasoning_parts = [rationale]
    reasoning_parts.append("담당자 검토 필요" if needs_hitl else "자동 진행 가능")
    reasoning_text = " ".join(reasoning_parts).strip()
    reasoning_text, check, retried = call_node_llm_with_consistency_check("verify", verifier_output.model_dump(), reasoning_text, max_retries=1)
    verify_context = {
        "gate_result": gate.value,
        "needs_hitl": needs_hitl,
        "re_evaluate_needed": re_evaluate_needed,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "verification_targets": verification_targets,
        "unsupported_claims": unsupported_claims_dicts[:8],
        "confidence_risk_signals": (review_audit.get("confidence_risk_signals") or {}),
        "last_node_summary": state.get("last_node_summary", "없음"),
    }
    reasoning_text, reasoning_events, note_source = await stream_reasoning_events_with_llm("verify", reasoning_text, context=verify_context)
    verifier_output_dict = verifier_output.model_dump()
    verifier_output_dict["reasoning"] = reasoning_text

    if hitl_request:
        hitl_request["unsupported_claims"] = unsupported_claims_dicts
        llm_review = await generate_hitl_review_content(
            hitl_request,
            verification_summary,
            claim_results_dicts,
            reasoning_text,
        )
        if llm_review.get("review_reasons"):
            hitl_request["unresolved_claims"] = llm_review["review_reasons"]
        if llm_review.get("review_questions"):
            hitl_request["review_questions"] = llm_review["review_questions"]
            hitl_request["questions"] = llm_review["review_questions"]
        need_reasons = not (hitl_request.get("unresolved_claims"))
        need_questions = not (hitl_request.get("review_questions") or hitl_request.get("questions"))
        if need_reasons or need_questions:
            retry_result = await retry_fill_hitl_review_when_empty(
                hitl_request,
                verification_summary,
                claim_results_dicts,
                reasoning_text,
                empty_reasons=need_reasons,
                empty_questions=need_questions,
            )
            if retry_result.get("review_reasons"):
                hitl_request["unresolved_claims"] = retry_result["review_reasons"]
            if retry_result.get("review_questions"):
                hitl_request["review_questions"] = retry_result["review_questions"]
                hitl_request["questions"] = retry_result["review_questions"]
        final_reasons = [str(x).strip() for x in (hitl_request.get("unresolved_claims") or []) if str(x).strip()]
        if not final_reasons:
            final_reasons = [f"[{u.taxonomy}] {u.reason or u.claim}" for u in unsupported_claim_issues[:5]]
        final_questions = [str(x).strip() for x in (hitl_request.get("review_questions") or hitl_request.get("questions") or []) if str(x).strip()]
        hitl_request["unresolved_claims"] = final_reasons[:5]
        hitl_request["review_questions"] = final_questions[:MAX_HITL_QUESTIONS]
        hitl_request["questions"] = final_questions[:MAX_HITL_QUESTIONS]

        # ── Sprint 3: 전표 제출자 사전 답변 Q&A 매칭 ──────────────────────────
        memo = (state.get("body_evidence") or {}).get("memo") or {}
        hitl_request["uploader_answers"] = {
            "bktxt": str(memo.get("bktxt") or "").strip(),
            "user_reason": str(memo.get("user_reason") or "").strip(),
            "sgtxt": str(memo.get("sgtxt") or "").strip(),
        }
        try:
            from agent.langgraph_verification_logic import _match_questions_to_prior_answers
            qa_matches = await _match_questions_to_prior_answers(
                questions=final_questions[:MAX_HITL_QUESTIONS],
                body_evidence=state.get("body_evidence") or {},
            )
            hitl_request["qa_matches"] = qa_matches
            logger.info(
                "verify_node: qa_matches %d/%d covered",
                sum(1 for m in qa_matches if m.get("covered")),
                len(qa_matches),
            )
        except Exception as _qa_err:
            logger.warning("verify_node: qa_matching skipped (%s)", _qa_err)
            hitl_request["qa_matches"] = []

    events: list[dict[str, Any]] = [
        AgentEvent(event_type="NODE_START", node="verify", phase="verify", message="근거 정합성과 추가 검토 필요 여부를 확인합니다.", metadata={}).to_payload(),
    ]
    if retried:
        events.append(
            AgentEvent(
                event_type="THINKING_RETRY",
                node="verify",
                phase="verify",
                message="추론 정합성 불일치 감지 — 재검토 후 문구를 보정합니다.",
                metadata={"conflict": check.conflict_description},
            ).to_payload()
        )
    events.extend(reasoning_events)
    events.append(
        AgentEvent(
            event_type="GATE_APPLIED",
            node="verify",
            phase="verify",
            message="검증 게이트 적용이 완료되었습니다.",
            decision_code="HITL_REQUIRED" if needs_hitl else "READY",
            observation="담당자 검토 필요" if needs_hitl else "자동 진행 가능",
            metadata={**verification},
        ).to_payload(),
    )
    events.append(
        AgentEvent(
            event_type="NODE_END",
            node="verify",
            phase="verify",
            message="검증 단계가 완료되었습니다.",
            observation="담당자 검토 필요" if needs_hitl else "자동 진행 가능",
            metadata={"needs_hitl": needs_hitl, "reasoning": reasoning_text, "note_source": note_source},
        ).to_payload()
    )
    if hitl_request:
        events.append(
            AgentEvent(
                event_type="HITL_REQUESTED",
                node="verify",
                phase="verify",
                message="담당자 검토가 필요한 케이스로 분류되었습니다.",
                decision_code="HITL_REQUIRED",
                metadata=dict(hitl_request),
            ).to_payload(),
        )
    last_node_summary = f"verify 완료: {reasoning_text[:60]}…" if len(reasoning_text) > 60 else f"verify 완료: {reasoning_text}"
    return {
        "verification": verification,
        "verifier_output": verifier_output_dict,
        "hitl_request": hitl_request,
        "review_audit": review_audit,
        "retry_count": (retry_count + 1) if verify_replan_reason else retry_count,
        "max_retries": max_retries,
        "replan_context": (
            {
                "critic_feedback": verify_replan_reason,
                "loop_count": retry_count + 1,
                "source": "verify_re_evaluate",
                "score_breakdown": {
                    "rule_score": score_bd.get("rule_score"),
                    "llm_score": score_bd.get("llm_score"),
                    "fidelity": score_bd.get("fidelity"),
                },
            }
            if verify_replan_reason
            else state.get("replan_context")
        ),
        "last_node_summary": last_node_summary,
        "pending_events": events,
    }
