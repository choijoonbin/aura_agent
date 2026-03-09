"""
Phase F: retrieval 고도화 — rerank 확장점, evidence verification 계층, 품질 비교 모드.
1차: cross-encoder rerank (sentence-transformers 선택). 2차: LLM rerank 옵션.
"""
from __future__ import annotations

import json
from typing import Any

# 1차 구현: cross-encoder. 미설치 시 입력 그대로 반환
_CROSS_ENCODER_MODEL: Any = None

# 한국어 규정 텍스트용 cross-encoder (영어 ms-marco 대체)
_KO_CROSS_ENCODER_MODEL_NAME = "Dongjin-kr/ko-reranker"


def _get_cross_encoder(model_name: str | None = None):
    global _CROSS_ENCODER_MODEL
    if _CROSS_ENCODER_MODEL is not None:
        return _CROSS_ENCODER_MODEL
    target = model_name or _KO_CROSS_ENCODER_MODEL_NAME
    try:
        from sentence_transformers import CrossEncoder
        _CROSS_ENCODER_MODEL = CrossEncoder(target)
        return _CROSS_ENCODER_MODEL
    except Exception:
        return None


def rerank_with_cross_encoder(
    groups: list[dict[str, Any]],
    query: str,
    *,
    model_name: str | None = None,
    batch_size: int = 32,
) -> list[dict[str, Any]]:
    """
    cross-encoder rerank 적용. 한국어 ko-reranker 기본.
    sentence-transformers 미설치 시 입력 그대로 반환하되 cross_encoder_available=False 마킹.
    batch_size: CrossEncoder.predict() 배치 크기 (GPU 메모리에 따라 조정).
    """
    if not groups or not query or not query.strip():
        return groups
    model = _get_cross_encoder(model_name or _KO_CROSS_ENCODER_MODEL_NAME)
    if model is None:
        for g in groups:
            g["cross_encoder_available"] = False
        return groups
    try:
        passages = [g.get("chunk_text") or " ".join(g.get("snippets") or []) or "" for g in groups]
        if not any(passages):
            return groups
        pairs = [(query.strip(), p) for p in passages]
        scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)
        for i, g in enumerate(groups):
            g["cross_encoder_score"] = float(scores[i]) if i < len(scores) else 0.0
            g["cross_encoder_available"] = True
        return sorted(groups, key=lambda x: x.get("cross_encoder_score", 0), reverse=True)
    except Exception:
        return groups


def rerank_with_llm_fallback(
    groups: list[dict[str, Any]],
    query: str,
    *,
    body_evidence: dict[str, Any] | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """
    Cross-Encoder 미설치 시 LLM 기반 경량 Rerank.
    상위 10개 청크를 LLM에 전달해 관련성 순서를 JSON으로 받아 재정렬.
    """
    from utils.config import settings

    if not getattr(settings, "openai_api_key", None) or not groups:
        return groups

    try:
        from openai import AzureOpenAI, OpenAI

        base_url = (getattr(settings, "openai_base_url") or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[: -len("/openai/v1")]
            client = AzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_ep,
                api_version=getattr(settings, "openai_api_version", "2024-02-15-preview"),
            )
        else:
            kw: dict[str, Any] = {"api_key": settings.openai_api_key}
            if base_url:
                kw["base_url"] = base_url
            client = OpenAI(**kw)

        top_groups = groups[:10]
        passages_for_llm = []
        for idx, g in enumerate(top_groups):
            article = g.get("regulation_article") or ""
            parent_title = g.get("parent_title") or ""
            text_preview = (g.get("chunk_text") or "")[:150]
            passages_for_llm.append(f"{idx}: [{article} {parent_title}] {text_preview}")

        system_prompt = (
            "당신은 한국 기업 경비 규정 전문가다.\n"
            "아래 케이스 상황에 대해 규정 조항들의 관련성 순서를 JSON 배열로만 응답하라.\n"
            "형식: {\"order\": [0, 2, 1, 5, 3, ...]} (인덱스 번호, 관련성 높은 순)\n"
            "불필요한 설명, 마크다운 금지."
        )
        user_prompt = (
            f"케이스: {query}\n\n"
            f"규정 조항 목록:\n" + "\n".join(passages_for_llm)
            + "\n\n관련성 높은 순서 (JSON 객체 order 키에 배열):"
        )

        response = client.chat.completions.create(
            model=getattr(settings, "reasoning_llm_model", "gpt-5"),
            max_tokens=100,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        order: list[int] = (
            parsed.get("order", [])
            if isinstance(parsed, dict)
            else (parsed if isinstance(parsed, list) else [])
        )
        order = [int(i) for i in order if isinstance(i, (int, float)) and 0 <= int(i) < len(top_groups)]

        if not order:
            return groups

        reranked = []
        seen: set[int] = set()
        for idx in order:
            i = int(idx)
            if i not in seen:
                item = dict(top_groups[i])
                item["llm_rerank_score"] = len(order) - order.index(idx)
                reranked.append(item)
                seen.add(i)
        for idx, item in enumerate(top_groups):
            if idx not in seen:
                reranked.append(dict(item))
        reranked.extend(groups[10:])
        return reranked

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
