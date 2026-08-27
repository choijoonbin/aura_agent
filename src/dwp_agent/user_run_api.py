from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from .policy import AskIdentity
from .security import header_values, require_gateway_service
from .security import verified_ask_identity
from .user_run_contracts import AgentRunState, UserAgentRunListEnvelope
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
    response: Response,
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
    response.headers["Cache-Control"] = "no-store"
    return UserAgentRunListEnvelope(data=runs)
