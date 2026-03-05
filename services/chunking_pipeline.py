"""
계층적 청킹 → 임베딩 생성 → pgvector 저장 파이프라인.

임베딩 모델: paraphrase-multilingual-mpnet-base-v2 (768차원, 다국어·한국어, safetensors 우선 — torch 2.6 미만 대응)
저장 대상: dwp_aura.rag_chunk (embedding_ko 컬럼 또는 embedding, search_text 컬럼)

DB 마이그레이션: embedding이 1536차원이면 별도 컬럼 사용 권장.
  ALTER TABLE dwp_aura.rag_chunk ADD COLUMN IF NOT EXISTS embedding_ko vector(768);
  CREATE INDEX IF NOT EXISTS ix_rag_chunk_embedding_ko_hnsw ON dwp_aura.rag_chunk
    USING hnsw (embedding_ko vector_cosine_ops) WITH (m = 16, ef_construction = 64);
"""
from __future__ import annotations

import logging
import os
from typing import Any

# torch 2.6 미만: safetensors만 사용하도록 유도 (CVE-2025-32434 대응)
if "TRANSFORMERS_SAFE_TENSORS_WEIGHTS_ONLY" not in os.environ:
    os.environ["TRANSFORMERS_SAFE_TENSORS_WEIGHTS_ONLY"] = "1"

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.rag_chunk_lab_service import ChunkNode, hierarchical_chunk
from utils.config import settings

logger = logging.getLogger(__name__)

# 768차원 ko 모델. DB에 embedding_ko vector(768) 컬럼 필요 (마이그레이션 B).
_EMBEDDING_MODEL: Any = None
_EMBEDDING_DIM = 768
_EMBED_COLUMN = "embedding_ko"
_last_embedding_error: str | None = None


def get_embedding_model():
    """
    ko-sroberta-multitask 모델 싱글톤 로더.
    sentence-transformers 미설치 시 None 반환 (graceful degradation).
    """
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is not None:
        return _EMBEDDING_MODEL
    global _last_embedding_error
    _last_embedding_error = None
    try:
        from sentence_transformers import SentenceTransformer

        logger.info("Loading paraphrase-multilingual-mpnet-base-v2 (768d)...")
        _EMBEDDING_MODEL = SentenceTransformer(
            "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
        )
        dim = _EMBEDDING_MODEL.get_sentence_embedding_dimension()
        logger.info("Embedding model loaded. Dim: %s", dim)
        return _EMBEDDING_MODEL
    except ImportError as e:
        _last_embedding_error = "sentence-transformers 미설치 또는 import 실패 (해당 venv에서 pip install torch sentence-transformers 후 API 재시작)"
        logger.warning("%s: %s", _last_embedding_error, e)
        return None
    except Exception as e:
        _last_embedding_error = f"모델 로드 실패: {e}"
        logger.error("Failed to load embedding model: %s", e)
        return None


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """
    텍스트 목록을 배치 임베딩. 실패 시 None 반환.
    search_text(prefix 제거된 순수 본문)를 임베딩 대상으로 사용.
    """
    model = get_embedding_model()
    if model is None:
        return None
    try:
        vectors = model.encode(
            texts, batch_size=32, show_progress_bar=False, normalize_embeddings=True
        )
        return [v.tolist() for v in vectors]
    except Exception as e:
        logger.error("Embedding failed: %s", e)
        return None


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
    4. 임베딩 생성 후 embedding_ko(또는 지정 컬럼) 업데이트
    5. tsvector 업데이트 (search_tsv)
    """
    col = embed_column or _EMBED_COLUMN
    tenant_id = settings.default_tenant_id

    db.execute(
        text(
            "UPDATE dwp_aura.rag_chunk SET is_active = false WHERE doc_id = :doc_id AND tenant_id = :tid"
        ),
        {"doc_id": doc_id, "tid": tenant_id},
    )

    article_nodes = [n for n in nodes if n.node_type == "ARTICLE"]
    clause_nodes = [n for n in nodes if n.node_type != "ARTICLE"]

    article_chunk_id_map: dict[str, int] = {}

    insert_sql = text("""
        INSERT INTO dwp_aura.rag_chunk (
            tenant_id, doc_id, chunk_text, search_text,
            regulation_article, regulation_clause,
            parent_title, node_type, parent_id,
            chunk_level, chunk_index, page_no,
            version, effective_from, effective_to,
            is_active, created_at
        ) VALUES (
            :tenant_id, :doc_id, :chunk_text, :search_text,
            :regulation_article, :regulation_clause,
            :parent_title, :node_type, :parent_id,
            :chunk_level, :chunk_index, :page_no,
            :version, :effective_from, :effective_to,
            true, now()
        ) RETURNING chunk_id
    """)

    saved_chunks: list[dict[str, Any]] = []

    for node in article_nodes:
        row = db.execute(
            insert_sql,
            {
                "tenant_id": tenant_id,
                "doc_id": doc_id,
                "chunk_text": node.chunk_text,
                "search_text": node.search_text,
                "regulation_article": node.regulation_article,
                "regulation_clause": node.regulation_clause,
                "parent_title": node.parent_title,
                "node_type": "ARTICLE",
                "parent_id": None,
                "chunk_level": "root",
                "chunk_index": node.chunk_index,
                "page_no": node.page_no,
                "version": version,
                "effective_from": effective_from,
                "effective_to": effective_to,
            },
        ).fetchone()
        chunk_id = row[0]
        key = node.regulation_article or str(node.chunk_index)
        article_chunk_id_map[key] = chunk_id
        saved_chunks.append(
            {"chunk_id": chunk_id, "search_text": node.search_text, "node_type": "ARTICLE"}
        )

    for node in clause_nodes:
        parent_id = article_chunk_id_map.get(node.regulation_article or "")
        row = db.execute(
            insert_sql,
            {
                "tenant_id": tenant_id,
                "doc_id": doc_id,
                "chunk_text": node.chunk_text,
                "search_text": node.search_text,
                "regulation_article": node.regulation_article,
                "regulation_clause": node.regulation_clause,
                "parent_title": node.parent_title,
                "node_type": "CLAUSE",
                "parent_id": parent_id,
                "chunk_level": "child",
                "chunk_index": node.chunk_index,
                "page_no": node.page_no,
                "version": version,
                "effective_from": effective_from,
                "effective_to": effective_to,
            },
        ).fetchone()
        chunk_id = row[0]
        saved_chunks.append(
            {"chunk_id": chunk_id, "search_text": node.search_text, "node_type": "CLAUSE"}
        )

    search_texts = [c["search_text"] for c in saved_chunks]
    vectors = embed_texts(search_texts)

    if vectors:
        for chunk, vector in zip(saved_chunks, vectors):
            db.execute(
                text(
                    f"UPDATE dwp_aura.rag_chunk SET {col} = CAST(:vec AS vector) WHERE chunk_id = :cid"
                ),
                {"vec": str(vector), "cid": chunk["chunk_id"]},
            )
        logger.info("Embedding saved for %s chunks", len(saved_chunks))
    else:
        reason = _last_embedding_error or "model unavailable"
        logger.warning("Embedding skipped: %s", reason)

    for chunk in saved_chunks:
        db.execute(
            text("""
                UPDATE dwp_aura.rag_chunk
                SET search_tsv = to_tsvector('simple', coalesce(search_text, chunk_text, ''))
                WHERE chunk_id = :cid
            """),
            {"cid": chunk["chunk_id"]},
        )

    db.commit()

    out = {
        "doc_id": doc_id,
        "total_chunks": len(saved_chunks),
        "article_chunks": len(article_nodes),
        "clause_chunks": len(clause_nodes),
        "embedding_saved": vectors is not None,
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
