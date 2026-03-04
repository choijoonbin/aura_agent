"""
Phase F: retrieval 고도화 — rerank 확장점, evidence verification 계층, 품질 비교 모드.
1차: cross-encoder rerank (sentence-transformers 선택). 2차: LLM rerank 옵션.
"""
from __future__ import annotations

from typing import Any

# 1차 구현: cross-encoder. 미설치 시 입력 그대로 반환
_CROSS_ENCODER_MODEL: Any = None


def _get_cross_encoder(model_name: str = "cross-encoder/ms-marco-MiniLM-L6-v2"):
    global _CROSS_ENCODER_MODEL
    if _CROSS_ENCODER_MODEL is not None:
        return _CROSS_ENCODER_MODEL
    try:
        from sentence_transformers import CrossEncoder
        _CROSS_ENCODER_MODEL = CrossEncoder(model_name)
        return _CROSS_ENCODER_MODEL
    except Exception:
        return None


def rerank_with_cross_encoder(
    groups: list[dict[str, Any]],
    query: str,
    *,
    model_name: str | None = None,
) -> list[dict[str, Any]]:
    """
    cross-encoder rerank 적용. sentence-transformers 미설치 시 입력 그대로 반환.
    """
    if not groups or not query or not query.strip():
        return groups
    model = _get_cross_encoder(model_name or "cross-encoder/ms-marco-MiniLM-L6-v2")
    if model is None:
        return groups
    try:
        passages = [g.get("chunk_text") or " ".join(g.get("snippets") or []) or "" for g in groups]
        if not any(passages):
            return groups
        pairs = [(query.strip(), p) for p in passages]
        scores = model.predict(pairs)
        for i, g in enumerate(groups):
            g["cross_encoder_score"] = float(scores[i]) if i < len(scores) else 0.0
        return sorted(groups, key=lambda x: x.get("cross_encoder_score", 0), reverse=True)
    except Exception:
        return groups


def verify_evidence_coverage(
    sentences: list[dict[str, Any]],
    retrieved_chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    evidence_verification 모듈 위임. 문장–청크 coverage 검증 및 gate_policy 반환.
    """
    from services.evidence_verification import verify_evidence_coverage as _verify
    return _verify(sentences, retrieved_chunks)


def retrieval_quality_comparison(
    body_evidence: dict[str, Any],
    result_a: list[dict[str, Any]],
    result_b: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    (스텁) 두 retrieval 결과 비교 모드.
    전략/파라미터가 다를 때 품질 지표 비교용.
    """
    return {
        "strategy_a_count": len(result_a),
        "strategy_b_count": len(result_b),
        "comparison_ready": True,
    }
