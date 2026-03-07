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


def get_dynamic_coverage_thresholds(
    severity: str,
    final_score: float,
    compound_multiplier: float = 1.0,
) -> tuple[float, float]:
    """
    케이스 심각도와 점수에 따라 동적으로 coverage 임계값을 반환한다.
    반환: (hold_threshold, caution_threshold)
    CRITICAL/HIGH 케이스는 임계값 상향, 복합 위험 승수 >= 1.3 시 추가 상향.
    """
    sev = str(severity or "").upper()
    base_hold = DEFAULT_COVERAGE_THRESHOLD_HOLD
    base_caution = DEFAULT_COVERAGE_THRESHOLD_CAUTION
    severity_delta = {
        "CRITICAL": 0.25,
        "HIGH": 0.15,
        "MEDIUM": 0.05,
        "LOW": 0.0,
    }.get(sev, 0.0)
    score_delta = 0.1 if final_score >= 80 else (0.05 if final_score >= 65 else 0.0)
    compound_delta = 0.1 if compound_multiplier >= 1.3 else 0.0
    total_delta = severity_delta + score_delta + compound_delta
    hold_threshold = min(0.9, base_hold + total_delta)
    caution_threshold = min(0.95, base_caution + total_delta)
    return hold_threshold, caution_threshold


_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")


def _chunk_supports_claim(claim: str, chunk: dict[str, Any]) -> bool:
    """
    강화된 판정 기준:
    규칙 1. 주장에 '제XX조'가 명시된 경우 청크도 해당 조항을 포함해야 함 (필수)
    규칙 2. 불용어 제거 후 의미 단어 3개 이상 중복 (기존 2개에서 상향)
    규칙 3. 숫자·코드 키워드 추가 가중치
    """
    if not claim or not chunk:
        return False

    claim_lower = (claim or "").lower()
    text_parts = [
        str(chunk.get("chunk_text") or ""),
        str(chunk.get("parent_title") or ""),
        str(chunk.get("article") or chunk.get("regulation_article") or ""),
        str(chunk.get("regulation_clause") or ""),
    ]
    combined = " ".join(text_parts).lower()

    # 규칙 1: 조항 번호 명시 시 청크와 일치 필수
    claim_articles = re.findall(r"제\s*(\d+)\s*조", claim_lower)
    if claim_articles:
        chunk_articles = re.findall(r"제\s*(\d+)\s*조", combined)
        if not any(article in chunk_articles for article in claim_articles):
            return False

    # 규칙 2: 의미 단어 추출 (불용어 제거)
    stop_words = {
        "이", "가", "을", "를", "의", "에", "에서", "으로", "로", "와", "과",
        "이다", "있음", "있다", "수", "하며", "하여", "해당", "필요", "경우",
        "대상", "조항", "기준", "해야", "한다", "되어", "위반", "가능성",
    }
    claim_words = {word for word in _WORD_RE.findall(claim_lower) if len(word) >= 2 and word not in stop_words}
    chunk_words = {word for word in _WORD_RE.findall(combined) if len(word) >= 2 and word not in stop_words}

    if len(claim_words) < 2:
        return bool(chunk)

    overlap = len(claim_words & chunk_words)

    # 규칙 3: 숫자·코드 키워드 가중치
    numeric_bonus = len({word for word in claim_words if any(ch.isdigit() for ch in word)} & chunk_words)
    weighted = overlap + numeric_bonus

    return weighted >= 3


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
