from __future__ import annotations

import os
from hmac import compare_digest
from typing import Annotated, Callable, TypeVar
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, Response, status

from .attachment_evidence_contracts import AttachmentEvidenceEnvelope
from .dwaion_workflow_contracts import (
    AttachmentCapabilitiesEnvelope,
    AttachmentEnvelope,
    AttachmentListEnvelope,
    AttachmentWorkerObservation,
    CompleteAttachmentUploadRequest,
    CreateAttachmentRequest,
    CreateProposalHandoffRequest,
    CreateResearchDeliveryRequest,
    CreateResearchPlanRequest,
    DeleteAttachmentRequest,
    ExecuteResearchRunRequest,
    ProposalHandoffEnvelope,
    ProposalHandoffObservation,
    ResearchDeliveryEnvelope,
    ResearchDeliveryListEnvelope,
    ResearchDeliveryObservation,
    ResearchDeliveryType,
    ResearchCapabilities,
    ResearchCapabilitiesEnvelope,
    ResearchPlanEnvelope,
    ResearchRunCommandRequest,
    ResearchRunEnvelope,
    ResearchWorkerObservation,
    StartResearchRunRequest,
    UpdateResearchPlanRequest,
    WorkflowCapability,
)
from .dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
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
from .research_delivery_store import get_research_delivery_store
from .research_plan_store import get_research_plan_store
from .research_executor import ResearchExecutor
from .research_run_store import get_research_run_store
from .secure_attachment_store import get_secure_attachment_store
from .workspace_authorization import resolve_workspace_request_authorization
from .workplace_actions import (
    WorkplaceActionForbidden,
    WorkplaceActionInputInvalid,
    WorkplaceActionNotFound,
    resolve_workplace_action,
    review_workplace_action_inputs,
)


router = APIRouter(dependencies=personal_domain_dependencies)
T = TypeVar("T")


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
    "/v1/attachments/capabilities",
    response_model=AttachmentCapabilitiesEnvelope,
    tags=["attachments"],
)
def attachment_capabilities(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentCapabilitiesEnvelope:
    _attachment_access(identity, write=False)
    _no_store(response)
    return AttachmentCapabilitiesEnvelope(data=_run(get_secure_attachment_store().capabilities))


@router.get("/v1/attachments", response_model=AttachmentListEnvelope, tags=["attachments"])
def list_attachments(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentListEnvelope:
    _attachment_access(identity, write=False)
    _no_store(response)
    return AttachmentListEnvelope(data=_run(lambda: get_secure_attachment_store().list(identity)))


@router.post(
    "/v1/attachments",
    status_code=status.HTTP_201_CREATED,
    response_model=AttachmentEnvelope,
    tags=["attachments"],
)
def create_attachment(
    request: CreateAttachmentRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentEnvelope:
    _attachment_access(identity, write=True)
    _no_store(response)
    return AttachmentEnvelope(data=_run(lambda: get_secure_attachment_store().create(identity, request)))


@router.get("/v1/attachments/{attachment_id}", response_model=AttachmentEnvelope, tags=["attachments"])
def get_attachment(
    attachment_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentEnvelope:
    _attachment_access(identity, write=False)
    _no_store(response)
    return AttachmentEnvelope(data=_run(lambda: get_secure_attachment_store().get(identity, attachment_id)))


@router.get(
    "/v1/attachments/{attachment_id}/evidence",
    response_model=AttachmentEvidenceEnvelope,
    tags=["attachments"],
)
def get_attachment_evidence(
    attachment_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentEvidenceEnvelope:
    _attachment_access(identity, write=False)
    _no_store(response)
    return AttachmentEvidenceEnvelope(
        data=_run(lambda: get_secure_attachment_store().evidence(identity, attachment_id))
    )


@router.post("/v1/attachments/{attachment_id}/complete", response_model=AttachmentEnvelope, tags=["attachments"])
def complete_attachment(
    attachment_id: UUID,
    request: CompleteAttachmentUploadRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentEnvelope:
    _attachment_access(identity, write=True)
    _no_store(response)
    return AttachmentEnvelope(data=_run(lambda: get_secure_attachment_store().complete_upload(identity, attachment_id, request)))


@router.delete("/v1/attachments/{attachment_id}", response_model=AttachmentEnvelope, tags=["attachments"])
def delete_attachment(
    attachment_id: UUID,
    request: Annotated[DeleteAttachmentRequest, Body()],
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AttachmentEnvelope:
    _attachment_access(identity, write=True)
    _no_store(response)
    return AttachmentEnvelope(data=_run(lambda: get_secure_attachment_store().delete(identity, attachment_id, request)))


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
    def unavailable(code: str, hint: str) -> WorkflowCapability:
        return WorkflowCapability(
            available=False, configured=False, reason_code=code, recovery_hint=hint,
        )
    return ResearchCapabilitiesEnvelope(data=ResearchCapabilities(
        raw_export=available,
        pdf_export=unavailable("RESEARCH_PDF_EXPORT_NOT_CONFIGURED", "Configure the governed research export renderer."),
        receipt_download=available,
        audit_download=available,
        fork=unavailable("RESEARCH_FORK_NOT_CONFIGURED", "Configure the governed research branch worker."),
        merge=unavailable("RESEARCH_MERGE_NOT_CONFIGURED", "Configure the governed research merge worker."),
        keep_local=unavailable("RESEARCH_KEEP_LOCAL_NOT_CONFIGURED", "Configure the governed research conflict resolver."),
        sensitivity_recalculation=unavailable("RESEARCH_SENSITIVITY_NOT_CONFIGURED", "Configure the governed sensitivity classifier."),
        cache_fallback=unavailable("RESEARCH_CACHE_FALLBACK_NOT_CONFIGURED", "Configure an attested research cache provider."),
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
def command_research_run(run_id: UUID, request: ResearchRunCommandRequest, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)], response: Response) -> ResearchRunEnvelope:
    _research_access(identity, write=True)
    _no_store(response)
    return ResearchRunEnvelope(data=_run(lambda: get_research_run_store().command(identity, run_id, request)))


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


def _attachment_access(identity: PersonalDomainIdentity, *, write: bool) -> None:
    identity.require("APP.ASK:VIEW")
    identity.require("APP.DWAION_ATTACHMENTS:MANAGE" if write else "APP.DWAION_ATTACHMENTS:VIEW")


def _research_access(identity: PersonalDomainIdentity, *, write: bool) -> None:
    identity.require("APP.ASK:VIEW")
    identity.require("APP.DWAION_RESEARCH:MANAGE" if write else "APP.DWAION_RESEARCH:VIEW")


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


def _run(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except DwaionWorkflowNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DwaionWorkflowConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except DwaionWorkflowUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


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


@internal_router.post("/research/runs/{run_id}/observations", response_model=ResearchRunEnvelope)
def observe_research_run(run_id: UUID, request: ResearchWorkerObservation, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)]) -> ResearchRunEnvelope:
    return ResearchRunEnvelope(data=_run(lambda: get_research_run_store().observe(identity, run_id, request)))


@internal_router.post("/research/runs/{run_id}/deliveries/{delivery_id}/observations", response_model=ResearchDeliveryEnvelope)
def observe_research_delivery(run_id: UUID, delivery_id: UUID, request: ResearchDeliveryObservation, identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)]) -> ResearchDeliveryEnvelope:
    return ResearchDeliveryEnvelope(data=_run(lambda: get_research_delivery_store().observe(identity, run_id, delivery_id, request)))
