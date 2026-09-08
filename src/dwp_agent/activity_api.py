from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from .activity_contracts import (
    ActivityEvent, ActivityEventEnvelope, ActivityPage, ActivityPageEnvelope,
    ActivityRunSnapshot, ExecutionSummary, ExecutionSummaryEnvelope,
)
from .activity_cursor import InvalidActivityCursor, decode_cursor, encode_cursor
from .activity_store import ActivityFilters, ActivityStoreUnavailable, get_agent_activity_store
from .policy import AskIdentity
from .security import header_values, require_gateway_service, verified_ask_identity


def require_activity_access(request: Request) -> None:
    headers = request.headers
    guarded = ("X-DWP-Identity-Plane", "X-DWP-Active-Access-Mode", "X-DWP-Support-Session-ID",
               "X-DWP-Tenant-ID", "X-DWP-User-ID", "X-DWP-Permissions", "X-DWP-Roles",
               "X-DWP-Rollout-State")
    if any(len(headers.getlist(name)) > 1 for name in guarded):
        raise HTTPException(403, "Workspace activity identity is invalid.")
    permissions = set(header_values(headers.get("X-DWP-Permissions")))
    roles = header_values(headers.get("X-DWP-Roles"))
    tenant = headers.get("X-DWP-Tenant-ID", "")
    if (headers.get("X-DWP-Identity-Plane") != "TENANT"
            or not {"APP.ACTIVITY:VIEW", "APP.ASK:VIEW"}.issubset(permissions)
            or any(role.startswith("PROVIDER_") for role in roles)
            or headers.get("X-DWP-Support-Session-ID", "").strip()
            or headers.get("X-DWP-Active-Access-Mode", "NORMAL") not in {"NORMAL", "ELEVATED"}
            or len(tenant) > 32 or not tenant.isascii() or not tenant.isdecimal() or int(tenant) <= 0):
        raise HTTPException(403, "Personal Activity and DWAI-ON access are required.")


router = APIRouter(prefix="/v1/activity", tags=["activity"],
                   dependencies=[Depends(require_gateway_service), Depends(require_activity_access)])


def activity_filters(
    actor: Annotated[str, Query(max_length=40)] = "ALL",
    state: Annotated[str, Query(max_length=40)] = "ALL",
    query: Annotated[str, Query(max_length=200)] = "",
    q: Annotated[str, Query(max_length=200)] = "",
    source: Annotated[str, Query(max_length=120)] = "",
    object_type: Annotated[str, Query(alias="objectType", max_length=80)] = "",
    object_id: Annotated[str, Query(alias="objectId", max_length=160)] = "",
    execution_id: Annotated[str, Query(alias="executionId", max_length=160)] = "",
    from_at: Annotated[datetime | None, Query(alias="from")] = None,
    to_at: Annotated[datetime | None, Query(alias="to")] = None,
) -> ActivityFilters:
    if any(value is not None and value.tzinfo is None for value in (from_at, to_at)):
        raise HTTPException(422, "Activity time filters must include a UTC offset.")
    if from_at is not None and to_at is not None and from_at >= to_at:
        raise HTTPException(422, "Activity time range is invalid.")
    return ActivityFilters(actor=actor.strip().upper(), state=state.strip().upper().replace("-", "_"),
                           query=(query or q).strip(), source=source.strip(), object_type=object_type.strip().upper(),
                           object_id=object_id.strip(), execution_id=execution_id.strip(),
                           from_at=from_at.astimezone(timezone.utc) if from_at else None,
                           to_at=to_at.astimezone(timezone.utc) if to_at else None)


def _event(row: ActivityRunSnapshot, *, now: datetime, locale: str, resume_cursor: str | None = None) -> ActivityEvent:
    korean = locale.lower().startswith("ko")
    state = row.activity_state(now)
    return ActivityEvent(
        id=row.run_id, occurred_at=row.created_at, state=state,
        title="DWAI·ON 에이전트 실행" if korean else "DWAI·ON Agent execution",
        summary=("질문·답변·출처 내용 없이 원장의 현재 실행 상태를 표시합니다." if korean else
                 "Current execution state from the source ledger; question, answer and source content are excluded."),
        object_label=row.agent_key, source_route=f"/dwaion/activity?run={row.run_id}",
        source_event_id=str(row.run_id), object_id=str(row.run_id), execution_id=str(row.run_id),
        execution_version=row.execution_version(now), attempt=max(1, row.generation),
        progress=row.progress_percent,
        audit_id=row.audit_id,
        audit_record_id=row.audit_record_id,
        audit_status=row.audit_link_state or "NOT_LINKED",
        data_provenance=row.data_provenance,
        resume_cursor=resume_cursor, source_observed_at=now,
        updated_at=row.completed_at,
    )


@router.get("/events", response_model=ActivityPageEnvelope, response_model_by_alias=True)
def list_activity_events(
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    filters: Annotated[ActivityFilters, Depends(activity_filters)],
    locale: Annotated[str, Header(alias="Accept-Language")] = "en",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> ActivityPageEnvelope:
    now = datetime.now(timezone.utc)
    try:
        snapshot_at, after = decode_cursor(cursor, tenant_id=identity.tenant_id, user_id=identity.user_id,
                                          filters=filters, now=now) if cursor else (now, None)
        rows, has_more = get_agent_activity_store().page(
            tenant_id=identity.tenant_id, user_id=identity.user_id, filters=filters,
            snapshot_at=snapshot_at, now=now, after=after, limit=limit)
        def position(row: ActivityRunSnapshot | None) -> str:
            return encode_cursor(tenant_id=identity.tenant_id, user_id=identity.user_id,
                                 filters=filters, snapshot_at=snapshot_at,
                                 after=(row.created_at, row.run_id) if row else None)
        start_cursor = position(None)
        events = [_event(row, now=now, locale=locale, resume_cursor=position(row)) for row in rows]
    except InvalidActivityCursor as error:
        raise HTTPException(400, str(error)) from error
    except ActivityStoreUnavailable as error:
        raise HTTPException(503, str(error)) from error
    return ActivityPageEnvelope(data=ActivityPage(
        events=events, generated_at=now, snapshot_at=snapshot_at, start_cursor=start_cursor,
        next_cursor=events[-1].resume_cursor if has_more and events else None, has_more=has_more))


@router.get("/events/{event_id}", response_model=ActivityEventEnvelope, response_model_by_alias=True)
def activity_event_detail(
    event_id: UUID, identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    locale: Annotated[str, Header(alias="Accept-Language")] = "en",
) -> ActivityEventEnvelope:
    try:
        row = get_agent_activity_store().detail(tenant_id=identity.tenant_id, user_id=identity.user_id, run_id=event_id)
    except ActivityStoreUnavailable as error:
        raise HTTPException(503, str(error)) from error
    if row is None:
        raise HTTPException(404, "Agent execution is unavailable.")
    return ActivityEventEnvelope(data=_event(row, now=datetime.now(timezone.utc), locale=locale))


@router.get("/executions/summary", response_model=ExecutionSummaryEnvelope, response_model_by_alias=True)
def activity_execution_summary(
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    filters: Annotated[ActivityFilters, Depends(activity_filters)],
) -> ExecutionSummaryEnvelope:
    now = datetime.now(timezone.utc)
    try:
        counts = get_agent_activity_store().counts(tenant_id=identity.tenant_id, user_id=identity.user_id,
                                                  filters=filters, now=now)
    except ActivityStoreUnavailable as error:
        raise HTTPException(503, str(error)) from error
    return ExecutionSummaryEnvelope(data=ExecutionSummary(
        total=sum(counts.values()), running=counts.get("RUNNING", 0), needs_input=counts.get("NEEDS_INPUT", 0),
        policy_blocked=counts.get("POLICY_BLOCKED", 0), completed=counts.get("COMPLETED", 0),
        failed=counts.get("FAILED", 0), unknown=counts.get("UNKNOWN", 0), generated_at=now))
