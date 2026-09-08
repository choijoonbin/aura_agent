from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status

from .policy import AskIdentity
from .security import header_values, require_gateway_service
from .security import verified_ask_identity
from .user_run_contracts import (
    AgentRunState,
    UserAgentRunEnvelope,
    UserAgentRunListEnvelope,
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
) -> UserAgentRunListEnvelope:
    try:
        runs = get_user_run_store().list(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            limit=max(1, min(limit, 100)),
            run_state=state,
        )
    except UserRunStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    return UserAgentRunListEnvelope(data=runs)


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
