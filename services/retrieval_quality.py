"""
Retrieval 품질 고도화 모듈.

포함 범위:
1) Cross-Encoder / LLM fallback rerank
2) 전략별 retrieval 비교 (sparse, dense, hybrid, rerank, query rewrite on/off)
3) Gold dataset 기반 평가 루프 (Recall@k, MRR, nDCG@k)
"""
from __future__ import annotations

from datetime import date
import json
import math
from pathlib import Path
from typing import Any

from utils.llm_azure import completion_kwargs_for_azure

# 1차 구현: cross-encoder. 미설치 시 입력 그대로 반환
_CROSS_ENCODER_MODEL: Any = None

# 한국어 규정 텍스트용 cross-encoder (영어 ms-marco 대체)
_KO_CROSS_ENCODER_MODEL_NAME = "Dongjin-kr/ko-reranker"


# 평가/비교 시 지원 전략
RETRIEVAL_STRATEGIES: tuple[str, ...] = (
    "sparse_only",
    "dense_only",
    "hybrid_rrf",
    "hybrid_rrf_rerank",
    "hybrid_rrf_rerank_rewrite_on",
    "hybrid_rrf_rerank_rewrite_off",
)

# 청킹 관점 비교 모드
CHUNKING_MODES: tuple[str, ...] = (
    "hybrid_hierarchical",  # ARTICLE+CLAUSE+ITEM
    "article_only",         # ARTICLE만
    "clause_only",          # CLAUSE만
)


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

        top_groups = groups[: min(max(1, top_k), 10)]
        passages_for_llm = []
        for idx, g in enumerate(top_groups):
            article = g.get("regulation_article") or g.get("article") or ""
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
            **completion_kwargs_for_azure(
                base_url,
                model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
                max_tokens=100,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            ),
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
            if idx in seen:
                continue
            item = dict(top_groups[idx])
            item["llm_rerank_score"] = len(order) - order.index(idx)
            reranked.append(item)
            seen.add(idx)
        for idx, item in enumerate(top_groups):
            if idx not in seen:
                reranked.append(dict(item))
        reranked.extend(groups[len(top_groups):])
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


def _normalize_token(value: Any) -> str:
    return "".join(str(value or "").split()).upper()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _extract_chunk_id(item: dict[str, Any]) -> int | None:
    raw = item.get("chunk_id")
    if raw is None:
        ids = item.get("chunk_ids") or []
        raw = ids[0] if ids else None
    try:
        return int(raw) if raw is not None else None
    except Exception:
        return None


def _extract_article(item: dict[str, Any]) -> str:
    return str(item.get("article") or item.get("regulation_article") or "")


def _extract_clause(item: dict[str, Any]) -> str:
    return str(item.get("clause") or item.get("regulation_clause") or "")


def _extract_item(item: dict[str, Any]) -> str | None:
    raw = item.get("item")
    if raw is not None:
        return str(raw)
    meta = item.get("metadata_json")
    if isinstance(meta, dict):
        marker = meta.get("regulation_item")
        return str(marker) if marker is not None else None
    if isinstance(meta, str):
        try:
            parsed = json.loads(meta)
            marker = parsed.get("regulation_item") if isinstance(parsed, dict) else None
            return str(marker) if marker is not None else None
        except Exception:
            return None
    return None


def _resolve_effective_date(body_evidence: dict[str, Any]) -> date | None:
    occurred_at = body_evidence.get("occurredAt")
    if not occurred_at:
        return None
    try:
        return date.fromisoformat(str(occurred_at)[:10])
    except Exception:
        return None


def _filter_by_chunking_mode(
    rows: list[dict[str, Any]],
    chunking_mode: str,
) -> list[dict[str, Any]]:
    if chunking_mode == "hybrid_hierarchical":
        return rows
    if chunking_mode == "article_only":
        return [r for r in rows if str(r.get("node_type") or "").upper() == "ARTICLE"]
    if chunking_mode == "clause_only":
        return [r for r in rows if str(r.get("node_type") or "").upper() == "CLAUSE"]
    raise ValueError(f"Unsupported chunking_mode: {chunking_mode}")


def _build_dense_query_for_strategy(
    body_evidence: dict[str, Any],
    *,
    use_query_rewrite: bool,
) -> str:
    from services import policy_service

    if use_query_rewrite:
        return policy_service._build_dense_query(body_evidence)
    rewritten = policy_service.query_rewrite_for_retrieval(body_evidence)
    keywords = rewritten.get("keywords") or policy_service.build_policy_keywords(body_evidence)
    parts = [
        str(rewritten.get("risk_type") or ""),
        str(rewritten.get("merchant_name") or ""),
        str(rewritten.get("hr_status") or ""),
        str(rewritten.get("occurred_at") or ""),
        " ".join(str(v) for v in (rewritten.get("document_line_hints") or []) if v),
        " ".join(str(v) for v in keywords[:12]),
    ]
    query_text = " ".join(p for p in parts if p).strip()
    return query_text or "경비 지출 규정"


def _strategy_specs() -> dict[str, dict[str, Any]]:
    return {
        "sparse_only": {
            "use_bm25": True,
            "use_dense": False,
            "use_rrf": False,
            "use_rerank": False,
            "use_query_rewrite": True,
            "allow_lexical_fallback": False,
        },
        "dense_only": {
            "use_bm25": False,
            "use_dense": True,
            "use_rrf": False,
            "use_rerank": False,
            "use_query_rewrite": True,
            "allow_lexical_fallback": False,
        },
        "hybrid_rrf": {
            "use_bm25": True,
            "use_dense": True,
            "use_rrf": True,
            "use_rerank": False,
            "use_query_rewrite": True,
            "allow_lexical_fallback": True,
        },
        "hybrid_rrf_rerank": {
            "use_bm25": True,
            "use_dense": True,
            "use_rrf": True,
            "use_rerank": True,
            "use_query_rewrite": True,
            "allow_lexical_fallback": True,
        },
        "hybrid_rrf_rerank_rewrite_on": {
            "use_bm25": True,
            "use_dense": True,
            "use_rrf": True,
            "use_rerank": True,
            "use_query_rewrite": True,
            "allow_lexical_fallback": True,
        },
        "hybrid_rrf_rerank_rewrite_off": {
            "use_bm25": True,
            "use_dense": True,
            "use_rrf": True,
            "use_rerank": True,
            "use_query_rewrite": False,
            "allow_lexical_fallback": True,
        },
    }


def _build_retrieval_results(
    fused: list[dict[str, Any]],
    *,
    limit: int,
    selection_stage: str,
    fallback_used: bool,
    fallback_reason: str | None,
    reranker_used: bool,
    reranker_type: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for idx, item in enumerate(fused[:limit], start=1):
        results.append(
            {
                "rank": idx,
                "chunk_id": _extract_chunk_id(item),
                "chunk_ids": [_extract_chunk_id(item)] if _extract_chunk_id(item) is not None else [],
                "doc_id": item.get("doc_id"),
                "article": _extract_article(item),
                "clause": _extract_clause(item),
                "item": _extract_item(item),
                "node_type": item.get("node_type"),
                "parent_title": item.get("parent_title"),
                "chunk_text": item.get("chunk_text"),
                "retrieval_score": item.get("cross_encoder_score") or item.get("rrf_score") or item.get("bm25_score") or item.get("dense_score") or 0,
                "score_detail": {
                    "bm25_score": item.get("bm25_score"),
                    "dense_score": item.get("dense_score"),
                    "rrf_score": item.get("rrf_score"),
                    "cross_encoder_score": item.get("cross_encoder_score"),
                    "llm_rerank_score": item.get("llm_rerank_score"),
                },
                "selection_stage": selection_stage,
                "fallback_used": fallback_used,
                "fallback_reason": fallback_reason,
                "reranker_used": reranker_used,
                "reranker_type": reranker_type,
            }
        )
    return results


def run_retrieval_strategy(
    db: Any,
    body_evidence: dict[str, Any],
    *,
    strategy: str = "hybrid_rrf_rerank",
    chunking_mode: str = "hybrid_hierarchical",
    limit: int = 5,
) -> dict[str, Any]:
    """
    전략별 retrieval 실행.
    - sparse_only
    - dense_only
    - hybrid_rrf
    - hybrid_rrf_rerank
    - hybrid_rrf_rerank_rewrite_on / off
    """
    from services import policy_service

    specs = _strategy_specs()
    if strategy not in specs:
        raise ValueError(f"Unsupported strategy: {strategy}")
    if chunking_mode not in CHUNKING_MODES:
        raise ValueError(f"Unsupported chunking_mode: {chunking_mode}")

    spec = specs[strategy]
    candidate_limit = max(limit * 6, 20)
    effective_date = _resolve_effective_date(body_evidence)
    group_filter = policy_service._get_semantic_group_filter(body_evidence)
    dense_query = _build_dense_query_for_strategy(
        body_evidence,
        use_query_rewrite=bool(spec["use_query_rewrite"]),
    )

    bm25_results: list[dict[str, Any]] = []
    dense_results: list[dict[str, Any]] = []

    if spec["use_bm25"]:
        bm25_results = policy_service._search_bm25_with_group_filter(
            db,
            body_evidence,
            limit=candidate_limit,
            effective_date=effective_date,
            group_filter=group_filter,
        )
        if group_filter and len(bm25_results) < candidate_limit:
            bm25_fallback = policy_service._search_bm25(
                db,
                body_evidence,
                limit=candidate_limit,
                effective_date=effective_date,
            )
            seen = {row.get("chunk_id") for row in bm25_results}
            for row in bm25_fallback:
                cid = row.get("chunk_id")
                if cid not in seen:
                    bm25_results.append(row)
                    seen.add(cid)

    if spec["use_dense"]:
        dense_results = policy_service._search_dense(
            db,
            body_evidence,
            limit=candidate_limit,
            effective_date=effective_date,
            group_filter=group_filter,
            use_hyde=False,
            query_text_override=dense_query,
        )
        if group_filter and len(dense_results) < candidate_limit:
            dense_fallback = policy_service._search_dense(
                db,
                body_evidence,
                limit=candidate_limit,
                effective_date=effective_date,
                group_filter=None,
                use_hyde=False,
                query_text_override=dense_query,
            )
            seen = {row.get("chunk_id") for row in dense_results}
            for row in dense_fallback:
                cid = row.get("chunk_id")
                if cid not in seen:
                    dense_results.append(row)
                    seen.add(cid)

    bm25_results = _filter_by_chunking_mode(bm25_results, chunking_mode)
    dense_results = _filter_by_chunking_mode(dense_results, chunking_mode)

    bm25_weight, dense_weight = policy_service._get_rrf_weights(body_evidence)
    fallback_used = False
    fallback_reason: str | None = None

    if spec["use_rrf"]:
        if bm25_results and dense_results:
            fused = policy_service._reciprocal_rank_fusion(
                bm25_results,
                dense_results,
                k=60,
                bm25_weight=bm25_weight,
                dense_weight=dense_weight,
            )
            selection_stage = "fused_rrf"
        elif bm25_results:
            fused = [dict(r, rrf_score=r.get("bm25_score", 0)) for r in bm25_results]
            fused.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
            selection_stage = "bm25_only"
        elif dense_results:
            fused = [dict(r, rrf_score=r.get("dense_score", 0)) for r in dense_results]
            fused.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
            selection_stage = "dense_only"
        elif spec["allow_lexical_fallback"]:
            fused = policy_service._search_lexical_legacy(
                db,
                body_evidence,
                limit=candidate_limit,
                effective_date=effective_date,
            )
            fused = _filter_by_chunking_mode(fused, chunking_mode)
            for row in fused:
                row.setdefault("rrf_score", row.get("bm25_score", 0))
            selection_stage = "lexical_fallback"
            fallback_used = True
            fallback_reason = "no_bm25_dense_hits"
        else:
            fused = []
            selection_stage = "empty"
            fallback_reason = "no_candidates"
    elif spec["use_bm25"]:
        fused = sorted(bm25_results, key=lambda x: x.get("bm25_score", 0), reverse=True)
        selection_stage = "bm25_only"
    else:
        fused = sorted(dense_results, key=lambda x: x.get("dense_score", 0), reverse=True)
        selection_stage = "dense_only"

    fused = policy_service._enrich_with_parent_context(db, fused[:candidate_limit])

    reranker_used = False
    reranker_type = "none"
    if spec["use_rerank"] and fused:
        rerank_input_limit = min(len(fused), 25)
        rerank_input = [dict(v) for v in fused[:rerank_input_limit]]
        reranked = rerank_with_cross_encoder(rerank_input, dense_query)
        reranked_ids = {row.get("chunk_id") for row in reranked if row.get("chunk_id") is not None}
        tail = [row for row in fused[rerank_input_limit:] if row.get("chunk_id") not in reranked_ids]
        fused = reranked + tail

        if any(row.get("cross_encoder_available") is True for row in reranked):
            reranker_used = True
            reranker_type = "cross_encoder"
            selection_stage = "reranked_cross_encoder"

    results = _build_retrieval_results(
        fused,
        limit=limit,
        selection_stage=selection_stage,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        reranker_used=reranker_used,
        reranker_type=reranker_type,
    )
    return {
        "strategy": strategy,
        "chunking_mode": chunking_mode,
        "candidate_limit": candidate_limit,
        "selection_stage": selection_stage,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "reranker_used": reranker_used,
        "reranker_type": reranker_type,
        "dense_query": dense_query,
        "bm25_count": len(bm25_results),
        "dense_count": len(dense_results),
        "results": results,
    }


def _expected_chunk_ids(gold_example: dict[str, Any]) -> set[int]:
    values = (
        _as_list(gold_example.get("acceptable_chunk_ids"))
        + _as_list(gold_example.get("expected_chunk_ids"))
    )
    out: set[int] = set()
    for value in values:
        try:
            out.add(int(value))
        except Exception:
            continue
    return out


def _expected_articles(gold_example: dict[str, Any]) -> set[str]:
    values = (
        _as_list(gold_example.get("expected_regulation_article"))
        + _as_list(gold_example.get("expected_article"))
        + _as_list(gold_example.get("expected_articles"))
    )
    return {_normalize_token(v) for v in values if str(v).strip()}


def _expected_clauses(gold_example: dict[str, Any]) -> set[str]:
    values = (
        _as_list(gold_example.get("expected_regulation_clause"))
        + _as_list(gold_example.get("expected_clause"))
        + _as_list(gold_example.get("expected_clauses"))
    )
    return {_normalize_token(v) for v in values if str(v).strip()}


def _expected_relevant_count(gold_example: dict[str, Any]) -> int:
    chunk_ids = _expected_chunk_ids(gold_example)
    articles = _expected_articles(gold_example)
    clauses = _expected_clauses(gold_example)
    if chunk_ids:
        return len(chunk_ids)
    if articles:
        return len(articles)
    if clauses:
        return len(clauses)
    return 1


def _is_relevant(
    item: dict[str, Any],
    gold_example: dict[str, Any],
) -> tuple[bool, str | None]:
    chunk_id = _extract_chunk_id(item)
    article = _normalize_token(_extract_article(item))
    clause = _normalize_token(_extract_clause(item))

    chunk_ids = _expected_chunk_ids(gold_example)
    if chunk_id is not None and chunk_id in chunk_ids:
        return True, f"chunk:{chunk_id}"

    articles = _expected_articles(gold_example)
    clauses = _expected_clauses(gold_example)
    if article and articles and article in articles:
        if clauses:
            if clause and clause in clauses:
                return True, f"article_clause:{article}:{clause}"
            return False, None
        return True, f"article:{article}"

    return False, None


def compute_retrieval_metrics_for_case(
    retrieved: list[dict[str, Any]],
    gold_example: dict[str, Any],
    *,
    top_ks: tuple[int, ...] = (3, 5),
    ndcg_k: int = 5,
) -> dict[str, Any]:
    """
    단일 케이스 기준 retrieval 지표 계산.
    반환:
      recall@k, mrr, ndcg@k
    """
    expected_count = _expected_relevant_count(gold_example)

    metrics: dict[str, Any] = {}
    for k in top_ks:
        matched: set[str] = set()
        for item in retrieved[:k]:
            ok, key = _is_relevant(item, gold_example)
            if ok and key is not None:
                matched.add(key)
        metrics[f"recall@{k}"] = round(min(1.0, len(matched) / max(1, expected_count)), 6)

    first_rank = 0
    for idx, item in enumerate(retrieved, start=1):
        ok, _ = _is_relevant(item, gold_example)
        if ok:
            first_rank = idx
            break
    metrics["mrr"] = round(1.0 / first_rank, 6) if first_rank > 0 else 0.0

    rel_vector: list[int] = []
    for item in retrieved[:ndcg_k]:
        ok, _ = _is_relevant(item, gold_example)
        rel_vector.append(1 if ok else 0)

    dcg = 0.0
    for idx, rel in enumerate(rel_vector, start=1):
        if rel <= 0:
            continue
        dcg += rel / math.log2(idx + 1)
    ideal_rels = [1] * min(expected_count, ndcg_k)
    idcg = 0.0
    for idx, rel in enumerate(ideal_rels, start=1):
        idcg += rel / math.log2(idx + 1)
    metrics[f"ndcg@{ndcg_k}"] = round((dcg / idcg) if idcg > 0 else 0.0, 6)
    return metrics


def _priority_weight(value: Any) -> float:
    if value is None:
        return 1.0
    try:
        numeric = float(value)
        if numeric > 0:
            return numeric
    except Exception:
        pass
    token = str(value).strip().upper()
    mapping = {"P0": 3.0, "HIGH": 2.0, "MEDIUM": 1.5, "LOW": 1.0}
    return mapping.get(token, 1.0)


def _build_body_evidence_from_gold(gold_example: dict[str, Any]) -> dict[str, Any]:
    body = dict(gold_example.get("body_evidence") or {})
    case_type = str(gold_example.get("case_type") or body.get("case_type") or "").strip()
    query = str(gold_example.get("query") or "").strip()
    if case_type and "case_type" not in body:
        body["case_type"] = case_type
    if query:
        extra = list(body.get("_extra_keywords") or [])
        if query not in extra:
            extra.append(query)
        body["_extra_keywords"] = extra
    return body


def evaluate_gold_dataset(
    db: Any,
    gold_examples: list[dict[str, Any]],
    *,
    strategies: list[str] | None = None,
    chunking_modes: list[str] | None = None,
    top_ks: tuple[int, ...] = (3, 5),
    ndcg_k: int = 5,
) -> dict[str, Any]:
    """
    Gold dataset 기반 retrieval 평가 루프.
    - Recall@k
    - MRR
    - nDCG@k
    """
    chosen_strategies = strategies or list(RETRIEVAL_STRATEGIES)
    chosen_chunking_modes = chunking_modes or ["hybrid_hierarchical"]
    eval_limit = max(max(top_ks), ndcg_k)

    reports: list[dict[str, Any]] = []
    for strategy in chosen_strategies:
        for chunking_mode in chosen_chunking_modes:
            cases: list[dict[str, Any]] = []
            for idx, gold in enumerate(gold_examples):
                body = _build_body_evidence_from_gold(gold)
                run = run_retrieval_strategy(
                    db,
                    body,
                    strategy=strategy,
                    chunking_mode=chunking_mode,
                    limit=eval_limit,
                )
                metrics = compute_retrieval_metrics_for_case(
                    run.get("results") or [],
                    gold,
                    top_ks=top_ks,
                    ndcg_k=ndcg_k,
                )
                case_id = (
                    gold.get("id")
                    or gold.get("voucher_key")
                    or gold.get("case_id")
                    or f"case_{idx+1}"
                )
                weight = _priority_weight(gold.get("priority"))
                cases.append(
                    {
                        "case_id": case_id,
                        "priority": gold.get("priority"),
                        "weight": weight,
                        "metrics": metrics,
                        "selection_stage": run.get("selection_stage"),
                        "reranker_used": run.get("reranker_used"),
                        "fallback_used": run.get("fallback_used"),
                        "top_result": (run.get("results") or [None])[0],
                    }
                )

            denominator = max(1, len(cases))
            weight_sum = sum(float(c.get("weight") or 1.0) for c in cases) or 1.0
            summary: dict[str, Any] = {"case_count": len(cases)}
            for k in top_ks:
                key = f"recall@{k}"
                mean_value = sum(c["metrics"].get(key, 0.0) for c in cases) / denominator
                weighted_value = sum(c["metrics"].get(key, 0.0) * float(c.get("weight") or 1.0) for c in cases) / weight_sum
                summary[key] = round(mean_value, 6)
                summary[f"{key}_weighted"] = round(weighted_value, 6)

            mrr = sum(c["metrics"].get("mrr", 0.0) for c in cases) / denominator
            mrr_w = sum(c["metrics"].get("mrr", 0.0) * float(c.get("weight") or 1.0) for c in cases) / weight_sum
            ndcg_key = f"ndcg@{ndcg_k}"
            ndcg = sum(c["metrics"].get(ndcg_key, 0.0) for c in cases) / denominator
            ndcg_w = sum(c["metrics"].get(ndcg_key, 0.0) * float(c.get("weight") or 1.0) for c in cases) / weight_sum
            summary["mrr"] = round(mrr, 6)
            summary["mrr_weighted"] = round(mrr_w, 6)
            summary[ndcg_key] = round(ndcg, 6)
            summary[f"{ndcg_key}_weighted"] = round(ndcg_w, 6)

            reports.append(
                {
                    "strategy": strategy,
                    "chunking_mode": chunking_mode,
                    "summary": summary,
                    "cases": cases,
                }
            )

    return {
        "dataset_size": len(gold_examples),
        "strategies": chosen_strategies,
        "chunking_modes": chosen_chunking_modes,
        "top_ks": list(top_ks),
        "ndcg_k": ndcg_k,
        "reports": reports,
    }


def compare_retrieval_strategies(
    db: Any,
    body_evidence: dict[str, Any],
    *,
    strategies: list[str] | None = None,
    chunking_mode: str = "hybrid_hierarchical",
    limit: int = 5,
) -> dict[str, Any]:
    """
    단일 케이스에서 전략별 결과를 비교한다.
    """
    chosen_strategies = strategies or list(RETRIEVAL_STRATEGIES)
    runs: list[dict[str, Any]] = []
    for strategy in chosen_strategies:
        run = run_retrieval_strategy(
            db,
            body_evidence,
            strategy=strategy,
            chunking_mode=chunking_mode,
            limit=limit,
        )
        top = (run.get("results") or [None])[0]
        runs.append(
            {
                "strategy": strategy,
                "chunking_mode": chunking_mode,
                "result_count": len(run.get("results") or []),
                "selection_stage": run.get("selection_stage"),
                "fallback_used": run.get("fallback_used"),
                "reranker_used": run.get("reranker_used"),
                "reranker_type": run.get("reranker_type"),
                "top_result": top,
                "results": run.get("results") or [],
            }
        )

    return {
        "comparison_ready": True,
        "run_count": len(runs),
        "runs": runs,
    }


def load_gold_dataset(path: str | Path) -> list[dict[str, Any]]:
    """
    gold dataset 로더.
    지원 형식:
    - .json: list[object]
    - .jsonl: line-delimited JSON objects
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Dataset file not found: {p}")

    if p.suffix.lower() == ".jsonl":
        out: list[dict[str, Any]] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            row = line.strip()
            if not row:
                continue
            parsed = json.loads(row)
            if not isinstance(parsed, dict):
                raise ValueError("Each JSONL row must be an object")
            out.append(parsed)
        return out

    raw = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        if not all(isinstance(v, dict) for v in raw):
            raise ValueError("JSON dataset must be list[object]")
        return raw
    raise ValueError("Unsupported dataset format: expected .json(list) or .jsonl")


def retrieval_quality_comparison(
    body_evidence: dict[str, Any],
    result_a: list[dict[str, Any]],
    result_b: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    하위 호환용 비교 함수.
    기존 count 비교를 유지하면서, top-k overlap 지표를 추가한다.
    """
    ids_a = [_extract_chunk_id(v) for v in result_a]
    ids_b = [_extract_chunk_id(v) for v in result_b]
    set_a = {v for v in ids_a if v is not None}
    set_b = {v for v in ids_b if v is not None}
    overlap = len(set_a & set_b)
    union = len(set_a | set_b)
    jaccard = (overlap / union) if union > 0 else 0.0
    return {
        "strategy_a_count": len(result_a),
        "strategy_b_count": len(result_b),
        "overlap_count": overlap,
        "jaccard_topk": round(jaccard, 6),
        "comparison_ready": True,
        "case_type": body_evidence.get("case_type"),
    }
