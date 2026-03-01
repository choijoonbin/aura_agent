from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from utils.config import settings


def persist_analysis_result(db: Session, *, run_id: str, result_payload: dict[str, Any]) -> None:
    result = (result_payload or {}).get("result") or {}
    score_breakdown = result.get("score_breakdown") or {}
    tool_results = result.get("tool_results") or []
    quality_gate_codes = result.get("quality_gate_codes") or []
    critique = result.get("critique") or {}
    hitl_request = result.get("hitl_request")
    hitl_response = result.get("hitl_response")

    rag_refs = []
    for tool in tool_results:
        if tool.get("skill") == "policy_rulebook_probe":
            for ref in tool.get("facts", {}).get("policy_refs", []) or []:
                rag_refs.append(ref)

    evidence_map = {
        "tool_results": tool_results,
        "hitl_request": hitl_request,
        "hitl_response": hitl_response,
        "critique": critique,
    }
    reason_text = result.get("reasonText") or ""
    sentences = [sentence.strip() for sentence in reason_text.split(". ") if sentence.strip()]
    sentence_map = []
    for idx, sentence in enumerate(sentences[:3], start=1):
        citation_ids = []
        if rag_refs:
            citation_ids = [f"C{min(idx, len(rag_refs))}"]
        sentence_map.append(
            {
                "sentence_index": idx,
                "sentence": sentence,
                "citation_ids": citation_ids,
            }
        )
    grounding_ratio = 1.0 if rag_refs else 0.0

    sql = text(
        """
        insert into dwp_aura.case_analysis_result (
            run_id,
            tenant_id,
            score,
            severity,
            reason_text,
            risk_score,
            violation_clause,
            reasoning_summary,
            recommended_action,
            confidence_json,
            evidence_json,
            similar_json,
            rag_refs_json,
            evidence_map_json,
            sentence_citation_map,
            analysis_score_breakdown,
            quality_gate_codes,
            grounding_coverage_ratio,
            ungrounded_claim_sentences,
            analysis_quality_signals
        ) values (
            :run_id,
            :tenant_id,
            :score,
            :severity,
            :reason_text,
            :risk_score,
            :violation_clause,
            :reasoning_summary,
            :recommended_action,
            cast(:confidence_json as jsonb),
            cast(:evidence_json as jsonb),
            cast(:similar_json as jsonb),
            cast(:rag_refs_json as jsonb),
            cast(:evidence_map_json as jsonb),
            cast(:sentence_citation_map as jsonb),
            cast(:analysis_score_breakdown as jsonb),
            cast(:quality_gate_codes as jsonb),
            :grounding_coverage_ratio,
            :ungrounded_claim_sentences,
            cast(:analysis_quality_signals as jsonb)
        )
        on conflict (run_id) do update set
            score = excluded.score,
            severity = excluded.severity,
            reason_text = excluded.reason_text,
            risk_score = excluded.risk_score,
            violation_clause = excluded.violation_clause,
            reasoning_summary = excluded.reasoning_summary,
            recommended_action = excluded.recommended_action,
            confidence_json = excluded.confidence_json,
            evidence_json = excluded.evidence_json,
            similar_json = excluded.similar_json,
            rag_refs_json = excluded.rag_refs_json,
            evidence_map_json = excluded.evidence_map_json,
            sentence_citation_map = excluded.sentence_citation_map,
            analysis_score_breakdown = excluded.analysis_score_breakdown,
            quality_gate_codes = excluded.quality_gate_codes,
            grounding_coverage_ratio = excluded.grounding_coverage_ratio,
            ungrounded_claim_sentences = excluded.ungrounded_claim_sentences,
            analysis_quality_signals = excluded.analysis_quality_signals
        """
    )
    db.execute(
        sql,
        {
            "run_id": run_id,
            "tenant_id": settings.default_tenant_id,
            "score": float(result.get("score") or 0),
            "severity": result.get("severity"),
            "reason_text": reason_text,
            "risk_score": int(score_breakdown.get("final_score") or 0),
            "violation_clause": ", ".join([ref.get("article") for ref in rag_refs if ref.get("article")]) or None,
            "reasoning_summary": reason_text,
            "recommended_action": "HITL_REQUIRED" if hitl_request else "REVIEW",
            "confidence_json": json.dumps({"analysis_mode": result.get("analysis_mode")}, ensure_ascii=False),
            "evidence_json": json.dumps(tool_results, ensure_ascii=False, default=str),
            "similar_json": json.dumps([], ensure_ascii=False),
            "rag_refs_json": json.dumps(rag_refs, ensure_ascii=False, default=str),
            "evidence_map_json": json.dumps(evidence_map, ensure_ascii=False, default=str),
            "sentence_citation_map": json.dumps(sentence_map, ensure_ascii=False, default=str),
            "analysis_score_breakdown": json.dumps(score_breakdown, ensure_ascii=False, default=str),
            "quality_gate_codes": json.dumps(quality_gate_codes, ensure_ascii=False),
            "grounding_coverage_ratio": grounding_ratio,
            "ungrounded_claim_sentences": 0 if rag_refs else 1,
            "analysis_quality_signals": json.dumps(quality_gate_codes, ensure_ascii=False),
        },
    )
    db.commit()
