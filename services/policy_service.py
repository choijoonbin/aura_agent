from __future__ import annotations

from datetime import date
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from utils.config import settings


KEYWORD_HINTS: dict[str, list[str]] = {
    "HOLIDAY_USAGE": ["휴일", "주말", "공휴일", "식대", "심야"],
    "LIMIT_EXCEED": ["한도", "초과", "금액"],
    "PRIVATE_USE_RISK": ["사적", "개인", "업무관련성"],
}

TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")

FIELD_WEIGHTS = {
    "chunk_text": 3,
    "parent_title": 5,
    "regulation_article": 7,
    "regulation_clause": 4,
}


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _tokenize(value: Any) -> list[str]:
    text = _normalize_text(value)
    return [token for token in TOKEN_RE.findall(text) if len(token) >= 2]


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def build_policy_keywords(body_evidence: dict[str, Any]) -> list[str]:
    case_type = str(body_evidence.get("case_type") or body_evidence.get("intended_risk_type") or "")
    keywords = list(KEYWORD_HINTS.get(case_type, []))

    merchant = str(body_evidence.get("merchantName") or "").strip()
    expense_type = str(body_evidence.get("expenseType") or "").strip()
    expense_type_name = str(body_evidence.get("expenseTypeName") or "").strip()
    mcc = str(body_evidence.get("mccCode") or "").strip()
    mcc_name = str(body_evidence.get("mccName") or "").strip()
    if body_evidence.get("isHoliday"):
        keywords.extend(["휴일", "주말"])
    if merchant:
        keywords.append(merchant)
    if expense_type:
        keywords.append(expense_type)
    if expense_type_name:
        keywords.append(expense_type_name)
    if mcc:
        keywords.append(mcc)
    if mcc_name:
        keywords.append(mcc_name)

    document = body_evidence.get("document") or {}
    for item in (document.get("items") or [])[:3]:
        if item.get("sgtxt"):
            keywords.append(str(item["sgtxt"]))
        if item.get("hkont"):
            keywords.append(str(item["hkont"]))

    expanded: list[str] = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        expanded.append(kw)
        expanded.extend(_tokenize(kw))
    keywords = _dedupe_keep_order(expanded)

    if body_evidence.get("_enriched_holidayRisk"):
        for kw in ["휴일", "주말", "공휴일"]:
            if kw not in keywords:
                keywords.append(kw)

    for kw in (body_evidence.get("_extra_keywords") or []):
        token = str(kw).strip()
        if token and token not in keywords:
            keywords.append(token)

    return keywords


def query_rewrite_for_retrieval(body_evidence: dict[str, Any]) -> dict[str, Any]:
    """
    Phase F: retrieval용 구조화 쿼리. query rewrite(risk_type, mcc, hr_status, occurredAt, document evidence) 반영.
    hierarchical retrieval / rerank 단계에서 사용할 수 있도록 동일 입력을 반환한다.
    """
    risk_type = str(body_evidence.get("case_type") or body_evidence.get("intended_risk_type") or "")
    keywords = build_policy_keywords(body_evidence)
    doc = body_evidence.get("document") or {}
    items = doc.get("items") or []
    line_hints = []
    for item in items[:3]:
        if item.get("sgtxt"):
            line_hints.append(str(item["sgtxt"]))
        if item.get("hkont"):
            line_hints.append(str(item["hkont"]))
    return {
        "risk_type": risk_type,
        "keywords": keywords,
        "mcc_code": body_evidence.get("mccCode"),
        "mcc_name": body_evidence.get("mccName"),
        "hr_status": body_evidence.get("hrStatus") or body_evidence.get("hrStatusRaw"),
        "occurred_at": body_evidence.get("occurredAt"),
        "is_holiday": bool(body_evidence.get("isHoliday")),
        "document_line_hints": line_hints,
        "merchant_name": body_evidence.get("merchantName"),
    }


def _build_candidate_sql(keyword_count: int) -> str:
    score_terms: list[str] = []
    filters: list[str] = []
    for i in range(keyword_count):
        key = f"p{i}"
        for field, weight in FIELD_WEIGHTS.items():
            score_terms.append(f"(case when lower(coalesce({field}, '')) like :{key} then {weight} else 0 end)")
        filters.append(f"lower(coalesce(chunk_text, '')) like :{key}")
        filters.append(f"lower(coalesce(parent_title, '')) like :{key}")
        filters.append(f"lower(coalesce(regulation_article, '')) like :{key}")
        filters.append(f"lower(coalesce(regulation_clause, '')) like :{key}")
    score_expr = " + ".join(score_terms) if score_terms else "0"
    where_kw = " or ".join(filters) if filters else "1=1"
    return f"""
        select
            chunk_id,
            doc_id,
            regulation_article,
            regulation_clause,
            parent_title,
            chunk_text,
            search_text,
            node_type,
            parent_id,
            version,
            effective_from,
            effective_to,
            page_no,
            chunk_index,
            parent_chunk_id,
            child_index,
            ({score_expr}) as lexical_score
        from dwp_aura.rag_chunk
        where tenant_id = :tenant_id
          and is_active = true
          and (
                :effective_date is null
                or coalesce(effective_from, :effective_date) <= :effective_date
              )
          and (
                :effective_date is null
                or coalesce(effective_to, :effective_date) >= :effective_date
              )
          and ({where_kw})
        order by lexical_score desc, chunk_id desc
        limit :candidate_limit
    """


def _expand_group_context(db: Session, doc_id: int, article: str | None, parent_title: str | None, limit: int = 3) -> list[dict[str, Any]]:
    if not doc_id:
        return []
    params = {
        "tenant_id": settings.default_tenant_id,
        "doc_id": doc_id,
        "article": article,
        "parent_title": parent_title,
        "limit": limit,
    }
    sql = text(
        """
        select
            chunk_id,
            chunk_text,
            regulation_article,
            regulation_clause,
            parent_title,
            page_no,
            chunk_index
        from dwp_aura.rag_chunk
        where tenant_id = :tenant_id
          and is_active = true
          and doc_id = :doc_id
          and (
                (:article is not null and regulation_article = :article)
                or (:parent_title is not null and parent_title = :parent_title)
              )
        order by page_no nulls last, chunk_index nulls last, chunk_id asc
        limit :limit
        """
    )
    return [dict(row) for row in db.execute(sql, params).mappings().all()]


# ─────────────────────────────────────────────────────────────────────────────
# Hybrid 검색: BM25 (tsvector) + Dense (pgvector) + RRF
# ─────────────────────────────────────────────────────────────────────────────


def _search_bm25(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    limit: int = 20,
    effective_date: Any = None,
) -> list[dict[str, Any]]:
    """BM25 검색: search_tsv GIN 인덱스 활용."""
    keywords = build_policy_keywords(body_evidence)
    if not keywords:
        return []

    # tsquery: simple config, prefix match. 공백 포함 키워드는 토큰으로 쪼개서 단일 토큰만 "t:*" 형태로 전달 (구문 오류 방지)
    TSQUERY_OPERATORS = set("&|!():*")
    ts_terms: list[str] = []
    seen: set[str] = set()
    for kw in keywords[:20]:
        if not kw or not str(kw).strip():
            continue
        for part in str(kw).strip().split():
            token = part.strip()
            if len(token) < 2:
                continue
            if any(c in token for c in TSQUERY_OPERATORS):
                continue
            safe = token.replace("'", "''")
            if safe in seen:
                continue
            seen.add(safe)
            ts_terms.append(f"{safe}:*")
    if not ts_terms:
        return []
    ts_query = " | ".join(ts_terms[:30])

    sql = text("""
        SELECT
            chunk_id, doc_id, regulation_article, regulation_clause,
            parent_title, chunk_text, search_text, node_type, parent_id,
            version, effective_from, effective_to, page_no, chunk_index,
            ts_rank_cd(search_tsv, query) AS bm25_score
        FROM dwp_aura.rag_chunk,
             to_tsquery('simple', :ts_query) AS query
        WHERE tenant_id = :tenant_id
          AND is_active = true
          AND search_tsv @@ query
          AND (:effective_date IS NULL OR coalesce(effective_from, :effective_date) <= :effective_date)
          AND (:effective_date IS NULL OR coalesce(effective_to, :effective_date) >= :effective_date)
        ORDER BY bm25_score DESC
        LIMIT :limit
    """)
    rows = db.execute(
        sql,
        {
            "tenant_id": settings.default_tenant_id,
            "ts_query": ts_query,
            "effective_date": effective_date,
            "limit": limit,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _search_dense(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    limit: int = 20,
    effective_date: Any = None,
    embed_column: str = settings.rag_embedding_column,
) -> list[dict[str, Any]]:
    """Dense 벡터 검색: pgvector <=> (cosine distance)."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(embed_column or "")):
        return []
    cast_type = str(settings.rag_embedding_cast_type or "halfvec").strip().lower()
    if cast_type not in {"vector", "halfvec"}:
        cast_type = "halfvec"
    try:
        from services.chunking_pipeline import embed_texts
    except ImportError:
        return []

    keywords = build_policy_keywords(body_evidence)
    case_type = body_evidence.get("case_type") or ""
    merchant = body_evidence.get("merchantName") or ""
    query_text = f"{case_type} {merchant} {' '.join(keywords[:10])}".strip()
    if not query_text:
        return []

    vectors = embed_texts([query_text])
    if not vectors:
        return []
    query_vector = vectors[0]

    sql = text(f"""
        SELECT
            chunk_id, doc_id, regulation_article, regulation_clause,
            parent_title, chunk_text, search_text, node_type, parent_id,
            version, effective_from, effective_to, page_no, chunk_index,
            1 - ({embed_column} <=> CAST(:query_vec AS {cast_type})) AS dense_score
        FROM dwp_aura.rag_chunk
        WHERE tenant_id = :tenant_id
          AND is_active = true
          AND {embed_column} IS NOT NULL
          AND (:effective_date IS NULL OR coalesce(effective_from, :effective_date) <= :effective_date)
          AND (:effective_date IS NULL OR coalesce(effective_to, :effective_date) >= :effective_date)
        ORDER BY {embed_column} <=> CAST(:query_vec AS {cast_type})
        LIMIT :limit
    """)
    rows = db.execute(
        sql,
        {
            "tenant_id": settings.default_tenant_id,
            "query_vec": str(query_vector),
            "effective_date": effective_date,
            "limit": limit,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _reciprocal_rank_fusion(
    bm25_results: list[dict[str, Any]],
    dense_results: list[dict[str, Any]],
    *,
    k: int = 60,
    bm25_weight: float = 0.5,
    dense_weight: float = 0.5,
) -> list[dict[str, Any]]:
    """RRF: 순위 기반 융합. RRF_score(d) = Σ weight / (k + rank(d))."""
    scores: dict[int, float] = {}
    chunk_data: dict[int, dict[str, Any]] = {}

    for rank, item in enumerate(bm25_results, start=1):
        cid = item.get("chunk_id")
        if cid is None:
            continue
        scores[cid] = scores.get(cid, 0.0) + bm25_weight / (k + rank)
        chunk_data[cid] = {**item, "bm25_rank": rank, "bm25_score": item.get("bm25_score", 0)}

    for rank, item in enumerate(dense_results, start=1):
        cid = item.get("chunk_id")
        if cid is None:
            continue
        scores[cid] = scores.get(cid, 0.0) + dense_weight / (k + rank)
        if cid not in chunk_data:
            chunk_data[cid] = item
        chunk_data[cid]["dense_rank"] = rank
        chunk_data[cid]["dense_score"] = item.get("dense_score", 0)

    ranked = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)
    result = []
    for cid in ranked:
        item = dict(chunk_data[cid])
        item["rrf_score"] = round(scores[cid], 6)
        result.append(item)
    return result


def _enrich_with_parent_context(
    db: Session,
    chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """CLAUSE 노드에 부모 ARTICLE 청크 맥락 prepend."""
    parent_ids = [
        c.get("parent_id")
        for c in chunks
        if c.get("node_type") == "CLAUSE" and c.get("parent_id")
    ]
    if not parent_ids:
        return chunks

    parent_sql = text("""
        SELECT chunk_id, chunk_text, regulation_article, parent_title
        FROM dwp_aura.rag_chunk
        WHERE chunk_id = ANY(:ids) AND tenant_id = :tid
    """)
    rows = db.execute(
        parent_sql,
        {"ids": parent_ids, "tid": settings.default_tenant_id},
    ).mappings().all()
    parent_map = {row["chunk_id"]: dict(row) for row in rows}

    enriched = []
    for chunk in chunks:
        if chunk.get("node_type") == "CLAUSE" and chunk.get("parent_id") in parent_map:
            parent = parent_map[chunk["parent_id"]]
            chunk = dict(chunk)
            chunk["context_chunk_ids"] = [parent["chunk_id"]]
            if chunk.get("chunk_text") and parent.get("parent_title") and not chunk["chunk_text"].strip().startswith("["):
                chunk["chunk_text"] = (
                    f"[{parent.get('regulation_article', '')} {parent.get('parent_title', '')}] "
                    + chunk["chunk_text"]
                )
        else:
            chunk = dict(chunk)
            if "context_chunk_ids" not in chunk:
                chunk["context_chunk_ids"] = []
        enriched.append(chunk)
    return enriched


def _search_lexical_legacy(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    limit: int = 20,
    effective_date: Any = None,
) -> list[dict[str, Any]]:
    """기존 LIKE 기반 검색 (fallback). BM25/Dense 모두 실패 시 사용."""
    keywords = build_policy_keywords(body_evidence)
    if not keywords:
        return []

    params: dict[str, Any] = {
        "tenant_id": settings.default_tenant_id,
        "candidate_limit": limit,
        "effective_date": effective_date,
    }
    for i, kw in enumerate(keywords[:10]):
        params[f"p{i}"] = f"%{kw.lower()}%"

    sql = text(_build_candidate_sql(min(len(keywords), 10)))
    rows = db.execute(sql, params).mappings().all()

    out = []
    for row in rows:
        d = dict(row)
        d["bm25_score"] = d.get("lexical_score", 0)
        out.append(d)
    return out


def _rerank_groups(groups: list[dict[str, Any]], body_evidence: dict[str, Any], keywords: list[str]) -> list[dict[str, Any]]:
    is_holiday = bool(body_evidence.get("isHoliday"))
    line_text = " ".join(
        str(item.get("sgtxt") or "")
        for item in ((body_evidence.get("document") or {}).get("items") or [])[:5]
    ).lower()
    for group in groups:
        article = _normalize_text(group.get("article"))
        parent_title = _normalize_text(group.get("parent_title"))
        merged_text = _normalize_text(group.get("chunk_text"))
        score = float(group.get("lexical_score") or 0)
        if is_holiday and any(term in merged_text for term in ("휴일", "주말", "공휴일")):
            score += 20
        if any(term in merged_text for term in ("식대", "접대비")) and any(term in line_text for term in ("식대", "주점", "식당", "막걸리", "술")):
            score += 10
        if any(kw.lower() in parent_title or kw.lower() in article for kw in keywords[:8]):
            score += 8
        if article:
            score += 2
        group["retrieval_score"] = round(score, 2)
    return sorted(groups, key=lambda item: item.get("retrieval_score", 0), reverse=True)


def search_policy_chunks(db: Session, body_evidence: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    """
    메인 검색 함수 (기존 인터페이스 유지).

    파이프라인: BM25 → Dense → RRF 융합 → Contextual 보강 → Cross-Encoder 재정렬.
    BM25/Dense 모두 실패 시 LIKE 기반 legacy fallback.
    """
    effective_date = None
    occurred_at = body_evidence.get("occurredAt")
    if occurred_at:
        try:
            effective_date = date.fromisoformat(str(occurred_at)[:10])
        except Exception:
            pass

    candidate_limit = max(limit * 6, 20)

    bm25_results = _search_bm25(db, body_evidence, limit=candidate_limit, effective_date=effective_date)
    dense_results = _search_dense(db, body_evidence, limit=candidate_limit, effective_date=effective_date)

    if bm25_results and dense_results:
        fused = _reciprocal_rank_fusion(bm25_results, dense_results, k=60)
    elif bm25_results:
        fused = [dict(r, rrf_score=r.get("bm25_score", 0)) for r in bm25_results]
        fused.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
    elif dense_results:
        fused = [dict(r, rrf_score=r.get("dense_score", 0)) for r in dense_results]
        fused.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
    else:
        fused = _search_lexical_legacy(db, body_evidence, limit=candidate_limit, effective_date=effective_date)
        for r in fused:
            r.setdefault("rrf_score", r.get("bm25_score", 0))

    enriched = _enrich_with_parent_context(db, fused[:candidate_limit])

    keywords = build_policy_keywords(body_evidence)
    query_str = " ".join(keywords[:12])
    try:
        from services.retrieval_quality import rerank_with_cross_encoder
        enriched = rerank_with_cross_encoder(enriched, query_str)
    except Exception:
        pass

    results = []
    for item in enriched[:limit]:
        results.append({
            "doc_id": item.get("doc_id"),
            "article": item.get("regulation_article"),
            "clause": item.get("regulation_clause"),
            "parent_title": item.get("parent_title"),
            "chunk_text": item.get("chunk_text"),
            "version": item.get("version"),
            "effective_from": str(item.get("effective_from")) if item.get("effective_from") else None,
            "effective_to": str(item.get("effective_to")) if item.get("effective_to") else None,
            "chunk_ids": [item.get("chunk_id")] if item.get("chunk_id") is not None else [],
            "context_chunk_ids": item.get("context_chunk_ids", []),
            "retrieval_score": item.get("cross_encoder_score") or item.get("rrf_score") or item.get("bm25_score") or 0,
            "source_strategy": "hybrid_bm25_dense_rrf",
            "score_detail": {
                "bm25_score": item.get("bm25_score"),
                "dense_score": item.get("dense_score"),
                "rrf_score": item.get("rrf_score"),
                "cross_encoder_score": item.get("cross_encoder_score"),
            },
        })
    return results
