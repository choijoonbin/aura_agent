"""
계층적 청킹 → 임베딩 생성 → pgvector 저장 파이프라인.

임베딩 모델: Azure/OpenAI text-embedding-3-large (3072차원, 기본)
저장 대상: dwp_aura.rag_chunk (embedding_az 컬럼 기본, 설정으로 변경 가능)

DB 마이그레이션(기본값 기준):
  ALTER TABLE dwp_aura.rag_chunk ADD COLUMN IF NOT EXISTS embedding_az vector(3072);
  CREATE INDEX IF NOT EXISTS ix_rag_chunk_embedding_az_hnsw ON dwp_aura.rag_chunk
    USING hnsw (embedding_az vector_cosine_ops) WITH (m = 16, ef_construction = 64);
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

# torch 2.6 미만: safetensors만 사용하도록 유도 (CVE-2025-32434 대응)
if "TRANSFORMERS_SAFE_TENSORS_WEIGHTS_ONLY" not in os.environ:
    os.environ["TRANSFORMERS_SAFE_TENSORS_WEIGHTS_ONLY"] = "1"

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.rag_chunk_lab_service import ChunkNode, _expand_tokens, hierarchical_chunk
from utils.config import settings

logger = logging.getLogger(__name__)

# Azure/OpenAI 임베딩 기본값 (config로 override 가능)
_EMBEDDING_DIM = settings.openai_embedding_dim
_EMBED_COLUMN = settings.rag_embedding_column
_last_embedding_error: str | None = None


def _embedding_cast_type() -> str:
    cast_type = str(settings.rag_embedding_cast_type or "halfvec").strip().lower()
    if cast_type not in {"vector", "halfvec"}:
        return "halfvec"
    return cast_type


def _sql_identifier_or_raise(value: str, *, label: str) -> str:
    ident = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ident):
        raise RuntimeError(f"유효하지 않은 SQL 식별자({label}): {value!r}")
    return ident


def _embedding_column_exists(db: Session, column_name: str) -> bool:
    row = db.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'dwp_aura'
              AND table_name = 'rag_chunk'
              AND column_name = :col
            LIMIT 1
            """
        ),
        {"col": column_name},
    ).fetchone()
    return bool(row)


def _build_embedding_client():
    from openai import AzureOpenAI, OpenAI  # type: ignore

    base_url = (settings.openai_base_url or "").strip()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY 미설정")
    if not base_url:
        return OpenAI(api_key=settings.openai_api_key)
    if ".openai.azure.com" in base_url:
        azure_endpoint = base_url.rstrip("/")
        if azure_endpoint.endswith("/openai/v1"):
            azure_endpoint = azure_endpoint[: -len("/openai/v1")]
        return AzureOpenAI(
            api_key=settings.openai_api_key,
            azure_endpoint=azure_endpoint,
            api_version=settings.openai_api_version,
        )
    return OpenAI(api_key=settings.openai_api_key, base_url=base_url)


def _embed_batch(client: Any, batch: list[str]) -> list[list[float]]:
    response = client.embeddings.create(
        input=batch,
        model=settings.openai_embedding_model,
    )
    vectors = [list(d.embedding) for d in response.data]
    for idx, vec in enumerate(vectors):
        if len(vec) != _EMBEDDING_DIM:
            raise RuntimeError(
                f"임베딩 차원 불일치: expected={_EMBEDDING_DIM}, got={len(vec)} (index={idx})"
            )
    return vectors


def embed_texts(texts: list[str], *, batch_size: int | None = None) -> list[list[float]] | None:
    """
    텍스트 목록을 배치 임베딩. 실패 시 None 반환.
    search_text(contextual_header + 본문)를 임베딩 대상으로 사용.
    Contextual Retrieval 패턴: 장·절·조 맥락이 포함된 search_text로 짧은 조항의 벡터 품질을 확보한다.
    """
    if not texts:
        return []
    global _last_embedding_error
    _last_embedding_error = None
    try:
        client = _build_embedding_client()
    except Exception as e:
        _last_embedding_error = f"임베딩 클라이언트 초기화 실패: {e}"
        logger.warning(_last_embedding_error)
        return None

    bs = max(1, int(batch_size or settings.openai_embedding_batch_size))
    max_retries = max(0, int(settings.openai_embedding_max_retries))
    all_vectors: list[list[float]] = []
    for i in range(0, len(texts), bs):
        batch = texts[i : i + bs]
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                vectors = _embed_batch(client, batch)
                all_vectors.extend(vectors)
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt < max_retries:
                    sleep_s = 0.5 * (2**attempt)
                    time.sleep(sleep_s)
        if last_err is not None:
            _last_embedding_error = f"Embedding failed after retries: {last_err}"
            logger.error(_last_embedding_error)
            return None
    return all_vectors


def save_hierarchical_chunks(
    db: Session,
    doc_id: int,
    nodes: list[ChunkNode],
    *,
    version: str | None = None,
    effective_from: str | None = None,
    effective_to: str | None = None,
    embed_column: str | None = None,
) -> dict[str, Any]:
    """
    계층적 청크 노드 목록을 rag_chunk 테이블에 저장.

    저장 전략:
    1. 기존 청크 비활성화 (is_active=false)
    2. ARTICLE 노드 먼저 저장 (parent_id 확보)
    3. CLAUSE 노드 저장 (parent_id = 해당 ARTICLE의 chunk_id)
    4. 임베딩 생성 후 embedding_az(또는 지정 컬럼) 업데이트
    5. tsvector 업데이트 (search_tsv)
    """
    col = _sql_identifier_or_raise(embed_column or _EMBED_COLUMN, label="embed_column")
    tenant_id = settings.default_tenant_id

    db.execute(
        text(
            "UPDATE dwp_aura.rag_chunk SET is_active = false WHERE doc_id = :doc_id AND tenant_id = :tid"
        ),
        {"doc_id": doc_id, "tid": tenant_id},
    )

    article_nodes = [n for n in nodes if n.node_type == "ARTICLE"]
    clause_nodes  = [n for n in nodes if n.node_type == "CLAUSE"]
    item_nodes    = [n for n in nodes if n.node_type == "ITEM"]

    article_chunk_id_map: dict[str, int] = {}
    clause_chunk_id_map: dict[int, int] = {}  # chunk_index → DB chunk_id (ITEM 부모 링크용)
    use_search_tokens = _embedding_column_exists(db, "search_tokens")

    if use_search_tokens:
        insert_sql = text("""
            INSERT INTO dwp_aura.rag_chunk (
                tenant_id, doc_id, chunk_text, search_text,
                regulation_article, regulation_clause,
                parent_title, node_type, parent_id, parent_chunk_id,
                chunk_level, chunk_index, page_no,
                version, effective_from, effective_to,
                metadata_json, search_tokens,
                is_active, created_at
            ) VALUES (
                :tenant_id, :doc_id, :chunk_text, :search_text,
                :regulation_article, :regulation_clause,
                :parent_title, :node_type, :parent_id, :parent_chunk_id,
                :chunk_level, :chunk_index, :page_no,
                :version, :effective_from, :effective_to,
                CAST(:metadata_json AS jsonb), :search_tokens,
                true, now()
            ) RETURNING chunk_id
        """)
    else:
        insert_sql = text("""
            INSERT INTO dwp_aura.rag_chunk (
                tenant_id, doc_id, chunk_text, search_text,
                regulation_article, regulation_clause,
                parent_title, node_type, parent_id, parent_chunk_id,
                chunk_level, chunk_index, page_no,
                version, effective_from, effective_to,
                metadata_json,
                is_active, created_at
            ) VALUES (
                :tenant_id, :doc_id, :chunk_text, :search_text,
                :regulation_article, :regulation_clause,
                :parent_title, :node_type, :parent_id, :parent_chunk_id,
                :chunk_level, :chunk_index, :page_no,
                :version, :effective_from, :effective_to,
                CAST(:metadata_json AS jsonb),
                true, now()
            ) RETURNING chunk_id
        """)

    saved_chunks: list[dict[str, Any]] = []

    for node in article_nodes:
        meta = {
            "semantic_group": getattr(node, "semantic_group", "") or "",
            "regulation_article": node.regulation_article or "",
            "current_section": getattr(node, "current_section", "") or "",
        }
        merged_articles = [
            str(a).strip()
            for a in (getattr(node, "merged_articles", None) or [])
            if str(a).strip()
        ]
        if merged_articles:
            meta["merged_articles"] = merged_articles
        if getattr(node, "merged_with", None):
            meta["merged_with"] = node.merged_with
        row_params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "doc_id": doc_id,
            "chunk_text": node.chunk_text,
            "search_text": node.search_text,
            "regulation_article": node.regulation_article,
            "regulation_clause": node.regulation_clause,
            "parent_title": node.parent_title,
            "node_type": "ARTICLE",
            "parent_id": None,
            "parent_chunk_id": None,
            "chunk_level": "root",
            "chunk_index": node.chunk_index,
            "page_no": node.page_no,
            "version": version,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "metadata_json": json.dumps(meta, ensure_ascii=False),
        }
        if use_search_tokens:
            row_params["search_tokens"] = _expand_tokens(node.search_text or "")
        row = db.execute(insert_sql, row_params).fetchone()
        chunk_id = row[0]
        key = node.regulation_article or str(node.chunk_index)
        article_chunk_id_map[key] = chunk_id
        for alt_article in merged_articles:
            article_chunk_id_map[alt_article] = chunk_id
        saved_chunks.append(
            {"chunk_id": chunk_id, "search_text": node.search_text, "node_type": "ARTICLE"}
        )

    for node in clause_nodes:
        parent_id = article_chunk_id_map.get(node.regulation_article or "")
        meta = {
            "semantic_group": getattr(node, "semantic_group", "") or "",
            "regulation_article": node.regulation_article or "",
            "parent_article": node.regulation_article or "",
            "current_section": getattr(node, "current_section", "") or "",
        }
        clause_params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "doc_id": doc_id,
            "chunk_text": node.chunk_text,
            "search_text": node.search_text,
            "regulation_article": node.regulation_article,
            "regulation_clause": node.regulation_clause,
            "parent_title": node.parent_title,
            "node_type": "CLAUSE",
            "parent_id": parent_id,
            "parent_chunk_id": str(parent_id) if parent_id else None,
            "chunk_level": "child",
            "chunk_index": node.chunk_index,
            "page_no": node.page_no,
            "version": version,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "metadata_json": json.dumps(meta, ensure_ascii=False),
        }
        if use_search_tokens:
            clause_params["search_tokens"] = _expand_tokens(node.search_text or "")
        row = db.execute(insert_sql, clause_params).fetchone()
        chunk_id = row[0]
        clause_chunk_id_map[node.chunk_index] = chunk_id  # ITEM 부모 링크용
        saved_chunks.append(
            {"chunk_id": chunk_id, "search_text": node.search_text, "node_type": "CLAUSE"}
        )

    for node in item_nodes:
        parent_clause_idx = getattr(node, "parent_clause_chunk_index", -1)
        parent_clause_db_id = clause_chunk_id_map.get(parent_clause_idx)
        meta = {
            "semantic_group": getattr(node, "semantic_group", "") or "",
            "regulation_article": node.regulation_article or "",
            "regulation_clause": node.regulation_clause or "",
            "regulation_item": getattr(node, "regulation_item", "") or "",
            "parent_article": node.regulation_article or "",
            "current_section": getattr(node, "current_section", "") or "",
        }
        item_params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "doc_id": doc_id,
            "chunk_text": node.chunk_text,
            "search_text": node.search_text,
            "regulation_article": node.regulation_article,
            "regulation_clause": node.regulation_clause,
            "parent_title": node.parent_title,
            "node_type": "ITEM",
            "parent_id": parent_clause_db_id,
            "parent_chunk_id": str(parent_clause_db_id) if parent_clause_db_id else None,
            "chunk_level": "leaf",
            "chunk_index": node.chunk_index,
            "page_no": node.page_no,
            "version": version,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "metadata_json": json.dumps(meta, ensure_ascii=False),
        }
        if use_search_tokens:
            item_params["search_tokens"] = _expand_tokens(node.search_text or "")
        row = db.execute(insert_sql, item_params).fetchone()
        chunk_id = row[0]
        saved_chunks.append(
            {"chunk_id": chunk_id, "search_text": node.search_text, "node_type": "ITEM"}
        )

    search_texts = [c["search_text"] for c in saved_chunks]
    vectors = None
    if not _embedding_column_exists(db, col):
        global _last_embedding_error
        _last_embedding_error = f"임베딩 컬럼 누락: {col} (scripts/migrate_embedding_az.sql 실행 필요)"
        logger.warning(_last_embedding_error)
    else:
        vectors = embed_texts(search_texts)

    if vectors:
        cast_type = _embedding_cast_type()
        db.execute(
            text(
                """
                CREATE TEMP TABLE IF NOT EXISTS tmp_rag_chunk_embedding (
                    chunk_id bigint PRIMARY KEY,
                    vec_text text NOT NULL
                ) ON COMMIT DROP
                """
            )
        )
        db.execute(text("TRUNCATE tmp_rag_chunk_embedding"))
        db.execute(
            text("INSERT INTO tmp_rag_chunk_embedding (chunk_id, vec_text) VALUES (:chunk_id, :vec_text)"),
            [{"chunk_id": c["chunk_id"], "vec_text": str(v)} for c, v in zip(saved_chunks, vectors)],
        )
        db.execute(
            text(
                f"""
                UPDATE dwp_aura.rag_chunk rc
                SET {col} = CAST(tmp.vec_text AS {cast_type})
                FROM tmp_rag_chunk_embedding tmp
                WHERE rc.chunk_id = tmp.chunk_id
                """
            )
        )
        logger.info("Embedding saved for %s chunks (bulk)", len(saved_chunks))
    else:
        reason = _last_embedding_error or "model unavailable"
        logger.warning("Embedding skipped: %s", reason)

    chunk_ids = [int(c["chunk_id"]) for c in saved_chunks]
    if chunk_ids:
        db.execute(
            text(
                """
                UPDATE dwp_aura.rag_chunk
                SET search_tsv = to_tsvector('simple', coalesce(search_text, chunk_text, ''))
                WHERE chunk_id = ANY(:chunk_ids)
                """
            ),
            {"chunk_ids": chunk_ids},
        )

    db.commit()

    # ROOT(ARTICLE) 청크만 short 판정 — CHILD(CLAUSE)는 항목 단위로 짧은 것이 정상
    short_roots = [n for n in article_nodes if len(n.chunk_text) < 200]
    short_chunk_rate = len(short_roots) / len(article_nodes) if article_nodes else 0.0

    out = {
        "doc_id": doc_id,
        "total_chunks": len(saved_chunks),
        "article_chunks": len(article_nodes),
        "clause_chunks": len(clause_nodes),
        "item_chunks": len(item_nodes),
        "embedding_saved": vectors is not None,
        "embedding_model": getattr(settings, "openai_embedding_model", "text-embedding-3-large"),
        "embed_column": col,
        "short_chunk_rate": short_chunk_rate,
    }
    if vectors is None and _last_embedding_error:
        out["embedding_skip_reason"] = _last_embedding_error
    return out


def run_chunking_pipeline(
    db: Session,
    doc_id: int,
    raw_text: str,
    *,
    version: str | None = None,
    effective_from: str | None = None,
    effective_to: str | None = None,
    embed_column: str | None = None,
) -> dict[str, Any]:
    """전체 파이프라인 실행: 청킹 → 임베딩 → 저장."""
    nodes = hierarchical_chunk(raw_text)
    if not nodes:
        return {"error": "청킹 결과가 없습니다. 텍스트 형식을 확인하세요."}
    return save_hierarchical_chunks(
        db,
        doc_id,
        nodes,
        version=version,
        effective_from=effective_from,
        effective_to=effective_to,
        embed_column=embed_column,
    )
