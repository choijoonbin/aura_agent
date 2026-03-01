from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from utils.config import settings


RESOURCE_TYPE = "analysis_run"
ACTOR_AGENT_ID = "mater_poc_langgraph"
ACTOR_DISPLAY_NAME = "MaterTask LangGraph Agent"


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
          and event_type in ('RUN_CREATED', 'HITL_REQUESTED', 'HITL_RESPONSE', 'RUN_COMPLETED', 'RUN_FAILED')
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
    hitl_response = None
    result = None
    for row in rows:
        metadata = dict(row["metadata_json"] or {})
        payload = metadata.get("payload") or metadata
        event_type = metadata.get("stored_event_type") or metadata.get("event_type")
        if event_type == "RUN_CREATED":
            lineage = metadata.get("lineage") or payload.get("lineage")
        elif event_type == "HITL_REQUESTED":
            hitl_request = metadata.get("hitl_request") or payload.get("metadata") or payload
        elif event_type == "HITL_RESPONSE":
            hitl_response = metadata.get("hitl_response") or payload
        elif event_type in {"RUN_COMPLETED", "RUN_FAILED"}:
            result = metadata.get("result") or payload.get("result")
    return {
        "lineage": lineage,
        "hitl_request": hitl_request,
        "hitl_response": hitl_response,
        "result_payload": result,
    }
