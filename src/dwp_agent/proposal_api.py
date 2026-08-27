from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status

from .policy import AskIdentity
from .proposal_contracts import (
    AgentProposalEnvelope,
    CreateAgentProposalRequest,
    DecideAgentProposalRequest,
    ProposalDecisionEnvelope,
    ProposalDecisionReceipt,
    ProposalInboxEnvelope,
    ProposalInboxPage,
    ProposalInboxView,
)
from .proposal_store import (
    ProposalConflict,
    ProposalCursorInvalid,
    ProposalNotFound,
    ProposalStoreUnavailable,
    get_proposal_store,
)
from .security import header_values, require_gateway_service, verified_ask_identity
from .workplace_actions import (
    WorkplaceActionInputInvalid,
    all_workplace_actions,
    review_workplace_action_inputs,
)


router = APIRouter(
    tags=["proposals"], dependencies=[Depends(require_gateway_service)]
)


def require_proposal_access(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
    identity_plane: Annotated[
        str | None, Header(alias="X-DWP-Identity-Plane")
    ] = None,
) -> None:
    if identity_plane != "TENANT" or "APP.ASK:VIEW" not in header_values(permissions):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON proposal access is required.",
        )


def require_proposal_producer(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
    identity_plane: Annotated[
        str | None, Header(alias="X-DWP-Identity-Plane")
    ] = None,
) -> None:
    authorities = set(header_values(permissions))
    if identity_plane != "TENANT" or not (
        {"ADMIN.DWAION_OPERATIONS:MANAGE", "ADMIN.DWAION:MANAGE"} & authorities
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON proposal producer access is required.",
        )


@router.get(
    "/v1/proposals",
    response_model=ProposalInboxEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_proposal_access)],
)
def list_proposals(
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    response: Response,
    view: ProposalInboxView = ProposalInboxView.ACTIVE,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
) -> ProposalInboxEnvelope:
    try:
        page = get_proposal_store().list(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            view=view,
            limit=limit,
            cursor=cursor,
        )
    except ProposalCursorInvalid as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The proposal cursor is invalid.",
        ) from error
    except ProposalStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent proposals are unavailable.",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return ProposalInboxEnvelope(
        data=ProposalInboxPage(
            items=page.items,
            summary=page.summary,
            next_cursor=page.next_cursor,
        )
    )


@router.post(
    "/v1/proposals/{proposal_id}/decisions",
    response_model=ProposalDecisionEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_proposal_access)],
)
def decide_proposal(
    proposal_id: UUID,
    request: DecideAgentProposalRequest,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    response: Response,
) -> ProposalDecisionEnvelope:
    try:
        proposal = get_proposal_store().decide(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            correlation_id=identity.correlation_id,
            proposal_id=proposal_id,
            request=request,
        )
    except ProposalNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The agent proposal is unavailable.",
        ) from error
    except ProposalConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ProposalStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent proposals are unavailable.",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return ProposalDecisionEnvelope(
        data=ProposalDecisionReceipt(
            proposal=proposal,
            action_review_required=proposal.action_key is not None,
        )
    )


@router.post(
    "/v1/admin/proposals",
    status_code=status.HTTP_201_CREATED,
    response_model=AgentProposalEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_proposal_producer)],
)
def create_proposal(
    request: CreateAgentProposalRequest,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    response: Response,
) -> AgentProposalEnvelope:
    request = _validated_request(request)
    try:
        proposal = get_proposal_store().create(
            tenant_id=identity.tenant_id,
            actor_user_id=identity.user_id,
            correlation_id=identity.correlation_id,
            request=request,
        )
    except ProposalConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ProposalStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent proposals are unavailable.",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return AgentProposalEnvelope(data=proposal)


def _validated_request(
    request: CreateAgentProposalRequest,
) -> CreateAgentProposalRequest:
    if request.action_key is None:
        if request.content.action_inputs:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Action inputs require a registered action key.",
            )
        return request
    action = next(
        (
            candidate
            for candidate in all_workplace_actions()
            if candidate.action_key == request.action_key
        ),
        None,
    )
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The proposal action is not registered.",
        )
    try:
        reviewed_inputs = review_workplace_action_inputs(
            action, request.content.action_inputs
        )
    except WorkplaceActionInputInvalid as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    return request.model_copy(
        update={
            "content": request.content.model_copy(
                update={"action_inputs": reviewed_inputs}
            )
        }
    )
