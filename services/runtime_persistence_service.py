"""
Phase E: Event log와 final result 분리 저장.
- Event log: log_run_event() — agent_activity_log에 이벤트별 행 적재 (RUN_CREATED, AGENT_EVENT, HITL_REQUESTED, RUN_COMPLETED 등).
- Final result: persist_analysis_result() (persistence_service) — run 종료 시 최종 결과만 별도 저장.
- Latest / History: get_latest_run_id_by_case(), list_run_ids_by_case() — 케이스별 최신 run 및 run 목록.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from utils.config import settings


RESOURCE_TYPE = "analysis_run"
ACTOR_AGENT_ID = "mater_poc_langgraph"
ACTOR_DISPLAY_NAME = "AuraAgent LangGraph Agent"


def create_analysis_run_row(
    db: Session,
    *,
    run_id: str,
    case_id_int: int,
    status: str = "STARTED",
    mode: str = "LIVE",
    requested_by: str = "HUMAN",
) -> None:
    """
    case_analysis_run에 run 행을 삽입.
    persist_analysis_result가 case_analysis_result에 넣을 때 FK(case_analysis_run.run_id)를 만족시키기 위해
    분석 run 시작 시 호출해야 함.
    """
    sql = text(
        """
        insert into dwp_aura.case_analysis_run (
            run_id,
            tenant_id,
            case_id,
            status,
            mode,
            requested_by
        ) values (
            cast(:run_id as uuid),
            :tenant_id,
            :case_id_int,
            :status,
            :mode,
            :requested_by
        )
        on conflict (run_id) do update set
            status = excluded.status,
            case_id = excluded.case_id
        """
    )
    db.execute(
        sql,
        {
            "run_id": run_id,
            "tenant_id": settings.default_tenant_id,
            "case_id_int": case_id_int,
            "status": status,
            "mode": mode,
            "requested_by": requested_by,
        },
    )
    db.commit()


def _json_default(value: Any) -> str:
    return str(value)


def log_run_event(
    db: Session,
    *,
    run_id: str,
    case_id: str,
    voucher_key: str | None,
    stage: str,
    event_type: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    payload = {
        "run_id": run_id,
        "case_id": case_id,
        "voucher_key": voucher_key,
        **(metadata or {}),
    }
    sql = text(
        """
        insert into dwp_aura.agent_activity_log (
            tenant_id,
            stage,
            event_type,
            resource_type,
            resource_id,
            occurred_at,
            actor_agent_id,
            actor_user_id,
            actor_display_name,
            metadata_json,
            created_at,
            created_by,
            updated_at,
            updated_by
        ) values (
            :tenant_id,
            :stage,
            :event_type,
            :resource_type,
            :resource_id,
            now(),
            :actor_agent_id,
            :actor_user_id,
            :actor_display_name,
            cast(:metadata_json as jsonb),
            now(),
            :created_by,
            now(),
            :updated_by
        )
        """
    )
    db.execute(
        sql,
        {
            "tenant_id": settings.default_tenant_id,
            "stage": stage,
            "event_type": event_type,
            "resource_type": RESOURCE_TYPE,
            "resource_id": run_id,
            "actor_agent_id": ACTOR_AGENT_ID,
            "actor_user_id": settings.default_user_id,
            "actor_display_name": ACTOR_DISPLAY_NAME,
            "metadata_json": json.dumps(payload, ensure_ascii=False, default=_json_default),
            "created_by": settings.default_user_id,
            "updated_by": settings.default_user_id,
        },
    )
    db.commit()


def get_persisted_timeline(db: Session, *, run_id: str) -> list[dict[str, Any]]:
    sql = text(
        """
        select
            event_type,
            occurred_at,
            metadata_json
        from dwp_aura.agent_activity_log
        where tenant_id = :tenant_id
          and resource_type = :resource_type
          and resource_id = :run_id
        order by occurred_at asc, activity_id asc
        """
    )
    rows = db.execute(
        sql,
        {
            "tenant_id": settings.default_tenant_id,
            "resource_type": RESOURCE_TYPE,
            "run_id": run_id,
        },
    ).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        metadata = dict(row["metadata_json"] or {})
        out.append(
            {
                "event_type": row["event_type"],
                "at": row["occurred_at"].isoformat() if row["occurred_at"] else None,
                "payload": metadata.get("payload") or metadata,
            }
        )
    return out


def get_latest_run_id_by_case(db: Session, *, case_id: str) -> str | None:
    sql = text(
        """
        select resource_id
        from dwp_aura.agent_activity_log
        where tenant_id = :tenant_id
          and resource_type = :resource_type
          and metadata_json ->> 'case_id' = :case_id
        order by occurred_at desc, activity_id desc
        limit 1
        """
    )
    row = db.execute(
        sql,
        {
            "tenant_id": settings.default_tenant_id,
            "resource_type": RESOURCE_TYPE,
            "case_id": case_id,
        },
    ).scalar_one_or_none()
    return str(row) if row else None


def list_run_ids_by_case(db: Session, *, case_id: str) -> list[str]:
    sql = text(
        """
        select distinct resource_id
        from dwp_aura.agent_activity_log
        where tenant_id = :tenant_id
          and resource_type = :resource_type
          and metadata_json ->> 'case_id' = :case_id
        order by resource_id desc
        """
    )
    rows = db.execute(
        sql,
        {
            "tenant_id": settings.default_tenant_id,
            "resource_type": RESOURCE_TYPE,
            "case_id": case_id,
        },
    ).scalars().all()
    return [str(row) for row in rows]


def get_run_aux_state(db: Session, *, run_id: str) -> dict[str, Any]:
    sql = text(
        """
        select metadata_json
        from dwp_aura.agent_activity_log
        where tenant_id = :tenant_id
          and resource_type = :resource_type
          and resource_id = :run_id
          and event_type in ('RUN_CREATED', 'HITL_REQUESTED', 'HITL_REQUIRED', 'HITL_DRAFT', 'HITL_RESPONSE', 'RUN_COMPLETED', 'RUN_FAILED', 'EVIDENCE_UPLOADED')
        order by occurred_at asc, activity_id asc
        """
    )
    rows = db.execute(
        sql,
        {
            "tenant_id": settings.default_tenant_id,
            "resource_type": RESOURCE_TYPE,
            "run_id": run_id,
        },
    ).mappings().all()
    lineage = None
    hitl_request = None
    hitl_draft = None
    hitl_response = None
    result = None
    evidence_document_result = None
    for row in rows:
        raw = row["metadata_json"]
        if isinstance(raw, dict):
            metadata = raw
        else:
            try:
                metadata = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        payload = metadata.get("payload") or metadata
        if not isinstance(payload, dict):
            payload = {}
        event_type = metadata.get("stored_event_type") or metadata.get("event_type")
        if event_type == "RUN_CREATED":
            lineage = metadata.get("lineage") or payload.get("lineage")
        elif event_type == "HITL_REQUESTED":
            candidate = metadata.get("hitl_request") or payload.get("metadata") or payload.get("hitl_request") or payload
            if isinstance(candidate, dict) and candidate:
                hitl_request = candidate
        elif event_type == "HITL_DRAFT":
            hitl_draft = metadata.get("hitl_draft") or payload
        elif event_type == "HITL_RESPONSE":
            candidate = metadata.get("hitl_response") or payload.get("hitl_response") or payload
            if isinstance(candidate, dict) and candidate:
                hitl_response = candidate
        elif event_type == "HITL_REQUIRED":
            candidate_result = metadata.get("result") or payload.get("result") or payload
            if isinstance(candidate_result, dict):
                result = candidate_result
                candidate_request = candidate_result.get("hitl_request")
                if isinstance(candidate_request, dict) and candidate_request:
                    hitl_request = candidate_request
        elif event_type in {"RUN_COMPLETED", "RUN_FAILED"}:
            candidate_result = metadata.get("result") or payload.get("result") or payload
            if isinstance(candidate_result, dict):
                result = candidate_result
                # REVIEW_REQUIRED 경로는 RUN_COMPLETED payload에만 hitl_request가 들어오므로 aux에서 복원한다.
                candidate_request = candidate_result.get("hitl_request")
                if isinstance(candidate_request, dict) and candidate_request:
                    hitl_request = candidate_request
                candidate_response = candidate_result.get("hitl_response")
                if isinstance(candidate_response, dict) and candidate_response:
                    hitl_response = candidate_response
        elif event_type == "EVIDENCE_UPLOADED":
            evidence_document_result = metadata.get("evidence_document_result") or payload.get("evidence_document_result")
    return {
        "lineage": lineage,
        "hitl_request": hitl_request,
        "hitl_draft": hitl_draft,
        "hitl_response": hitl_response,
        "result_payload": result,
        "evidence_document_result": evidence_document_result,
    }
