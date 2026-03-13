from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from agent.event_schema import AgentEvent
from agent.output_models import Citation, ReporterOutput, ReporterSentence

logger = logging.getLogger(__name__)


async def hitl_validate_node_impl(
    state: dict[str, Any],
    *,
    get_hitl_response_value: Callable[[dict[str, Any], str], Any],
) -> dict[str, Any]:
    """
    재분석 시: 사용자가 입력한 HITL 응답이 규정에서 요구한 필수 항목을 채웠는지 에이전트가 판단.
    누락이 있으면 해당 항목만 담은 새 HITL 요청을 만들어 hitl_pause로 돌려 추가 입력을 받는다.
    """
    hitl_request = state.get("hitl_request") or {}
    hitl_response = (state.get("body_evidence") or {}).get("hitlResponse") or {}
    required_inputs = hitl_request.get("required_inputs") or []
    logger.info(
        "[RESUME_TRACE] hitl_validate_node: required_inputs=%s hitl_response_keys=%s",
        [r.get("field") for r in required_inputs[:10]],
        list(hitl_response.keys()) if isinstance(hitl_response, dict) else type(hitl_response).__name__,
    )
    if not required_inputs:
        logger.info("[RESUME_TRACE] hitl_validate_node: required_inputs 비어 있음 → reporter")
        return {"hitl_request": None}

    missing: list[dict[str, str]] = []
    for req in required_inputs:
        field = (req.get("field") or "").strip()
        if not field:
            continue
        val = get_hitl_response_value(hitl_response, field)
        if val is None:
            missing.append(req)
            logger.info("[RESUME_TRACE] hitl_validate_node: field=%r → 값 없음 (missing)", field)
            continue
        if isinstance(val, list):
            if not val:
                missing.append(req)
                logger.info("[RESUME_TRACE] hitl_validate_node: field=%r → 빈 리스트 (missing)", field)
            continue
        if isinstance(val, str) and not val.strip():
            missing.append(req)
            logger.info("[RESUME_TRACE] hitl_validate_node: field=%r → 빈 문자열 (missing)", field)

    if not missing:
        flags_has = (state.get("flags") or {}).get("hasHitlResponse")
        logger.info(
            "[RESUME_TRACE] hitl_validate_node: 모든 필수 항목 충족 → reporter (flags.hasHitlResponse=%s, True여야 사용자 답변 반영 후 verdict LLM 호출됨)",
            flags_has,
        )
        return {"hitl_request": None}

    logger.info(
        "[RESUME_TRACE] hitl_validate_node: 누락 필드=%s → hitl_pause 재요청",
        [m.get("field") for m in missing[:5]],
    )
    new_request = dict(hitl_request)
    new_request["required_inputs"] = missing
    new_request["why_hitl"] = "규정에서 요구한 필수 입력/증빙 항목 중 아래 항목이 비어 있어 추가 입력이 필요합니다."
    new_request["reasons"] = [f"필수 항목 미기입: {m.get('field', '')} — {m.get('reason', '')}" for m in missing[:5]]
    new_request["review_questions"] = [m.get("guide", m.get("reason", "")) for m in missing if m.get("guide") or m.get("reason")]
    if not new_request.get("review_questions"):
        new_request["review_questions"] = [f"{m.get('field')}: {m.get('reason')}" for m in missing]
    new_request["questions"] = new_request["review_questions"]
    return {"hitl_request": new_request}


async def hitl_pause_node_impl(
    state: dict[str, Any],
    *,
    interrupt_fn: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    """Phase D: HITL 필요 시 interrupt()로 중단. resume 시 hitl_response를 body_evidence에 반영하고, hitl_validate를 거쳐 reporter 또는 재요청으로 간다."""
    hitl_request = state.get("hitl_request") or {}
    hitl_response = interrupt_fn(hitl_request)
    hitl_response = hitl_response if isinstance(hitl_response, dict) else {}
    looks_like_resume = isinstance(hitl_response, dict) and (
        "approved" in hitl_response or "comment" in hitl_response or "reviewer" in hitl_response
    )
    logger.info(
        "[RESUME_TRACE] hitl_pause_node: interrupt 반환 keys=%s looks_like_resume=%s",
        list(hitl_response.keys()) if hitl_response else [],
        looks_like_resume,
    )
    body = dict(state.get("body_evidence") or {})
    body["hitlResponse"] = hitl_response
    out: dict[str, Any] = {"body_evidence": body}
    if looks_like_resume and hitl_response:
        flags = dict(state.get("flags") or {})
        flags["hasHitlResponse"] = True
        flags["hitlApproved"] = hitl_response.get("approved") is True
        out["flags"] = flags
        logger.info(
            "[HITL_RESPONSE_TRACE] hitl_pause_node: 재개 감지 → flags.hasHitlResponse=True hitlApproved=%s 반영 (verdict LLM 호출되도록 함)",
            flags["hitlApproved"],
        )
    return out


async def reporter_node_impl(
    state: dict[str, Any],
    *,
    score_with_hitl_adjustment: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    format_occurred_at: Callable[[Any], str],
    llm_decide_hitl_verdict: Callable[..., Awaitable[tuple[str, str]]],
    llm_summarize_hold_reason: Callable[[dict[str, Any]], Awaitable[str]],
    is_verify_ready_without_hitl: Callable[[dict[str, Any]], bool],
    top_policy_refs: Callable[[list[dict[str, Any]], int], list[dict[str, Any]]],
    select_policy_refs_by_relevance: Callable[[dict[str, Any], list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]],
    call_node_llm_with_consistency_check: Callable[[str, Any, str], tuple[str, Any, bool]],
    stream_reasoning_events_with_llm: Callable[[str, str], Awaitable[tuple[str, list[dict[str, Any]], str]]],
) -> dict[str, Any]:
    score = score_with_hitl_adjustment(state["score_breakdown"], state["flags"])
    hitl_request = state.get("hitl_request")
    body = state["body_evidence"]
    hitl_response = body.get("hitlResponse") or {}
    hitl_approved = hitl_response.get("approved")
    hitl_verdict_reason = ""
    occurred_at = format_occurred_at(body.get("occurredAt"))
    merchant = body.get("merchantName") or "거래처 미상"
    summary = (
        f"전표는 {occurred_at} 시점 {merchant} 사용 건으로 분석되었습니다. "
        f"정책점수 {score['policy_score']}점, 근거점수 {score['evidence_score']}점, 최종점수 {score['final_score']}점입니다."
    )
    has_hitl_response = state["flags"].get("hasHitlResponse")
    body_has_hitl = bool(hitl_response)
    if body_has_hitl and not has_hitl_response:
        logger.warning(
            "[HITL_RESPONSE_TRACE] reporter_node 의심: body_evidence.hitlResponse 있음(keys=%s) but flags.hasHitlResponse=False → 사용자 답변 작성했는데 응답 없으면 flags 미반영 가능성",
            list(hitl_response.keys())[:12] if isinstance(hitl_response, dict) else None,
        )
    logger.info(
        "[VERDICT_LLM] reporter_node verdict 분기: hitl_request=%s hasHitlResponse=%s hitl_approved=%s (body_has_hitl=%s)",
        bool(hitl_request),
        has_hitl_response,
        hitl_approved,
        body_has_hitl,
    )
    if hitl_request:
        summary += " 담당자 검토가 필요한 상태입니다."
        verdict = "HITL_REQUIRED"
        logger.info("[VERDICT_LLM] reporter_node → 분기: hitl_request 있음, verdict=HITL_REQUIRED (verdict LLM 미호출)")
    elif has_hitl_response and hitl_approved is True:
        prior_hitl_request = (body.get("hitlRequest") or {}) if isinstance(body.get("hitlRequest"), dict) else {}
        check_req = hitl_request or prior_hitl_request or {}
        evidence_result = body.get("evidenceDocumentResult") if isinstance(body.get("evidenceDocumentResult"), dict) else None
        logger.info(
            "[VERDICT_LLM] reporter_node → verdict LLM 호출 (hasHitlResponse=True approved=True) check_req_keys=%s",
            list(check_req.keys())[:12] if isinstance(check_req, dict) else None,
        )
        verdict, hitl_verdict_reason = await llm_decide_hitl_verdict(
            hitl_request=check_req,
            hitl_response=hitl_response,
            evidence_result=evidence_result,
        )
        logger.info(
            "[VERDICT_LLM] reporter_node ← verdict LLM 반환 verdict=%s reason_preview=%s",
            verdict,
            (hitl_verdict_reason[:100] + "…") if hitl_verdict_reason and len(hitl_verdict_reason) > 100 else (hitl_verdict_reason or "(없음)"),
        )
        if verdict == "COMPLETED_AFTER_HITL":
            summary += f" {hitl_verdict_reason}" if hitl_verdict_reason else " 담당자 검토 결과 승인 가능으로 판단되어 최종 확정 후보로 전환되었습니다."
        else:
            summary += f" {hitl_verdict_reason}" if hitl_verdict_reason else " 담당자 검토 기준 미충족으로 재검토가 필요합니다."
    elif has_hitl_response and hitl_approved is False:
        verdict = "HOLD_AFTER_HITL"
        hold_summary = await llm_summarize_hold_reason(hitl_response)
        if hold_summary:
            hitl_verdict_reason = hold_summary
            summary += f" 담당자 검토 결과 보류합니다. {hold_summary}"
        else:
            hitl_verdict_reason = ""
            hold_reason = (hitl_response.get("comment") or hitl_response.get("business_purpose") or "").strip()
            if hold_reason:
                summary += f" 담당자 검토 결과 보류합니다. 담당자 사유: {hold_reason[:400]}{'…' if len(hold_reason) > 400 else ''}"
            else:
                summary += " 담당자 검토 결과 보류/추가 검토 의견이 있어 자동 확정을 중단합니다."
        logger.info("[VERDICT_LLM] reporter_node → 분기: approved=False, verdict=HOLD_AFTER_HITL (verdict LLM 미호출)")
    else:
        if is_verify_ready_without_hitl(state):
            summary += " 검증 게이트를 통과해 자동 확정 후보로 분류되었습니다."
        else:
            summary += " 현재 수집된 증거 기준으로 추가 검토 우선순위가 높습니다."
        verdict = "READY"
        logger.info("[VERDICT_LLM] reporter_node → 분기: hasHitlResponse=False 또는 기타, verdict=READY (verdict LLM 미호출)")
    refs = top_policy_refs(state.get("tool_results", []), limit=5)
    refs = await select_policy_refs_by_relevance(state, refs)
    citations_list = []
    for ref in refs:
        cids = ref.get("chunk_ids") or []
        chunk_id = str(cids[0]) if cids else None
        citations_list.append(Citation(chunk_id=chunk_id, article=ref.get("article") or "조항 미상", title=ref.get("parent_title")))
    sentences_list: list[ReporterSentence] = [
        ReporterSentence(sentence=summary, citations=citations_list),
    ]
    reasoning_text = f"{summary} {verdict}".strip()
    reasoning_text, check, retried = call_node_llm_with_consistency_check("reporter", {"verdict": verdict}, reasoning_text, max_retries=1)
    reporter_context = {
        "verdict": verdict,
        "summary": summary,
        "score_breakdown": score,
        "last_node_summary": state.get("last_node_summary", "없음"),
    }
    reasoning_text, reasoning_events, note_source = await stream_reasoning_events_with_llm("reporter", reasoning_text, context=reporter_context)
    reporter_output = ReporterOutput(summary=summary, verdict=verdict, sentences=sentences_list, reasoning=reasoning_text)
    reporter_output_dict = reporter_output.model_dump()
    if verdict in ("COMPLETED_AFTER_HITL", "REVIEW_REQUIRED", "HOLD_AFTER_HITL") and hitl_verdict_reason:
        reporter_output_dict["hitl_verdict_reason"] = hitl_verdict_reason
    last_node_summary = f"reporter 완료: {reasoning_text[:60]}…" if len(reasoning_text) > 60 else f"reporter 완료: {reasoning_text}"
    pending: list[dict[str, Any]] = [
        AgentEvent(event_type="NODE_START", node="reporter", phase="report", message="사용자에게 제시할 보고 문안을 구성합니다.", metadata={}).to_payload(),
    ]
    if retried:
        pending.append(
            AgentEvent(
                event_type="THINKING_RETRY",
                node="reporter",
                phase="report",
                message="추론 정합성 불일치 감지 — 재검토 후 문구를 보정합니다.",
                metadata={"conflict": check.conflict_description},
            ).to_payload()
        )
    pending.extend(reasoning_events)
    pending.append(
        AgentEvent(
            event_type="NODE_END",
            node="reporter",
            phase="report",
            message="보고 문안 구성이 완료되었습니다.",
            metadata={"summary": summary, "verdict": verdict, "reasoning": reasoning_text, "note_source": note_source},
        ).to_payload(),
    )
    return {
        "reporter_output": reporter_output_dict,
        "last_node_summary": last_node_summary,
        "pending_events": pending,
    }


async def finalizer_node_impl(
    state: dict[str, Any],
    *,
    score_with_hitl_adjustment: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    is_verify_ready_without_hitl: Callable[[dict[str, Any]], bool],
    llm_summarize_completed_reason: Callable[[dict[str, Any]], Awaitable[str]],
    build_grounded_reason: Callable[[dict[str, Any], str | None], tuple[str, str]],
    call_node_llm_with_consistency_check: Callable[[str, Any, str], tuple[str, Any, bool]],
    stream_reasoning_events_with_llm: Callable[[str, str], Awaitable[tuple[str, list[dict[str, Any]], str]]],
    build_system_auto_finalize_blockers: Callable[..., list[str]],
    generate_hitl_review_content: Callable[[dict[str, Any], dict[str, Any], list[dict[str, Any]], str], Awaitable[dict[str, Any]]],
    retry_fill_hitl_review_when_empty: Callable[..., Awaitable[dict[str, Any]]],
    find_tool_result: Callable[[list[dict[str, Any]], str], dict[str, Any] | None],
) -> dict[str, Any]:
    score = score_with_hitl_adjustment(state["score_breakdown"], state["flags"])
    hitl_request = state.get("hitl_request")
    completed_tail = None
    if not hitl_request and not state.get("flags", {}).get("hasHitlResponse"):
        verify_ready = is_verify_ready_without_hitl(state)
        reporter_verdict = str((state.get("reporter_output") or {}).get("verdict") or "").upper()
        if verify_ready and reporter_verdict in {"", "READY", "COMPLETED", "AUTO_APPROVED"}:
            completed_tail = await llm_summarize_completed_reason(state)
    reason, status = build_grounded_reason(state, completed_tail=completed_tail)
    final_reasoning = f"{reason} 최종 상태는 {status}로 확정한다.".strip()
    final_reasoning, final_check, final_retried = call_node_llm_with_consistency_check("finalizer", {"status": status}, final_reasoning, max_retries=1)
    finalizer_context = {
        "status": status,
        "score_breakdown": score,
        "has_hitl_request": bool(hitl_request),
        "last_node_summary": state.get("last_node_summary", "없음"),
        "score_semantics": (
            "정책점수(policy_score)는 위험 지표: 높을수록 휴일/심야/근태충돌/고위험업종 등 위반 정황이 많음. "
            "근거점수(evidence_score)는 수집 증거 충실도: 높을수록 규정 조항·전표 증거가 잘 확보됨. "
            "따라서 정책점수 높음=위험 높음, 근거점수 높음=증거 충실."
        ),
    }
    final_reasoning, final_reasoning_events, note_source = await stream_reasoning_events_with_llm("finalizer", final_reasoning, context=finalizer_context)
    if status == "REVIEW_REQUIRED" and not hitl_request:
        verification_summary = (state.get("verification") or {}).get("verification_summary") or {}
        verifier_output = state.get("verifier_output") or {}
        claim_results = verifier_output.get("claim_results") or []
        stop_reasons = build_system_auto_finalize_blockers(
            verification_summary,
            quality_signals=(state.get("verification") or {}).get("quality_signals") or [],
            fallback_reason=reason,
        )
        seed_request = {
            "required": True,
            "handoff": "FINANCE_REVIEWER",
            "why_hitl": reason,
            "blocking_gate": "REVIEW_REQUIRED",
            "blocking_reason": reason,
            "reasons": stop_reasons,
            "auto_finalize_blockers": stop_reasons,
            "required_inputs": [],
            "evidence_snapshot": [],
        }
        llm_review = await generate_hitl_review_content(
            seed_request,
            verification_summary,
            claim_results if isinstance(claim_results, list) else [],
            final_reasoning,
        )
        review_reasons = [str(x).strip() for x in (llm_review.get("review_reasons") or []) if str(x).strip()]
        review_questions = [str(x).strip() for x in (llm_review.get("review_questions") or []) if str(x).strip()]
        if not review_reasons or not review_questions:
            retry_result = await retry_fill_hitl_review_when_empty(
                seed_request,
                verification_summary,
                claim_results if isinstance(claim_results, list) else [],
                final_reasoning,
                empty_reasons=not review_reasons,
                empty_questions=not review_questions,
            )
            if not review_reasons:
                review_reasons = [str(x).strip() for x in (retry_result.get("review_reasons") or []) if str(x).strip()]
            if not review_questions:
                review_questions = [str(x).strip() for x in (retry_result.get("review_questions") or []) if str(x).strip()]
        if not review_reasons:
            review_reasons = [str(reason or "검토가 필요한 근거를 확인해 주세요.").strip()]
        if not review_questions:
            review_questions = [f"다음 판단 사유를 해소할 근거를 확인할 수 있는가: {str(reason or '')[:180]}"]
        hitl_request = {
            **seed_request,
            "why_hitl": review_reasons[0],
            "blocking_reason": review_reasons[0],
            "reasons": review_reasons,
            "auto_finalize_blockers": stop_reasons,
            "unresolved_claims": review_reasons,
            "review_questions": review_questions,
            "questions": review_questions,
        }

    probe_facts = (find_tool_result(state["tool_results"], "policy_rulebook_probe") or {}).get("facts", {}) or {}
    policy_refs = probe_facts.get("policy_refs") or []
    reporter_out = state.get("reporter_output") or {}
    sentences = reporter_out.get("sentences") or []
    adopted_citations: list[dict[str, Any]] = []
    for s in sentences:
        for c in (s.get("citations") or []):
            if isinstance(c, dict):
                cit = dict(c)
            else:
                cit = {"chunk_id": str(c)} if c is not None else {}
            if "adoption_reason" not in cit:
                cid = str(cit.get("chunk_id") or "")
                art = cit.get("article")
                for ref in policy_refs:
                    ref_cids = [str(x) for x in (ref.get("chunk_ids") or [])]
                    if cid and cid in ref_cids:
                        cit["adoption_reason"] = ref.get("adoption_reason", "규정 근거로 채택")
                        break
                    if art and str(ref.get("article") or "") == str(art):
                        cit["adoption_reason"] = ref.get("adoption_reason", "규정 근거로 채택")
                        break
                else:
                    cit.setdefault("adoption_reason", "규정 근거로 채택")
            adopted_citations.append(cit)
    retrieval_snapshot = {
        "candidates_after_rerank": probe_facts.get("retrieval_candidates") or policy_refs,
        "adopted_citations": adopted_citations,
    }
    final = {
        "caseId": state["case_id"],
        "status": status,
        "reasonText": reason,
        "score": score["final_score"] / 100,
        "severity": score.get("severity") or ("HIGH" if score["final_score"] >= 70 else ("MEDIUM" if score["final_score"] >= 40 else "LOW")),
        "analysis_mode": "langgraph_agentic",
        "score_breakdown": score,
        "quality_gate_codes": (state.get("verification") or {}).get("quality_signals", []),
        "hitl_request": hitl_request,
        "tool_results": state["tool_results"],
        "policy_refs": probe_facts.get("policy_refs") or [],
        "critique": state.get("critique"),
        "hitl_response": (state["body_evidence"].get("hitlResponse") or None),
        "planner_output": state.get("planner_output"),
        "execute_output": state.get("execute_output"),
        "critic_output": state.get("critic_output"),
        "verifier_output": state.get("verifier_output"),
        "reporter_output": state.get("reporter_output"),
        "retrieval_snapshot": retrieval_snapshot,
        "verification_summary": (state.get("verification") or {}).get("verification_summary"),
        "rule_score": score.get("rule_score"),
        "llm_score": score.get("llm_score"),
        "final_decision": score.get("final_decision"),
        "verification_gate": score.get("verification_gate"),
        "fidelity": score.get("fidelity"),
        "fallback_used": score.get("fallback_used"),
        "fallback_reason": score.get("fallback_reason"),
        "summary_reason": score.get("summary_reason"),
        "diagnostic_log": score.get("diagnostic_log"),
        "latency_ms": {"llm_judge": score.get("latency_ms")},
        "version_meta": score.get("version_meta") or {},
        "evaluation_history": state.get("evaluation_history") or [],
    }
    pending_events: list[dict[str, Any]] = [
        AgentEvent(event_type="NODE_START", node="finalizer", phase="finalize", message="최종 판정 결과를 확정합니다.", metadata={}).to_payload(),
    ]
    if final_retried:
        pending_events.append(
            AgentEvent(
                event_type="THINKING_RETRY",
                node="finalizer",
                phase="finalize",
                message="추론 정합성 불일치 감지 — 재검토 후 문구를 보정합니다.",
                metadata={"conflict": final_check.conflict_description},
            ).to_payload()
        )
    pending_events.extend(final_reasoning_events)
    pending_events.append(
        AgentEvent(
            event_type="NODE_END",
            node="finalizer",
            phase="finalize",
            message="최종 분석 결과가 생성되었습니다.",
            observation=f"최종 상태={status}",
            metadata={"status": status, "reasoning": final_reasoning, "note_source": note_source},
        ).to_payload()
    )
    return {
        "final_result": final,
        "pending_events": pending_events,
    }
