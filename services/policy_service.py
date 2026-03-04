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
    return _dedupe_keep_order(expanded)


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
    keywords = build_policy_keywords(body_evidence)
    if not keywords:
        return []

    effective_date = None
    occurred_at = body_evidence.get("occurredAt")
    if occurred_at:
        try:
            effective_date = date.fromisoformat(str(occurred_at)[:10])
        except Exception:
            effective_date = None

    params: dict[str, Any] = {
        "tenant_id": settings.default_tenant_id,
        "candidate_limit": max(limit * 8, 20),
        "effective_date": effective_date,
    }
    for i, kw in enumerate(keywords[:10]):
        params[f"p{i}"] = f"%{kw.lower()}%"

    sql = text(_build_candidate_sql(min(len(keywords), 10)))
    rows = db.execute(sql, params).mappings().all()

    grouped: dict[tuple[Any, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            row.get("doc_id"),
            str(row.get("regulation_article") or ""),
            str(row.get("parent_title") or ""),
        )
        group = grouped.setdefault(
            key,
            {
                "doc_id": row.get("doc_id"),
                "article": row.get("regulation_article"),
                "clause": row.get("regulation_clause"),
                "parent_title": row.get("parent_title"),
                "version": row.get("version"),
                "effective_from": str(row.get("effective_from")) if row.get("effective_from") else None,
                "effective_to": str(row.get("effective_to")) if row.get("effective_to") else None,
                "chunk_ids": [],
                "snippets": [],
                "lexical_score": 0.0,
            },
        )
        group["chunk_ids"].append(row.get("chunk_id"))
        if row.get("chunk_text"):
            group["snippets"].append(str(row.get("chunk_text")))
        group["lexical_score"] = max(float(group["lexical_score"]), float(row.get("lexical_score") or 0))

    groups = list(grouped.values())
    for group in groups:
        context_rows = _expand_group_context(
            db,
            doc_id=group.get("doc_id"),
            article=group.get("article"),
            parent_title=group.get("parent_title"),
            limit=3,
        )
        context_snippets = [str(row.get("chunk_text") or "") for row in context_rows if row.get("chunk_text")]
        merged_snippets = _dedupe_keep_order(group["snippets"] + context_snippets)
        group["chunk_text"] = " ".join(merged_snippets[:3])
        group["context_chunk_ids"] = [row.get("chunk_id") for row in context_rows if row.get("chunk_id")]
        group["source_strategy"] = "hierarchical_keyword_rerank"

    ranked = _rerank_groups(groups, body_evidence, keywords)
    try:
        from services.retrieval_quality import rerank_with_cross_encoder
        query_str = " ".join(keywords[:12])
        ranked = rerank_with_cross_encoder(ranked, query_str)
    except Exception:
        pass
    results: list[dict[str, Any]] = []
    for group in ranked[:limit]:
        results.append(
            {
                "doc_id": group.get("doc_id"),
                "article": group.get("article"),
                "clause": group.get("clause"),
                "parent_title": group.get("parent_title"),
                "chunk_text": group.get("chunk_text"),
                "version": group.get("version"),
                "effective_from": group.get("effective_from"),
                "effective_to": group.get("effective_to"),
                "chunk_ids": group.get("chunk_ids", []),
                "context_chunk_ids": group.get("context_chunk_ids", []),
                "retrieval_score": group.get("retrieval_score", 0),
                "source_strategy": group.get("source_strategy"),
            }
        )
    return results
