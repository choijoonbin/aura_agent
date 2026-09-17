from __future__ import annotations

import os
from hmac import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from .attachment_stage_contracts import AttachmentWorkerObservation

from .dwaion_workflow_api_support import (
    prevent_response_storage as _no_store,
    require_research_access as _research_access,
    run_workflow as _run,
)
from .dwaion_workflow_contracts import (
    AttachmentEnvelope,
    CreateProposalHandoffRequest,
    CreateResearchDeliveryRequest,
    CreateResearchPlanRequest,
    ExecuteResearchRunRequest,
    ProposalHandoffEnvelope,
    ProposalHandoffObservation,
    ResearchDeliveryEnvelope,
    ResearchDeliveryListEnvelope,
    ResearchDeliveryType,
    ResearchCapabilities,
    ResearchCapabilitiesEnvelope,
    ResearchPlanEnvelope,
    ResearchRunCommandRequest,
    ResearchRunEnvelope,
    StartResearchRunRequest,
    UpdateResearchPlanRequest,
    WorkflowCapability,
)
from .governance_contracts import ActionExecutionPolicy
from .governance_store import GovernanceStoreUnavailable, get_governance_store
from .personal_domain_security import (
    PersonalDomainIdentity,
    personal_domain_dependencies,
    require_personal_domain_identity,
)
from .proposal_handoff_store import (
    find_user_proposal,
    get_proposal_handoff_store,
    require_accepted_proposal,
)
from .proposal_handoff_draft_contracts import (
    ProposalHandoffDraftEnvelope,
    SaveProposalHandoffDraftRequest,
)
from .proposal_handoff_draft_store import get_proposal_handoff_draft_store
from .research_delivery_store import get_research_delivery_store
from .research_delivery_capabilities import research_delivery_capabilities
from .research_recovery_contracts import (
    ResearchRecoveryCommandRequest,
    ResearchRecoveryReceiptEnvelope,
)
from .research_recovery_store import get_research_recovery_store
from .research_plan_store import get_research_plan_store
from .research_executor import ResearchExecutor
from .research_run_store import get_research_run_store
from .secure_attachment_store import get_secure_attachment_store
from .secure_attachment_api import router as secure_attachment_router
from .workspace_authorization import resolve_workspace_request_authorization
from .workplace_actions import (
    WorkplaceActionForbidden,
    WorkplaceActionInputInvalid,
    WorkplaceActionNotFound,
    resolve_workplace_action,
    review_workplace_action_inputs,
)


router = APIRouter(dependencies=personal_domain_dependencies)
router.include_router(secure_attachment_router)


@router.post(
    "/v1/proposals/{proposal_id}/handoff",
    response_model=ProposalHandoffEnvelope,
    tags=["proposals"],
)
def create_proposal_handoff(
    proposal_id: UUID,
    request: CreateProposalHandoffRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ProposalHandoffEnvelope:
    identity.require("APP.ASK:VIEW")
    proposal = _run(lambda: find_user_proposal(identity, proposal_id))
    _run(lambda: require_accepted_proposal(proposal, request.expected_version))
    try:
        action = resolve_workplace_action(proposal.action_key or "", tuple(identity.permissions))
        inputs = request.reviewed_inputs or proposal.content.action_inputs
        reviewed = review_workplace_action_inputs(action, inputs)
        _require_action_policy(identity, action.action_key)
    except WorkplaceActionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkplaceActionForbidden as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except WorkplaceActionInputInvalid as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    _no_store(response)
    return ProposalHandoffEnvelope(
        data=_run(
            lambda: get_proposal_handoff_store().create(
                identity,
                proposal_id,
                request,
                action_key=action.action_key,
                target_route=action.target_route,
                approval_required=action.confirmation_required,
                reviewed_inputs=reviewed,
            )
        )
    )


@router.get(
    "/v1/proposals/{proposal_id}/handoff",
    response_model=ProposalHandoffEnvelope,
    tags=["proposals"],
)
def get_proposal_handoff(
    proposal_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ProposalHandoffEnvelope:
    identity.require("APP.ASK:VIEW")
    _no_store(response)
    return ProposalHandoffEnvelope(
        data=_run(lambda: get_proposal_handoff_store().by_proposal(identity, proposal_id))
    )


@router.get(
    "/v1/proposal-handoffs/{handoff_id}",
    response_model=ProposalHandoffEnvelope,
    tags=["proposals"],
)
def get_handoff(
    handoff_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ProposalHandoffEnvelope:
    identity.require("APP.ASK:VIEW")
    _no_store(response)
    return ProposalHandoffEnvelope(
        data=_run(lambda: get_proposal_handoff_store().get(identity, handoff_id))
    )


@router.get(
    "/v1/proposal-handoffs/{handoff_id}/drafts/current",
    response_model=ProposalHandoffDraftEnvelope,
    tags=["proposals"],
)
def get_proposal_handoff_draft(
    handoff_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ProposalHandoffDraftEnvelope:
    identity.require("APP.ASK:VIEW")
    _no_store(response)
    return ProposalHandoffDraftEnvelope(
        data=_run(
            lambda: get_proposal_handoff_draft_store().current(identity, handoff_id)
        )
    )


@router.put(
    "/v1/proposal-handoffs/{handoff_id}/drafts/current",
    response_model=ProposalHandoffDraftEnvelope,
    tags=["proposals"],
)
def save_proposal_handoff_draft(
    handoff_id: UUID,
    request: SaveProposalHandoffDraftRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ProposalHandoffDraftEnvelope:
    identity.require("APP.ASK:VIEW")
    _no_store(response)
    return ProposalHandoffDraftEnvelope(
        message="Proposal handoff draft saved.",
        data=_run(
            lambda: get_proposal_handoff_draft_store().save(
                identity, handoff_id, request
            )
        ),
    )


@router.post("/v1/research/plans", status_code=201, response_model=ResearchPlanEnvelope, tags=["research"])
def create_research_plan(request: CreateResearchPlanRequest, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)], response: Response) -> ResearchPlanEnvelope:
    _research_access(identity, write=True)
    _no_store(response)
    return ResearchPlanEnvelope(data=_run(lambda: get_research_plan_store().create(identity, request)))


@router.get(
    "/v1/research/capabilities",
    response_model=ResearchCapabilitiesEnvelope,
    tags=["research"],
)
def research_capabilities(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ResearchCapabilitiesEnvelope:
    _research_access(identity, write=False)
    _no_store(response)
    available = WorkflowCapability(available=True, configured=True)
    return ResearchCapabilitiesEnvelope(data=ResearchCapabilities(
        raw_export=available,
        pdf_export=available,
        receipt_download=available,
        audit_download=available,
        fork=available,
        merge=available,
        keep_local=available,
        sensitivity_recalculation=available,
        cache_fallback=available,
        delivery=research_delivery_capabilities(
            os.getenv("DWP_AGENT_DATABASE_URL", "").strip(), identity
        ),
    ))


@router.get("/v1/research/plans/{plan_id}", response_model=ResearchPlanEnvelope, tags=["research"])
def get_research_plan(plan_id: UUID, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)], response: Response) -> ResearchPlanEnvelope:
    _research_access(identity, write=False)
    _no_store(response)
    return ResearchPlanEnvelope(data=_run(lambda: get_research_plan_store().get(identity, plan_id)))


@router.put("/v1/research/plans/{plan_id}", response_model=ResearchPlanEnvelope, tags=["research"])
def update_research_plan(plan_id: UUID, request: UpdateResearchPlanRequest, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)], response: Response) -> ResearchPlanEnvelope:
    _research_access(identity, write=True)
    _no_store(response)
    return ResearchPlanEnvelope(data=_run(lambda: get_research_plan_store().update(identity, plan_id, request)))


@router.post("/v1/research/plans/{plan_id}/runs", status_code=201, response_model=ResearchRunEnvelope, tags=["research"])
def start_research_run(plan_id: UUID, request: StartResearchRunRequest, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)], response: Response) -> ResearchRunEnvelope:
    _research_access(identity, write=True)
    _no_store(response)
    return ResearchRunEnvelope(data=_run(lambda: get_research_run_store().start(identity, plan_id, request)))


@router.get("/v1/research/runs/{run_id}", response_model=ResearchRunEnvelope, tags=["research"])
def get_research_run(run_id: UUID, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)], response: Response) -> ResearchRunEnvelope:
    _research_access(identity, write=False)
    _no_store(response)
    return ResearchRunEnvelope(data=_run(lambda: get_research_run_store().get(identity, run_id)))


@router.post("/v1/research/runs/{run_id}/commands", response_model=ResearchRunEnvelope, tags=["research"])
def command_research_run(run_id: UUID, request: ResearchRunCommandRequest, http_request: Request, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)], response: Response) -> ResearchRunEnvelope:
    _research_access(identity, write=True)
    _no_store(response)
    return ResearchRunEnvelope(data=_run(lambda: get_research_run_store().command(
        identity, run_id, request, resolve_workspace_request_authorization(http_request)
    )))


@router.post(
    "/v1/research/runs/{run_id}/recovery-actions",
    response_model=ResearchRecoveryReceiptEnvelope,
    tags=["research"],
)
def recover_research_run(
    run_id: UUID,
    request: ResearchRecoveryCommandRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ResearchRecoveryReceiptEnvelope:
    _research_access(identity, write=True)
    _no_store(response)
    return ResearchRecoveryReceiptEnvelope(
        data=_run(
            lambda: get_research_recovery_store().execute(identity, run_id, request)
        )
    )


@router.post("/v1/research/runs/{run_id}/execute", response_model=ResearchRunEnvelope, tags=["research"])
def execute_research_run(
    run_id: UUID,
    request: ExecuteResearchRunRequest,
    http_request: Request,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ResearchRunEnvelope:
    _research_access(identity, write=True)
    _no_store(response)
    return ResearchRunEnvelope(
        data=_run(
            lambda: ResearchExecutor().execute(
                identity,
                run_id,
                request,
                resolve_workspace_request_authorization(http_request),
            )
        )
    )


@router.get("/v1/research/runs/{run_id}/deliveries", response_model=ResearchDeliveryListEnvelope, tags=["research"])
def list_research_deliveries(run_id: UUID, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)], response: Response) -> ResearchDeliveryListEnvelope:
    _research_access(identity, write=False)
    _no_store(response)
    return ResearchDeliveryListEnvelope(data=_run(lambda: get_research_delivery_store().list(identity, run_id)))


@router.get("/v1/research/runs/{run_id}/deliveries/{delivery_id}", response_model=ResearchDeliveryEnvelope, tags=["research"])
def get_research_delivery(run_id: UUID, delivery_id: UUID, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)], response: Response) -> ResearchDeliveryEnvelope:
    _research_access(identity, write=False)
    _no_store(response)
    return ResearchDeliveryEnvelope(data=_run(lambda: get_research_delivery_store().get(identity, run_id, delivery_id)))


def _delivery_route(delivery_type: ResearchDeliveryType, suffix: str):
    def endpoint(run_id: UUID, request: CreateResearchDeliveryRequest, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)], response: Response) -> ResearchDeliveryEnvelope:
        _research_access(identity, write=True)
        _no_store(response)
        return ResearchDeliveryEnvelope(data=_run(lambda: get_research_delivery_store().create(identity, run_id, delivery_type, request)))
    endpoint.__name__ = f"create_research_{delivery_type.value.lower()}"
    router.add_api_route(
        f"/v1/research/runs/{{run_id}}/{suffix}", endpoint,
        methods=["POST"], response_model=ResearchDeliveryEnvelope, tags=["research"],
    )


for _type, _suffix in (
    (ResearchDeliveryType.ARTIFACT, "artifact"),
    (ResearchDeliveryType.PROPOSAL, "proposal"),
    (ResearchDeliveryType.EXPORT, "exports"),
    (ResearchDeliveryType.HANDOFF, "handoffs"),
    (ResearchDeliveryType.SHARE, "shares"),
    (ResearchDeliveryType.ROUTINE, "routines"),
):
    _delivery_route(_type, _suffix)


def _require_action_policy(identity: PersonalDomainIdentity, action_key: str) -> None:
    try:
        policy = next(
            (
                item
                for item in get_governance_store().action_policies(
                    tenant_id=str(identity.tenant_id), actor_user_id=identity.user_id
                )
                if item.action_key == action_key
            ),
            None,
        )
    except GovernanceStoreUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if policy is None or not policy.enabled or policy.execution_policy == ActionExecutionPolicy.BLOCKED:
        raise HTTPException(status_code=403, detail="The proposal action is disabled by DWAI-ON governance.")


def require_workflow_worker(
    token: Annotated[str | None, Header(alias="X-DWP-Workflow-Worker-Token")] = None,
) -> None:
    expected = os.getenv("DWP_WORKFLOW_WORKER_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Workflow worker identity is not configured.")
    if token is None or not compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid workflow worker identity.")


internal_router = APIRouter(
    prefix="/internal/v1",
    include_in_schema=False,
    # Worker authority supplements the normal service/delegated-identity boundary;
    # it never replaces it for tenant-personal observations.
    dependencies=[Depends(require_workflow_worker), *personal_domain_dependencies],
)


@internal_router.post("/proposal-handoffs/{handoff_id}/observations", response_model=ProposalHandoffEnvelope)
def observe_handoff(handoff_id: UUID, request: ProposalHandoffObservation, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)]) -> ProposalHandoffEnvelope:
    return ProposalHandoffEnvelope(data=_run(lambda: get_proposal_handoff_store().observe(identity, handoff_id, request)))


@internal_router.post("/attachments/{attachment_id}/observations", response_model=AttachmentEnvelope)
def observe_attachment(attachment_id: UUID, request: AttachmentWorkerObservation, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)]) -> AttachmentEnvelope:
    return AttachmentEnvelope(data=_run(lambda: get_secure_attachment_store().observe(identity, attachment_id, request)))
