from __future__ import annotations

from typing import Any


def _build_system_auto_finalize_blockers(
    verification_summary: dict[str, Any],
    *,
    quality_signals: list[str] | None = None,
    fallback_reason: str = "",
) -> list[str]:
    out: list[str] = []
    gate_policy = str(verification_summary.get("gate_policy") or "").strip()
    covered = verification_summary.get("covered")
    total = verification_summary.get("total")
    if gate_policy:
        out.append(f"검증 게이트 판정: {gate_policy}")
    if isinstance(covered, int) and isinstance(total, int) and total > 0:
        ratio = verification_summary.get("coverage_ratio")
        if isinstance(ratio, (int, float)):
            out.append(f"근거 연결률: {covered}/{total} ({float(ratio)*100:.1f}%)")
        else:
            out.append(f"근거 연결률: {covered}/{total}")
    missing_citations = verification_summary.get("missing_citations") or []
    if isinstance(missing_citations, list) and missing_citations:
        out.append(f"미연결 주장 수: {len(missing_citations)}건")
    for s in (quality_signals or []):
        t = str(s or "").strip()
        if t:
            out.append(f"검증 신호: {t}")
    if not out and fallback_reason:
        out.append(str(fallback_reason).strip())
    dedup: list[str] = []
    seen: set[str] = set()
    for s in out:
        k = s.strip()
        if not k or k in seen:
            continue
        seen.add(k)
        dedup.append(k)
    return dedup


def _assess_hitl_resolution_requirements(
    *,
    hitl_request: dict[str, Any],
    hitl_response: dict[str, Any],
    evidence_result: dict[str, Any] | None,
) -> tuple[bool, list[str], list[str]]:
    approved = hitl_response.get("approved") is True
    if not approved:
        return False, [], []

    blockers: list[str] = []
    followup: list[str] = []

    required_inputs = hitl_request.get("required_inputs") or []
    missing_required: list[dict[str, str]] = []
    extra = hitl_response.get("extra_facts") or {}
    attendees = hitl_response.get("attendees") or []
    for req in required_inputs:
        field = str(req.get("field") or "").strip()
        if not field:
            continue
        if field == "attendees":
            val = attendees
        else:
            val = hitl_response.get(field)
            if (val is None or (isinstance(val, str) and not val.strip())) and isinstance(extra, dict):
                val = extra.get(field)
        if val is None or (isinstance(val, str) and not val.strip()) or (isinstance(val, list) and not val):
            missing_required.append(req)
    if missing_required:
        blockers.append(f"필수 입력/증빙 항목 {len(missing_required)}개가 누락되었습니다.")
        for m in missing_required[:5]:
            q = str(m.get("guide") or m.get("reason") or m.get("field") or "").strip()
            if q:
                followup.append(q)

    evidence_passed = None
    if isinstance(evidence_result, dict) and evidence_result:
        evidence_passed = evidence_result.get("passed") is True
        if evidence_passed is False:
            blockers.append("첨부 증빙 검증 결과가 불일치(passed=false)입니다.")
            reasons = evidence_result.get("reasons") or []
            for r in reasons[:3]:
                t = str(r or "").strip()
                if t:
                    followup.append(f"증빙 불일치 사유 해소: {t}")

    dedup_blockers: list[str] = []
    seen_b: set[str] = set()
    for s in blockers:
        k = s.strip()
        if not k or k in seen_b:
            continue
        seen_b.add(k)
        dedup_blockers.append(k)
    dedup_followup: list[str] = []
    seen_q: set[str] = set()
    for s in followup:
        k = s.strip()
        if not k or k in seen_q:
            continue
        seen_q.add(k)
        dedup_followup.append(k)

    return (len(dedup_blockers) == 0), dedup_blockers, dedup_followup[:8]


def _pick_llm_review_reason(hitl_request: dict[str, Any]) -> str:
    for key in ("unresolved_claims", "reasons", "review_questions", "questions"):
        vals = hitl_request.get(key) or []
        if isinstance(vals, list):
            for v in vals:
                t = str(v or "").strip()
                if t:
                    return t
    for key in ("why_hitl", "blocking_reason"):
        t = str(hitl_request.get(key) or "").strip()
        if t:
            return t
    return ""


def _is_verify_ready_without_hitl(state: dict[str, Any]) -> bool:
    verification = state.get("verification") or {}
    verifier_output = state.get("verifier_output") or {}
    needs_hitl = bool(verification.get("needs_hitl"))
    gate_raw = verifier_output.get("gate")
    if hasattr(gate_raw, "value"):
        gate_raw = getattr(gate_raw, "value")
    gate = str(gate_raw or "").upper()
    if gate.startswith("VERIFIERGATE."):
        gate = gate.split(".", 1)[1]
    if not gate and not needs_hitl:
        quality = {str(x).upper() for x in (verification.get("quality_signals") or [])}
        if "OK" in quality:
            gate = "READY"
    return (not needs_hitl) and gate in {"READY", "PASS"}


def _format_hitl_reason_for_stream(hitl_payload: dict[str, Any]) -> str:
    if not hitl_payload:
        return ""
    why = str(hitl_payload.get("why_hitl") or hitl_payload.get("blocking_reason") or "").strip()
    reqs = hitl_payload.get("required_inputs") or []
    parts = []
    if why:
        parts.append(why)
    for r in reqs[:5]:
        if not isinstance(r, dict):
            continue
        guide = str(r.get("guide") or "").strip()
        reason = str(r.get("reason") or "").strip()
        field = str(r.get("field") or "").strip()
        if guide:
            parts.append(guide)
        elif reason or field:
            parts.append(f"{field}: {reason}" if field and reason else (reason or field))
    if not parts:
        return ""
    return " ".join(parts)[:400].rstrip()
