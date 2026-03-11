from __future__ import annotations

from datetime import date
import json
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.chunking_pipeline import _embedding_column_exists
from utils.config import settings
from utils.llm_azure import completion_kwargs_for_azure


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
    # 규정 제14조: 모든 경비 지출은 증빙 구비 의무. case_type 무관하게 공통 증빙 관련 조항 검색에 포함.
    for kw in ["증빙", "공통", "제14조", "필수 증빙"]:
        if kw not in keywords:
            keywords.append(kw)

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

    try:
        from services.rag_chunk_lab_service import _SYNONYM_MAP
        for kw in list(keywords):
            for canonical, synonyms in _SYNONYM_MAP.items():
                if kw == canonical or kw in synonyms:
                    for s in [canonical] + synonyms:
                        if s not in keywords:
                            keywords.append(s)
                    break
    except ImportError:
        pass

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
            metadata_json,
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


def _get_rrf_weights(body_evidence: dict[str, Any]) -> tuple[float, float]:
    """
    케이스 유형에 따라 BM25/Dense RRF 가중치 동적 결정.
    반환: (bm25_weight, dense_weight)
    """
    case_type = str(
        body_evidence.get("case_type") or body_evidence.get("intended_risk_type") or ""
    )
    _CASE_WEIGHTS: dict[str, tuple[float, float]] = {
        "HOLIDAY_USAGE": (0.65, 0.35),
        "LIMIT_EXCEED": (0.45, 0.55),
        "PRIVATE_USE_RISK": (0.50, 0.50),
        "UNUSUAL_PATTERN": (0.35, 0.65),
    }
    bm25_w, dense_w = _CASE_WEIGHTS.get(case_type, (0.50, 0.50))
    if body_evidence.get("_regulation_article_hint"):
        bm25_w = min(0.80, bm25_w + 0.15)
        dense_w = 1.0 - bm25_w
    occurred_at = str(body_evidence.get("occurredAt") or "")
    try:
        if len(occurred_at) >= 13:
            hour = int(occurred_at[11:13])
            if hour >= 22 or hour < 6:
                dense_w = min(0.70, dense_w + 0.10)
                bm25_w = 1.0 - dense_w
    except Exception:
        pass
    return round(bm25_w, 2), round(dense_w, 2)


def _get_semantic_group_filter(body_evidence: dict[str, Any]) -> list[str] | None:
    """
    케이스 유형에 따라 검색할 장(章) semantic_group 패턴 목록 반환.
    None이면 전체 검색.
    """
    case_type = str(body_evidence.get("case_type") or "")
    _CASE_GROUP_HINTS: dict[str, list[str]] = {
        "HOLIDAY_USAGE": ["제7장", "제8장", "제3장"],
        "LIMIT_EXCEED": ["제8장", "제3장"],
        "PRIVATE_USE_RISK": ["제7장", "제8장", "제4장"],
        "UNUSUAL_PATTERN": ["제8장", "제10장", "제12장"],
    }
    return _CASE_GROUP_HINTS.get(case_type)


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

    use_search_tokens = _embedding_column_exists(db, "search_tokens")
    if use_search_tokens:
        sql = text("""
            SELECT
                chunk_id, doc_id, regulation_article, regulation_clause,
                parent_title, chunk_text, search_text, node_type, parent_id,
                version, effective_from, effective_to, page_no, chunk_index,
                metadata_json,
                ts_rank_cd(
                    setweight(search_tsv, 'A') || setweight(to_tsvector('simple', coalesce(search_tokens, '')), 'B'),
                    query
                ) AS bm25_score
            FROM dwp_aura.rag_chunk,
                 to_tsquery('simple', :ts_query) AS query
            WHERE tenant_id = :tenant_id
              AND is_active = true
              AND (search_tsv @@ query OR to_tsvector('simple', coalesce(search_tokens, '')) @@ query)
              AND (:effective_date IS NULL OR coalesce(effective_from, :effective_date) <= :effective_date)
              AND (:effective_date IS NULL OR coalesce(effective_to, :effective_date) >= :effective_date)
            ORDER BY bm25_score DESC
            LIMIT :limit
        """)
    else:
        sql = text("""
            SELECT
                chunk_id, doc_id, regulation_article, regulation_clause,
                parent_title, chunk_text, search_text, node_type, parent_id,
                version, effective_from, effective_to, page_no, chunk_index,
                metadata_json,
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


def _search_bm25_with_group_filter(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    limit: int = 20,
    effective_date: Any = None,
    group_filter: list[str] | None = None,
) -> list[dict[str, Any]]:
    """BM25 검색 + semantic_group 필터(장/절). group_filter가 있으면 해당 패턴 내에서만 검색."""
    keywords = build_policy_keywords(body_evidence)
    if not keywords:
        return []

    TSQUERY_OPERATORS = set("&|!():*")
    ts_terms = []
    seen = set()
    for kw in keywords[:20]:
        if not kw or not str(kw).strip():
            continue
        for part in str(kw).strip().split():
            token = part.strip()
            if len(token) < 2 or any(c in token for c in TSQUERY_OPERATORS):
                continue
            safe = token.replace("'", "''")
            if safe in seen:
                continue
            seen.add(safe)
            ts_terms.append(f"{safe}:*")
    if not ts_terms:
        return []
    ts_query = " | ".join(ts_terms[:30])

    group_filter_sql = ""
    group_params: dict[str, str] = {}
    if group_filter:
        conditions = [
            f"(metadata_json->>'semantic_group' LIKE :grp{i})"
            for i in range(len(group_filter))
        ]
        group_filter_sql = " AND (" + " OR ".join(conditions) + ")"
        for i, grp in enumerate(group_filter):
            group_params[f"grp{i}"] = f"{grp}%"

    use_search_tokens = _embedding_column_exists(db, "search_tokens")
    if use_search_tokens:
        rank_expr = "ts_rank_cd(setweight(search_tsv, 'A') || setweight(to_tsvector('simple', coalesce(search_tokens, '')), 'B'), query)"
        where_match = "(search_tsv @@ query OR to_tsvector('simple', coalesce(search_tokens, '')) @@ query)"
    else:
        rank_expr = "ts_rank_cd(search_tsv, query)"
        where_match = "search_tsv @@ query"

    sql = text(
        """
        SELECT
            chunk_id, doc_id, regulation_article, regulation_clause,
            parent_title, chunk_text, search_text, node_type, parent_id,
            version, effective_from, effective_to, page_no, chunk_index,
            metadata_json,
            """
        + rank_expr
        + """ AS bm25_score
        FROM dwp_aura.rag_chunk,
             to_tsquery('simple', :ts_query) AS query
        WHERE tenant_id = :tenant_id
          AND is_active = true
          AND """
        + where_match
        + """
          AND (:effective_date IS NULL OR coalesce(effective_from, :effective_date) <= :effective_date)
          AND (:effective_date IS NULL OR coalesce(effective_to, :effective_date) >= :effective_date)
        """
        + group_filter_sql
        + """
        ORDER BY bm25_score DESC
        LIMIT :limit
        """
    )
    params = {
        "tenant_id": settings.default_tenant_id,
        "ts_query": ts_query,
        "effective_date": effective_date,
        "limit": limit,
        **group_params,
    }
    rows = db.execute(sql, params).mappings().all()
    return [dict(row) for row in rows]


def _build_dense_query(body_evidence: dict[str, Any]) -> str:
    """
    Dense 검색용 자연어 쿼리 문장 생성.
    케이스 유형별 템플릿 + 전표 사실(시간, 금액, MCC, 근태) 삽입. 내부 코드(HOLIDAY_USAGE 등) 제외.
    """
    case_type = str(
        body_evidence.get("case_type") or body_evidence.get("intended_risk_type") or ""
    ).strip()
    merchant = body_evidence.get("merchantName") or "거래처 미상"
    amount = body_evidence.get("amount")
    amount_str = f"{int(amount):,}원" if amount is not None else "금액 미상"
    is_holiday = bool(body_evidence.get("isHoliday"))
    hr_status = str(body_evidence.get("hrStatus") or "").upper()
    mcc_code = body_evidence.get("mccCode") or ""
    mcc_name = body_evidence.get("mccName") or ""
    occurred_at = str(body_evidence.get("occurredAt") or "")
    hour = None
    try:
        if len(occurred_at) >= 13:
            hour = int(occurred_at[11:13])
    except Exception:
        pass
    is_night = hour is not None and (hour >= 22 or hour < 6)

    hr_hint = ""
    if hr_status in {"LEAVE", "OFF", "VACATION"}:
        hr_label = {"LEAVE": "휴가·결근", "OFF": "휴무", "VACATION": "휴가"}.get(
            hr_status, hr_status
        )
        hr_hint = f"해당 일자 근태 상태는 {hr_label}({hr_status})이다. "

    night_hint = ""
    if is_night and hour is not None:
        night_hint = f"결제 시각은 {hour:02d}시로 심야 시간대에 해당한다. "

    mcc_display = (
        f"{mcc_name}({mcc_code})" if (mcc_name and mcc_code) else (mcc_code or "")
    )

    _CASE_TEMPLATES: dict[str, str] = {
        "HOLIDAY_USAGE": (
            "주말 또는 공휴일 경비 사용 건으로, {merchant}에서 {amount}을 지출하였다. "
            "{hr_hint}"
            "{night_hint}"
            "이 지출에 적용되는 휴일 경비 사용 제한 규정과 식대 규정을 찾아야 한다."
        ),
        "LIMIT_EXCEED": (
            "{merchant}에서 {amount}을 지출하였으며, 예산 한도를 초과한 것으로 확인되었다. "
            "금액 구간별 승인 기준과 예산 초과 처리 절차 규정을 찾아야 한다."
        ),
        "PRIVATE_USE_RISK": (
            "{merchant}(MCC: {mcc})에서 {amount}을 사용하였으나 사적 사용 여부가 불명확하다. "
            "업무 관련성 증빙 기준과 사적 사용 금지 규정을 찾아야 한다."
        ),
        "UNUSUAL_PATTERN": (
            "{merchant}에서 {amount}을 지출하였으며 비정상 패턴이 감지되었다. "
            "{night_hint}"
            "관련 경비 지출 규정과 심야 시간대 지출 기준을 찾아야 한다."
        ),
    }

    template = _CASE_TEMPLATES.get(
        case_type,
        (
            "{merchant}에서 {amount}을 지출한 건에 대해 적용 가능한 사내 경비 지출 규정을 찾아야 한다. "
            "{hr_hint}{night_hint}"
        ),
    )

    query = template.format(
        merchant=merchant,
        amount=amount_str,
        hr_hint=hr_hint,
        night_hint=night_hint,
        mcc=mcc_display,
    ).strip()

    if is_holiday and "공휴일" not in query and "휴일" not in query:
        query += " 해당 날은 공휴일 또는 주말이다."

    # 규정 제14조: 모든 전표에 공통 적용되는 증빙 의무 조항 포함 검색
    if "증빙" not in query and "공통" not in query:
        query += " 모든 경비 지출에 공통 적용되는 증빙 의무(제14조 공통 증빙) 규정을 포함해야 한다."

    return query


def _build_dense_query_with_hyde(
    body_evidence: dict[str, Any],
    *,
    llm_client: Any = None,
) -> str:
    """
    HyDE 적용 시 LLM으로 가설 규정 문장 생성 후 쿼리와 결합.
    LLM 미설정 또는 실패 시 _build_dense_query()로 fallback.
    """
    base_query = _build_dense_query(body_evidence)
    if not getattr(settings, "enable_hyde_query", False):
        return base_query
    client = llm_client
    if client is None and settings.openai_api_key:
        try:
            from openai import AsyncOpenAI, OpenAI

            base_url = (settings.openai_base_url or "").strip()
            if ".openai.azure.com" in base_url:
                azure_ep = base_url.rstrip("/")
                if azure_ep.endswith("/openai/v1"):
                    azure_ep = azure_ep[: -len("/openai/v1")]
                client = OpenAI(
                    api_key=settings.openai_api_key,
                    azure_endpoint=azure_ep,
                    api_version=getattr(
                        settings, "openai_api_version", "2024-12-01-preview"
                    ),
                )
            else:
                kw: dict[str, Any] = {"api_key": settings.openai_api_key}
                if base_url:
                    kw["base_url"] = base_url
                client = OpenAI(**kw)
        except Exception:
            client = None
    if client is None:
        return base_query
    base_url = (settings.openai_base_url or "").strip()
    try:
        system_prompt = (
            "당신은 한국 기업의 사내 경비 지출 관리 규정 전문가다.\n"
            "아래 전표 상황에 대해 실제 사내 규정집에 나올 법한 조문 형태의 문장을 1~2문장 작성하라.\n"
            "반드시 '제N조' 형식의 조문 번호, '①②③' 형식의 항 번호를 포함하라.\n"
            "실제 규정집 문체와 유사하게 작성할 것. JSON, 코드블록 사용 금지."
        )
        user_prompt = f"전표 상황:\n{base_query}\n\n이 상황에 적용될 가설 규정 문장:"
        response = client.chat.completions.create(
            **completion_kwargs_for_azure(
                base_url,
                model=getattr(settings, "reasoning_llm_model", "gpt-4o-mini"),
                max_tokens=150,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            ),
        )
        hyde_text = (response.choices[0].message.content or "").strip()
        return f"{base_query}\n\n[가설 규정 문장] {hyde_text}"
    except Exception:
        return base_query


def _search_dense(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    limit: int = 20,
    effective_date: Any = None,
    group_filter: list[str] | None = None,
    embed_column: str = settings.rag_embedding_column,
    use_hyde: bool = False,
    llm_client: Any = None,
    query_text_override: str | None = None,
) -> list[dict[str, Any]]:
    """Dense 벡터 검색. 자연어 쿼리 사용, 선택 시 HyDE 적용. group_filter 있으면 해당 장(章) 내에서만 검색."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(embed_column or "")):
        return []
    cast_type = str(settings.rag_embedding_cast_type or "halfvec").strip().lower()
    if cast_type not in {"vector", "halfvec"}:
        cast_type = "halfvec"
    try:
        from services.chunking_pipeline import embed_texts
    except ImportError:
        return []

    if query_text_override is not None:
        query_text = str(query_text_override).strip()
    elif use_hyde:
        query_text = _build_dense_query_with_hyde(body_evidence, llm_client=llm_client)
    else:
        query_text = _build_dense_query(body_evidence)
    if not query_text or not query_text.strip():
        return []

    vectors = embed_texts([query_text])
    if not vectors:
        return []
    query_vector = vectors[0]

    group_filter_sql = ""
    group_params: dict[str, str] = {}
    if group_filter:
        conditions = [
            f"(metadata_json->>'semantic_group' LIKE :dense_grp{i})"
            for i in range(len(group_filter))
        ]
        group_filter_sql = " AND (" + " OR ".join(conditions) + ")"
        for i, grp in enumerate(group_filter):
            group_params[f"dense_grp{i}"] = f"{grp}%"

    sql = text(
        f"""
        SELECT
            chunk_id, doc_id, regulation_article, regulation_clause,
            parent_title, chunk_text, search_text, node_type, parent_id,
            version, effective_from, effective_to, page_no, chunk_index,
            metadata_json,
            1 - ({embed_column} <=> CAST(:query_vec AS {cast_type})) AS dense_score
        FROM dwp_aura.rag_chunk
        WHERE tenant_id = :tenant_id
          AND is_active = true
          AND {embed_column} IS NOT NULL
          AND (:effective_date IS NULL OR coalesce(effective_from, :effective_date) <= :effective_date)
          AND (:effective_date IS NULL OR coalesce(effective_to, :effective_date) >= :effective_date)
        """
        + group_filter_sql
        + f"""
        ORDER BY {embed_column} <=> CAST(:query_vec AS {cast_type})
        LIMIT :limit
        """
    )
    params: dict[str, Any] = {
        "tenant_id": settings.default_tenant_id,
        "query_vec": str(query_vector),
        "effective_date": effective_date,
        "limit": limit,
        **group_params,
    }
    rows = db.execute(sql, params).mappings().all()
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
    """CLAUSE/ITEM 노드에 부모 청크 맥락 prepend.

    - CLAUSE: 부모 ARTICLE chunk_text prepend
    - ITEM: 부모 CLAUSE의 chunk_id를 context_chunk_ids에 추가 (chunk_text는 contextual_header가 이미 포함)
    """
    parent_ids = [
        c.get("parent_id")
        for c in chunks
        if c.get("node_type") in ("CLAUSE", "ITEM") and c.get("parent_id")
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
        node_type = chunk.get("node_type")
        parent_id = chunk.get("parent_id")
        if node_type == "CLAUSE" and parent_id in parent_map:
            parent = parent_map[parent_id]
            chunk = dict(chunk)
            chunk["context_chunk_ids"] = [parent["chunk_id"]]
            if chunk.get("chunk_text") and parent.get("parent_title") and not chunk["chunk_text"].strip().startswith("["):
                chunk["chunk_text"] = (
                    f"[{parent.get('regulation_article', '')} {parent.get('parent_title', '')}] "
                    + chunk["chunk_text"]
                )
        elif node_type == "ITEM" and parent_id in parent_map:
            # ITEM의 부모는 CLAUSE — chunk_text는 이미 contextual_header 포함, context_chunk_ids만 설정
            chunk = dict(chunk)
            chunk["context_chunk_ids"] = [parent_id]
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


def _parse_metadata_json(raw_meta: Any) -> dict[str, Any]:
    if isinstance(raw_meta, dict):
        return raw_meta
    if isinstance(raw_meta, str):
        try:
            parsed = json.loads(raw_meta)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _get_regulation_item(item: dict[str, Any]) -> str | None:
    meta = _parse_metadata_json(item.get("metadata_json"))
    value = meta.get("regulation_item")
    return str(value) if value is not None else None


def _to_trace_candidate(
    item: dict[str, Any],
    *,
    rank: int,
    stage: str,
    selected_by: str | None = None,
) -> dict[str, Any]:
    return {
        "rank": rank,
        "chunk_id": item.get("chunk_id"),
        "doc_id": item.get("doc_id"),
        "article": item.get("regulation_article"),
        "clause": item.get("regulation_clause"),
        "item": _get_regulation_item(item),
        "node_type": item.get("node_type"),
        "parent_title": item.get("parent_title"),
        "selected_by": selected_by or item.get("selected_by"),
        "scores": {
            "bm25": item.get("bm25_score"),
            "dense": item.get("dense_score"),
            "rrf": item.get("rrf_score"),
            "cross_encoder": item.get("cross_encoder_score"),
            "llm_rerank": item.get("llm_rerank_score"),
        },
        "stage": stage,
    }


def _build_why_selected(item: dict[str, Any], selection_stage: str) -> str:
    ce_score = item.get("cross_encoder_score")
    llm_score = item.get("llm_rerank_score")
    bm25_score = item.get("bm25_score")
    dense_score = item.get("dense_score")
    rrf_score = item.get("rrf_score")

    if selection_stage == "reranked_cross_encoder":
        if isinstance(ce_score, (int, float)):
            return f"cross-encoder 재정렬 상위 ({float(ce_score):.4f})"
        return "cross-encoder 재정렬 상위"
    if selection_stage == "reranked_llm_fallback":
        if isinstance(llm_score, (int, float)):
            return f"LLM rerank fallback 상위 ({float(llm_score):.2f})"
        return "LLM rerank fallback 상위"
    if selection_stage == "fused_rrf":
        if isinstance(rrf_score, (int, float)):
            return f"BM25+DENSE RRF 상위 ({float(rrf_score):.6f})"
        return "BM25+DENSE RRF 상위"
    if selection_stage == "bm25_only":
        if isinstance(bm25_score, (int, float)):
            return f"BM25 상위 ({float(bm25_score):.6f})"
        return "BM25 상위"
    if selection_stage == "dense_only":
        if isinstance(dense_score, (int, float)):
            return f"Dense 상위 ({float(dense_score):.6f})"
        return "Dense 상위"
    if selection_stage == "lexical_fallback":
        return "BM25/Dense 결과 부재로 lexical fallback 상위"
    return "하이브리드 검색 상위"


def _rewrite_query(
    body_evidence: dict[str, Any],
    *,
    limit: int,
    effective_date: date | None,
    candidate_limit: int,
    group_filter: list[str] | None,
    bm25_weight: float,
    dense_weight: float,
) -> dict[str, Any]:
    rewritten = query_rewrite_for_retrieval(body_evidence)
    rerank_query = _build_dense_query(body_evidence)
    return {
        "original_query": rewritten,
        "rewritten_query": rerank_query,
        "effective_date": str(effective_date) if effective_date else None,
        "limit": limit,
        "candidate_limit": candidate_limit,
        "group_filter": group_filter or [],
        "weights": {"bm25": bm25_weight, "dense": dense_weight},
    }


def _retrieve_candidates(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    candidate_limit: int,
    effective_date: date | None,
    group_filter: list[str] | None,
) -> dict[str, Any]:
    bm25_results = _search_bm25_with_group_filter(
        db,
        body_evidence,
        limit=candidate_limit,
        effective_date=effective_date,
        group_filter=group_filter,
    )
    bm25_group_relaxed = False
    if group_filter and len(bm25_results) < candidate_limit:
        bm25_group_relaxed = True
        bm25_fallback = _search_bm25(
            db,
            body_evidence,
            limit=candidate_limit,
            effective_date=effective_date,
        )
        existing_ids = {r["chunk_id"] for r in bm25_results}
        for r in bm25_fallback:
            if r.get("chunk_id") not in existing_ids:
                bm25_results.append(r)
                existing_ids.add(r["chunk_id"])

    dense_results = _search_dense(
        db,
        body_evidence,
        limit=candidate_limit,
        effective_date=effective_date,
        group_filter=group_filter,
        use_hyde=getattr(settings, "enable_hyde_query", False),
    )
    dense_group_relaxed = False
    if group_filter and len(dense_results) < candidate_limit:
        dense_group_relaxed = True
        dense_fallback = _search_dense(
            db,
            body_evidence,
            limit=candidate_limit,
            effective_date=effective_date,
            group_filter=None,
            use_hyde=getattr(settings, "enable_hyde_query", False),
        )
        existing_ids = {r["chunk_id"] for r in dense_results}
        for r in dense_fallback:
            if r.get("chunk_id") not in existing_ids:
                dense_results.append(r)
                existing_ids.add(r["chunk_id"])

    return {
        "bm25_results": bm25_results,
        "dense_results": dense_results,
        "bm25_group_relaxed": bm25_group_relaxed,
        "dense_group_relaxed": dense_group_relaxed,
    }


def _fuse_candidates(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    bm25_results: list[dict[str, Any]],
    dense_results: list[dict[str, Any]],
    candidate_limit: int,
    effective_date: date | None,
    bm25_weight: float,
    dense_weight: float,
) -> dict[str, Any]:
    fallback_used = False
    fallback_reason: str | None = None

    if bm25_results and dense_results:
        fused = _reciprocal_rank_fusion(
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
    else:
        fused = _search_lexical_legacy(
            db,
            body_evidence,
            limit=candidate_limit,
            effective_date=effective_date,
        )
        for r in fused:
            r.setdefault("rrf_score", r.get("bm25_score", 0))
        selection_stage = "lexical_fallback"
        fallback_used = True
        fallback_reason = "no_bm25_dense_hits"

    for idx, item in enumerate(fused, start=1):
        item["selected_by"] = selection_stage
        item["selection_rank"] = idx

    enriched = _enrich_with_parent_context(db, fused[:candidate_limit])
    return {
        "enriched": enriched,
        "selection_stage": selection_stage,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
    }


def _rerank_candidates(
    enriched: list[dict[str, Any]],
    *,
    rerank_query: str,
    selection_stage: str,
    body_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reranker_used = False
    reranker_type = "none"
    rerank_input_limit = min(len(enriched), 25)
    rerank_input = enriched[:rerank_input_limit]
    rerank_exception: str | None = None

    try:
        from services.retrieval_quality import rerank_with_cross_encoder

        reranked = rerank_with_cross_encoder(rerank_input, rerank_query)
        reranked_ids = {
            r.get("chunk_id") for r in reranked if r.get("chunk_id") is not None
        }
        remaining = [
            r
            for r in enriched[rerank_input_limit:]
            if r.get("chunk_id") not in reranked_ids
        ]
        enriched = reranked + remaining

        cross_encoder_available = any(
            r.get("cross_encoder_available") is True for r in reranked
        )
        model_unavailable = bool(reranked) and all(
            r.get("cross_encoder_available") is False for r in reranked[:1]
        )
        if cross_encoder_available:
            reranker_used = True
            reranker_type = "cross_encoder"
            selection_stage = "reranked_cross_encoder"
            for idx, item in enumerate(enriched[:rerank_input_limit], start=1):
                item["selected_by"] = selection_stage
                item["selection_rank"] = idx

        if model_unavailable and getattr(settings, "enable_llm_rerank_fallback", True):
            try:
                from services.retrieval_quality import rerank_with_llm_fallback

                reranked_llm = rerank_with_llm_fallback(
                    rerank_input,
                    rerank_query,
                    body_evidence=body_evidence,
                )
                llm_ids = {
                    r.get("chunk_id")
                    for r in reranked_llm
                    if r.get("chunk_id") is not None
                }
                tail = [
                    r
                    for r in enriched[rerank_input_limit:]
                    if r.get("chunk_id") not in llm_ids
                ]
                enriched = reranked_llm + tail
                reranker_used = True
                reranker_type = "llm_fallback"
                selection_stage = "reranked_llm_fallback"
                for idx, item in enumerate(enriched[:rerank_input_limit], start=1):
                    item["selected_by"] = selection_stage
                    item["selection_rank"] = idx
            except Exception as llm_ex:
                rerank_exception = f"llm_fallback_failed:{llm_ex.__class__.__name__}"
    except Exception as ce_ex:
        rerank_exception = f"cross_encoder_failed:{ce_ex.__class__.__name__}"
        if getattr(settings, "enable_llm_rerank_fallback", True):
            try:
                from services.retrieval_quality import rerank_with_llm_fallback

                reranked_llm = rerank_with_llm_fallback(
                    rerank_input,
                    rerank_query,
                    body_evidence=body_evidence,
                )
                llm_ids = {
                    r.get("chunk_id")
                    for r in reranked_llm
                    if r.get("chunk_id") is not None
                }
                tail = [
                    r
                    for r in enriched[rerank_input_limit:]
                    if r.get("chunk_id") not in llm_ids
                ]
                enriched = reranked_llm + tail
                reranker_used = True
                reranker_type = "llm_fallback"
                selection_stage = "reranked_llm_fallback"
                for idx, item in enumerate(enriched[:rerank_input_limit], start=1):
                    item["selected_by"] = selection_stage
                    item["selection_rank"] = idx
            except Exception as llm_ex:
                rerank_exception = f"{rerank_exception}|llm_fallback_failed:{llm_ex.__class__.__name__}"

    return {
        "enriched": enriched,
        "selection_stage": selection_stage,
        "reranker_used": reranker_used,
        "reranker_type": reranker_type,
        "rerank_input_limit": rerank_input_limit,
        "rerank_exception": rerank_exception,
    }


def _finalize_context(
    enriched: list[dict[str, Any]],
    *,
    limit: int,
    selection_stage: str,
    reranker_used: bool,
    reranker_type: str,
    fallback_used: bool,
    fallback_reason: str | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in enriched[:limit]:
        regulation_item = _get_regulation_item(item)
        why_selected = _build_why_selected(item, selection_stage)
        results.append({
            "doc_id": item.get("doc_id"),
            "article": item.get("regulation_article"),
            "clause": item.get("regulation_clause"),
            "item": regulation_item,
            "node_type": item.get("node_type"),
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
                "llm_rerank_score": item.get("llm_rerank_score"),
            },
            "why_selected": why_selected,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            "reranker_used": reranker_used,
            "reranker_type": reranker_type,
            "selection_stage": selection_stage,
            "selected_by": item.get("selected_by", selection_stage),
        })
    return results


def _run_search_policy_chunks_pipeline(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """검색 파이프라인 실행 + structured trace 생성."""
    effective_date = None
    occurred_at = body_evidence.get("occurredAt")
    if occurred_at:
        try:
            effective_date = date.fromisoformat(str(occurred_at)[:10])
        except Exception:
            pass

    candidate_limit = max(limit * 6, 20)
    bm25_weight, dense_weight = _get_rrf_weights(body_evidence)
    group_filter = _get_semantic_group_filter(body_evidence)
    rewrite_trace = _rewrite_query(
        body_evidence,
        limit=limit,
        effective_date=effective_date,
        candidate_limit=candidate_limit,
        group_filter=group_filter,
        bm25_weight=bm25_weight,
        dense_weight=dense_weight,
    )

    retrieve_out = _retrieve_candidates(
        db,
        body_evidence,
        candidate_limit=candidate_limit,
        effective_date=effective_date,
        group_filter=group_filter,
    )
    bm25_results = retrieve_out["bm25_results"]
    dense_results = retrieve_out["dense_results"]

    fuse_out = _fuse_candidates(
        db,
        body_evidence,
        bm25_results=bm25_results,
        dense_results=dense_results,
        candidate_limit=candidate_limit,
        effective_date=effective_date,
        bm25_weight=bm25_weight,
        dense_weight=dense_weight,
    )
    enriched = fuse_out["enriched"]
    fused_snapshot = [dict(item) for item in enriched[:candidate_limit]]
    selection_stage = str(fuse_out["selection_stage"])
    fallback_used = bool(fuse_out["fallback_used"])
    fallback_reason = fuse_out.get("fallback_reason")

    rerank_out = _rerank_candidates(
        enriched,
        rerank_query=rewrite_trace["rewritten_query"],
        selection_stage=selection_stage,
        body_evidence=body_evidence,
    )
    enriched = rerank_out["enriched"]
    selection_stage = str(rerank_out["selection_stage"])
    reranker_used = bool(rerank_out["reranker_used"])
    reranker_type = str(rerank_out["reranker_type"] or "none")
    rerank_input_limit = int(rerank_out["rerank_input_limit"])
    rerank_exception = rerank_out.get("rerank_exception")

    results = _finalize_context(
        enriched,
        limit=limit,
        selection_stage=selection_stage,
        reranker_used=reranker_used,
        reranker_type=reranker_type,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
    )

    trace = {
        "trace_version": "policy_search.v1",
        "search": {
            **rewrite_trace,
            "selection_stage": selection_stage,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            "reranker_used": reranker_used,
            "reranker_type": reranker_type,
            "rerank_input_limit": rerank_input_limit,
            "rerank_exception": rerank_exception,
            "group_filter_relaxed": {
                "bm25": bool(retrieve_out["bm25_group_relaxed"]),
                "dense": bool(retrieve_out["dense_group_relaxed"]),
            },
            "decision_summary": (
                f"{selection_stage}로 {len(results)}건 선택"
                + (f", reranker={reranker_type}" if reranker_used else ", reranker=none")
                + (f", fallback={fallback_reason}" if fallback_used else "")
            ),
        },
        "stages": {
            "bm25_candidates": [
                _to_trace_candidate(item, rank=i, stage="bm25")
                for i, item in enumerate(bm25_results[:candidate_limit], start=1)
            ],
            "dense_candidates": [
                _to_trace_candidate(item, rank=i, stage="dense")
                for i, item in enumerate(dense_results[:candidate_limit], start=1)
            ],
            "fused_candidates": [
                _to_trace_candidate(item, rank=i, stage="fused", selected_by=fuse_out["selection_stage"])
                for i, item in enumerate(fused_snapshot, start=1)
            ],
            "reranked_candidates": [
                _to_trace_candidate(item, rank=i, stage="reranked", selected_by=selection_stage)
                for i, item in enumerate(enriched[:candidate_limit], start=1)
            ],
            "selected_candidates": [
                {
                    "rank": i,
                    "article": item.get("article"),
                    "clause": item.get("clause"),
                    "item": item.get("item"),
                    "node_type": item.get("node_type"),
                    "chunk_ids": item.get("chunk_ids"),
                    "selected_by": item.get("selected_by"),
                    "selection_stage": item.get("selection_stage"),
                    "why_selected": item.get("why_selected"),
                    "scores": {
                        "bm25": item.get("score_detail", {}).get("bm25_score"),
                        "dense": item.get("score_detail", {}).get("dense_score"),
                        "rrf": item.get("score_detail", {}).get("rrf_score"),
                        "cross_encoder": item.get("score_detail", {}).get("cross_encoder_score"),
                        "llm_rerank": item.get("score_detail", {}).get("llm_rerank_score"),
                    },
                }
                for i, item in enumerate(results, start=1)
            ],
        },
    }
    return results, trace


def search_policy_chunks_with_trace(
    db: Session,
    body_evidence: dict[str, Any],
    limit: int = 5,
) -> dict[str, Any]:
    """디버그/설명용 검색 결과. chunks + trace를 함께 반환."""
    chunks, trace = _run_search_policy_chunks_pipeline(
        db,
        body_evidence,
        limit=limit,
    )
    return {"chunks": chunks, "trace": trace}


def search_policy_chunks(
    db: Session,
    body_evidence: dict[str, Any],
    limit: int = 5,
    *,
    debug: bool = False,
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    메인 검색 함수.

    기본 동작은 기존과 동일하게 chunks(list)만 반환한다.
    debug=True일 때만 chunks + trace(dict)를 반환한다.
    """
    chunks, trace = _run_search_policy_chunks_pipeline(
        db,
        body_evidence,
        limit=limit,
    )
    if debug:
        return {"chunks": chunks, "trace": trace}
    return chunks
