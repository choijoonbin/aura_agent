"""
Phase F: evidence verification 독립 검증 계층.
문장–청크 coverage 검증 및 coverage 부족 시 게이트 정책(hold / caution / regenerate_citations) 반환.
reporter 이전 게이트에서 사용 가능.
"""
from __future__ import annotations

import re
from typing import Any

# coverage 부족 시 정책: hold=보류, caution=주의 진행, regenerate_citations=인용 재생성 유도
EVIDENCE_GATE_HOLD = "hold"
EVIDENCE_GATE_CAUTION = "caution"
EVIDENCE_GATE_REGENERATE = "regenerate_citations"

# coverage 비율 임계값 (이하면 게이트 정책 적용)
DEFAULT_COVERAGE_THRESHOLD_HOLD = 0.5
DEFAULT_COVERAGE_THRESHOLD_CAUTION = 0.7

_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")


def _chunk_supports_claim(claim: str, chunk: dict[str, Any]) -> bool:
    """청크가 주장 문장을 뒷받침할 수 있는지 (단어 중복 2개 이상)."""
    words = set(_WORD_RE.findall((claim or "").lower()))
    if len(words) < 2:
        return bool(chunk)
    text_parts = [
        str(chunk.get("chunk_text") or ""),
        str(chunk.get("parent_title") or ""),
        str(chunk.get("article") or ""),
        str(chunk.get("regulation_clause") or ""),
    ]
    combined = " ".join(text_parts).lower()
    chunk_words = set(_WORD_RE.findall(combined))
    overlap = len(words & chunk_words)
    return overlap >= 2


def verify_evidence_coverage_claims(
    claims: list[str],
    retrieved_chunks: list[dict[str, Any]],
    *,
    threshold_hold: float = DEFAULT_COVERAGE_THRESHOLD_HOLD,
    threshold_caution: float = DEFAULT_COVERAGE_THRESHOLD_CAUTION,
) -> dict[str, Any]:
    """
    검증 대상 주장(문장)이 검색된 청크에 의해 뒷받침되는지 검증.
    반환: {"covered": int, "total": int, "coverage_ratio": float, "details": [...], "gate_policy": str | None}
    """
    total = len(claims) if claims else 0
    covered = sum(1 for c in (claims or []) if any(_chunk_supports_claim(c, ch) for ch in (retrieved_chunks or [])))
    ratio = (covered / total) if total else 1.0
    details: list[dict[str, Any]] = []
    for i, c in enumerate(claims or []):
        supported = any(_chunk_supports_claim(c, ch) for ch in (retrieved_chunks or []))
        details.append({"index": i, "sentence_preview": (c or "")[:80], "citation_count": 1 if supported else 0, "covered": supported})
    gate_policy: str | None = None
    if total > 0:
        if ratio < threshold_hold:
            gate_policy = EVIDENCE_GATE_HOLD
        elif ratio < threshold_caution:
            gate_policy = EVIDENCE_GATE_CAUTION
        elif ratio < 1.0:
            gate_policy = EVIDENCE_GATE_REGENERATE
    return {
        "covered": covered,
        "total": total,
        "coverage_ratio": round(ratio, 4),
        "details": details,
        "gate_policy": gate_policy,
        "missing_citations": [c for i, c in enumerate(claims or []) if i < len(details) and not details[i].get("covered")],
    }


def verify_evidence_coverage(
    sentences: list[dict[str, Any]],
    retrieved_chunks: list[dict[str, Any]],
    *,
    threshold_hold: float = DEFAULT_COVERAGE_THRESHOLD_HOLD,
    threshold_caution: float = DEFAULT_COVERAGE_THRESHOLD_CAUTION,
) -> dict[str, Any]:
    """
    문장이 검색된 청크에 근거하는지 검증.
    반환: {"covered": int, "total": int, "coverage_ratio": float, "details": [...], "gate_policy": str | None}
    gate_policy: coverage 부족 시 EVIDENCE_GATE_HOLD | EVIDENCE_GATE_CAUTION | EVIDENCE_GATE_REGENERATE 중 하나.
    """
    total = len(sentences) if sentences else 0
    covered = sum(1 for s in (sentences or []) if (s.get("citations") or []))
    ratio = (covered / total) if total else 1.0
    details: list[dict[str, Any]] = []
    for i, s in enumerate(sentences or []):
        cits = s.get("citations") or []
        details.append({"index": i, "sentence_preview": (s.get("sentence") or "")[:80], "citation_count": len(cits), "covered": len(cits) > 0})

    gate_policy: str | None = None
    if total > 0:
        if ratio < threshold_hold:
            gate_policy = EVIDENCE_GATE_HOLD
        elif ratio < threshold_caution:
            gate_policy = EVIDENCE_GATE_CAUTION
        elif ratio < 1.0:
            gate_policy = EVIDENCE_GATE_REGENERATE

    return {
        "covered": covered,
        "total": total,
        "coverage_ratio": round(ratio, 4),
        "details": details,
        "gate_policy": gate_policy,
    }
