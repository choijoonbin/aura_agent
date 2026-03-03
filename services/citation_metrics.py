"""
Phase F/H: sentence-level citation coverage 및 evidence verification.
규정 근거가 reporter output에 구조적으로 연결되었는지 측정한다.
"""
from __future__ import annotations

from typing import Any


def citation_coverage(reporter_output: dict[str, Any] | None) -> float:
    """
    Reporter output의 문장 중 인용(citation)이 1개 이상 붙은 비율.
    목표: 90% 이상 (공식 4.2 Phase D).
    """
    if not reporter_output:
        return 0.0
    sentences = reporter_output.get("sentences") or []
    if not sentences:
        return 0.0
    with_citation = sum(1 for s in sentences if (s.get("citations") or []))
    return round(with_citation / len(sentences), 4) if sentences else 0.0


def evidence_grounded(sentences: list[dict[str, Any]]) -> bool:
    """문장 목록 중 최소 한 문장이라도 인용이 있으면 True."""
    return any((s.get("citations") or []) for s in sentences)
