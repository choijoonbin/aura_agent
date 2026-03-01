from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from utils.config import settings


def list_rag_documents(db: Session) -> list[dict[str, Any]]:
    sql = text(
        """
        with latest_quality as (
            select distinct on (rq.doc_id)
                rq.doc_id,
                rq.run_id,
                rq.quality_gate_passed,
                rq.input_chunks,
                rq.final_chunks,
                rq.article_coverage,
                rq.noise_rate,
                rq.duplicate_rate,
                rq.short_chunk_rate,
                rq.missing_required,
                rq.errors,
                rq.raw_report_json,
                rq.created_at
            from dwp_aura.rag_document_quality_report rq
            where rq.tenant_id = :tenant_id
            order by rq.doc_id, rq.created_at desc, rq.id desc
        )
        select
            rd.doc_id,
            rd.title,
            rd.status,
            rd.doc_type,
            rd.source_type,
            rd.version,
            rd.effective_from,
            rd.effective_to,
            rd.lifecycle_status,
            rd.active_from,
            rd.active_to,
            rd.created_at,
            rd.updated_at,
            rd.quality_gate_passed,
            rd.last_quality_score,
            rd.last_quality_report_json,
            lq.run_id as quality_run_id,
            lq.quality_gate_passed as quality_report_passed,
            lq.input_chunks,
            lq.final_chunks,
            lq.article_coverage,
            lq.noise_rate,
            lq.duplicate_rate,
            lq.short_chunk_rate,
            lq.missing_required,
            lq.errors,
            lq.raw_report_json
        from dwp_aura.rag_document rd
        left join latest_quality lq on lq.doc_id = rd.doc_id
        where rd.tenant_id = :tenant_id
        order by rd.doc_id asc
        """
    )
    rows = db.execute(sql, {"tenant_id": settings.default_tenant_id}).mappings().all()
    return [dict(row) for row in rows]


def get_rag_document_detail(db: Session, doc_id: int) -> dict[str, Any] | None:
    docs = list_rag_documents(db)
    doc = next((item for item in docs if int(item.get("doc_id")) == int(doc_id)), None)
    if not doc:
        return None

    chunk_sql = text(
        """
        select
            chunk_id,
            regulation_article,
            regulation_clause,
            parent_title,
            chunk_text,
            version,
            effective_from,
            effective_to,
            page_no,
            chunk_index,
            is_active
        from dwp_aura.rag_chunk
        where tenant_id = :tenant_id
          and doc_id = :doc_id
          and is_active = true
        order by page_no nulls last, chunk_index nulls last, chunk_id asc
        limit 200
        """
    )
    chunks = [dict(row) for row in db.execute(chunk_sql, {"tenant_id": settings.default_tenant_id, "doc_id": doc_id}).mappings().all()]
    doc["chunks"] = chunks
    return doc
