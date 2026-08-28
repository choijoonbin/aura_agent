from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status

from .delivery_gate import (
    DeliveryCapability,
    OperationalDeliveryConfigurationError,
    OperationalDeliveryNotReady,
    require_delivery_capability,
)
from .policy import AskIdentity
from .proposal_analysis import (
    ContextBrokerUnavailable,
    get_proposal_analysis_service,
)
from .proposal_analysis_commands import (
    ProposalAnalysisCommandUnavailable,
    ProposalAnalysisInProgress,
    ProposalAnalysisRateLimited,
    ProposalAnalysisReplay,
)
from .proposal_analysis_contracts import (
    AnalyzeProposalsRequest,
    ClearProposalInboxEnvelope,
    ClearProposalInboxRequest,
    ProposalAnalysisEnvelope,
    ProposalAnalysisPreferenceEnvelope,
    UpdateProposalAnalysisPreferenceRequest,
)
from .proposal_analysis_control import (
    ProposalAnalysisControlUnavailable,
    ProposalAnalysisDisabled,
    ProposalAnalysisPreferenceConflict,
    get_proposal_analysis_control,
)
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
from .proposal_privacy import (
    ProposalPrivacyUnavailable,
    get_proposal_privacy_service,
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


def require_proposal_analysis_delivery(identity: AskIdentity) -> None:
    try:
        require_delivery_capability(
            tenant_id=identity.tenant_id,
            capability=DeliveryCapability.ASK,
            actor_user_id=identity.user_id,
        )
    except (OperationalDeliveryConfigurationError, OperationalDeliveryNotReady) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DWAI-ON analysis is not approved for this environment.",
        ) from error


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
    "/v1/proposals/analyze",
    response_model=ProposalAnalysisEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_proposal_access)],
)
def analyze_proposals(
    request: AnalyzeProposalsRequest,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    response: Response,
    auth_session_id: Annotated[
        str, Header(alias="X-DWP-Auth-Session-ID", min_length=1, max_length=160)
    ],
    accept_language: Annotated[
        str | None, Header(alias="Accept-Language", max_length=100)
    ] = None,
) -> ProposalAnalysisEnvelope:
    require_proposal_analysis_delivery(identity)
    try:
        receipt = get_proposal_analysis_service().analyze(
            identity=identity,
            command_id=request.command_id,
            locale=_proposal_locale(accept_language),
            auth_session_id=auth_session_id,
        )
    except ProposalAnalysisDisabled as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ProposalAnalysisInProgress as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
            headers={"Retry-After": "5"},
        ) from error
    except ProposalAnalysisReplay as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ProposalAnalysisRateLimited as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
            headers={"Retry-After": "30"},
        ) from error
    except (
        ContextBrokerUnavailable,
        ProposalAnalysisCommandUnavailable,
        ProposalAnalysisControlUnavailable,
        ProposalStoreUnavailable,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workspace analysis is unavailable.",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return ProposalAnalysisEnvelope(data=receipt)


@router.get(
    "/v1/proposals/preferences",
    response_model=ProposalAnalysisPreferenceEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_proposal_access)],
)
def get_proposal_preferences(
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    response: Response,
) -> ProposalAnalysisPreferenceEnvelope:
    try:
        preference = get_proposal_analysis_control().preference(
            tenant_id=identity.tenant_id, user_id=identity.user_id
        )
    except ProposalAnalysisControlUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Proposal preferences are unavailable.",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return ProposalAnalysisPreferenceEnvelope(data=preference)


@router.put(
    "/v1/proposals/preferences",
    response_model=ProposalAnalysisPreferenceEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_proposal_access)],
)
def update_proposal_preferences(
    request: UpdateProposalAnalysisPreferenceRequest,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    response: Response,
) -> ProposalAnalysisPreferenceEnvelope:
    try:
        preference = get_proposal_analysis_control().update_preference(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            actor_user_id=identity.user_id,
            correlation_id=identity.correlation_id,
            command_id=request.command_id,
            expected_revision=request.expected_revision,
            enabled=request.proactive_analysis_enabled,
        )
    except ProposalAnalysisPreferenceConflict as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ProposalAnalysisControlUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Proposal preferences are unavailable.",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return ProposalAnalysisPreferenceEnvelope(data=preference)


@router.post(
    "/v1/proposals/clear",
    response_model=ClearProposalInboxEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_proposal_access)],
)
def clear_proposal_inbox(
    request: ClearProposalInboxRequest,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    response: Response,
) -> ClearProposalInboxEnvelope:
    try:
        receipt = get_proposal_privacy_service().clear(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            correlation_id=identity.correlation_id,
            command_id=request.command_id,
        )
    except ProposalPrivacyUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Proposal privacy controls are unavailable.",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return ClearProposalInboxEnvelope(data=receipt)


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


def _proposal_locale(value: str | None) -> str:
    return "ko" if (value or "").lower().startswith("ko") else "en"
