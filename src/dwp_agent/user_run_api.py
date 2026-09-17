from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from .policy import AskIdentity
from .security import header_values, require_gateway_service
from .security import verified_ask_identity
from .user_run_contracts import (
    AgentRunState,
    UserAgentRunEnvelope,
    UserAgentRunListEnvelope,
)
from .user_run_cursor import (
    InvalidUserRunCursor,
    decode_user_run_cursor,
    encode_user_run_cursor,
)
from .user_run_store import UserRunStoreUnavailable, get_user_run_store


router = APIRouter(
    prefix="/v1/runs",
    tags=["runs"],
    dependencies=[Depends(require_gateway_service)],
)


def require_run_access(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> None:
    if "APP.ASK:VIEW" not in header_values(permissions):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON access is required.",
        )


@router.get(
    "",
    response_model=UserAgentRunListEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_run_access)],
)
def list_user_runs(
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    limit: int = 50,
    state: AgentRunState | None = None,
    from_at: Annotated[datetime | None, Query(alias="from")] = None,
    to_at: Annotated[datetime | None, Query(alias="to")] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> UserAgentRunListEnvelope:
    if any(value is not None and value.tzinfo is None for value in (from_at, to_at)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Agent run time filters must include a UTC offset.",
        )
    normalized_from = from_at.astimezone(timezone.utc) if from_at else None
    normalized_to = to_at.astimezone(timezone.utc) if to_at else None
    if normalized_from is not None and normalized_to is not None and normalized_from >= normalized_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Agent run time range is invalid.",
        )
    now = datetime.now(timezone.utc)
    try:
        snapshot_at, after = decode_user_run_cursor(
            cursor,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            run_state=state,
            from_at=normalized_from,
            to_at=normalized_to,
            now=now,
        ) if cursor else (now, None)
        runs, has_more = get_user_run_store().page(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            limit=max(1, min(limit, 100)),
            run_state=state,
            from_at=normalized_from,
            to_at=normalized_to,
            snapshot_at=snapshot_at,
            after=after,
        )
        next_cursor = (
            encode_user_run_cursor(
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                run_state=state,
                from_at=normalized_from,
                to_at=normalized_to,
                snapshot_at=snapshot_at,
                after=(runs[-1].created_at, runs[-1].run_id),
            )
            if has_more and runs
            else None
        )
    except InvalidUserRunCursor as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    except UserRunStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    return UserAgentRunListEnvelope(
        data=runs,
        snapshot_at=snapshot_at,
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/{run_id}",
    response_model=UserAgentRunEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_run_access)],
)
def get_user_run(
    run_id: UUID,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
) -> UserAgentRunEnvelope:
    try:
        run = get_user_run_store().get(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            run_id=run_id,
        )
    except UserRunStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent run is unavailable.",
        )
    return UserAgentRunEnvelope(data=run)
