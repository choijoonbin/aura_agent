from __future__ import annotations

import os
import json
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from .admin_authority import AdminPreflightDenied, require_admin_preflight
from .admin_commands import resolve_admin_command
from .ask_runtime import AskRuntime
from .action_api import router as action_router
from .audit import record_plan_preview
from .contracts import AskEnvelope, AskRequest, PlanPreviewEnvelope, PlanPreviewRequest
from .contracts import (
    AnswerFeedbackRequest,
    ConversationEnvelope,
    ConversationListEnvelope,
    FeedbackEnvelope,
    RenameConversationRequest,
)
from .conversation_store import (
    ConversationNotFound,
    ConversationRetentionLocked,
    get_conversation_store,
)
from .delivery_gate import (
    DeliveryCapability,
    OperationalDeliveryConfigurationError,
    OperationalDeliveryNotReady,
    require_delivery_capability,
    validate_delivery_gate_runtime,
)
from .policy import AskIdentity, SafetyControls
from .planner import build_reference_plan
from .registry import RegistryResolutionError, resolve_agent
from .readiness import validate_runtime_configuration
from .observability import install_api_history
from .operations_api import router as operations_router
from .question_launch_api import router as question_launch_router
from .proposal_api import router as proposal_router
from .question_launch_store import MAINTENANCE as question_launch_maintenance
from .governed_worker_runtime import MAINTENANCE as governed_worker_maintenance
from .governance_api import router as governance_router
from .governance_safety_api import router as governance_safety_router
from .operational_gate_api import (
    install_operational_gate_problem_handler,
    router as operational_gate_router,
)
from .governance_store import GovernanceStoreUnavailable
from .meeting_intelligence_api import router as meeting_intelligence_router
from .meeting_intelligence_body_limit import install_meeting_intelligence_body_limit
from .meeting_media_api import router as meeting_media_router
from .meeting_media_body_limit import install_meeting_media_body_limit
from .local_governance_seed import seed_local_governance
from .local_activity_seed import seed_local_activity
from .meeting_intelligence_provider import validate_meeting_intelligence_runtime_configuration
from .meeting_media_provider import validate_meeting_media_runtime_configuration
from .product_surface_pep import (
    document_expected_decision_revision,
    install_product_surface_openapi_contract,
    install_product_surface_pep,
)
from .run_store import (
    RequestIdConflict,
    RunInProgress,
    RunStoreUnavailable,
    initialize_database,
)
from .stream_runtime import (
    shutdown_ask_stream_pool,
    stream_ask_response,
)
from .system_api import build_system_router
from .user_run_api import router as user_run_router
from .activity_api import router as activity_router
from .artifact_api import router as artifact_router
from .domain_retention_api import admin_router as domain_retention_admin_router
from .domain_retention_api import router as domain_retention_router
from .personal_memory_api import router as personal_memory_router
from .personal_routine_api import router as personal_routine_router
from .private_no_store import install_private_no_store
from .voice_api import router as voice_router
from .voice_provider import validate_voice_runtime_configuration
from .workspace_authorization import resolve_workspace_request_authorization
from .conversation_continuation import verify_conversation_agent
from .security import header_values, require_gateway_service, verified_ask_identity
from .runtime_policy import (
    RuntimeGovernanceNotConfigured,
    SourcePolicyBlocked,
    SourceScopeLimitExceeded,
    resolve_runtime_safety_controls,
)


SERVICE_NAME = os.getenv("APP_NAME", "DWP Agent Runtime")
SERVICE_VERSION = os.getenv("APP_VERSION", "0.2.0")


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_runtime_configuration()
    validate_voice_runtime_configuration()
    validate_meeting_intelligence_runtime_configuration()
    validate_meeting_media_runtime_configuration()
    initialize_database()
    seed_local_governance()
    seed_local_activity()
    validate_delivery_gate_runtime()
    question_launch_maintenance.start()
    governed_worker_maintenance.start()
    try:
        yield
    finally:
        governed_worker_maintenance.close()
        question_launch_maintenance.close()
        shutdown_ask_stream_pool()


app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION, lifespan=lifespan)
install_api_history(app)
install_meeting_intelligence_body_limit(app)
install_meeting_media_body_limit(app)
install_product_surface_pep(app)
install_private_no_store(app)
install_operational_gate_problem_handler(app)
app.include_router(
    build_system_router(service_name=SERVICE_NAME, service_version=SERVICE_VERSION)
)
app.include_router(operations_router)
app.include_router(governance_router)
app.include_router(governance_safety_router)
app.include_router(operational_gate_router)
app.include_router(action_router)
app.include_router(question_launch_router)
app.include_router(proposal_router)
app.include_router(user_run_router)
app.include_router(activity_router)
app.include_router(voice_router)
app.include_router(meeting_intelligence_router)
app.include_router(meeting_media_router)
app.include_router(personal_routine_router)
app.include_router(personal_memory_router)
app.include_router(artifact_router)
app.include_router(domain_retention_router)
app.include_router(domain_retention_admin_router)


def require_operational_delivery(
    *, tenant_id: str, user_id: str, capability: DeliveryCapability
) -> None:
    try:
        require_delivery_capability(
            tenant_id=tenant_id,
            capability=capability,
        )
    except (OperationalDeliveryConfigurationError, OperationalDeliveryNotReady) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"DWAI-ON {capability.value.lower()} is not approved for this environment.",
        ) from error


def get_ask_runtime() -> AskRuntime:
    return AskRuntime()


def require_ask_access(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> None:
    if "APP.ASK:VIEW" not in header_values(permissions):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON access is required.",
        )


def runtime_safety_controls(request: AskRequest, identity: AskIdentity) -> SafetyControls:
    try:
        return resolve_runtime_safety_controls(request, identity)
    except GovernanceStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error
    except RuntimeGovernanceNotConfigured as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error
    except SourcePolicyBlocked as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(error),
        )
    except SourceScopeLimitExceeded as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        )


@app.post(
    "/v1/ask",
    response_model=AskEnvelope,
    response_model_by_alias=True,
    tags=["ask"],
    dependencies=[Depends(require_gateway_service), Depends(require_ask_access), Depends(document_expected_decision_revision)],
)
def ask(
    request: AskRequest,
    http_request: Request,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    runtime: AskRuntime = Depends(get_ask_runtime),
) -> AskEnvelope:
    require_operational_delivery(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        capability=DeliveryCapability.ASK,
    )
    try:
        safety_controls = runtime_safety_controls(request, identity)
        return AskEnvelope(data=runtime.answer(
            request,
            identity=identity,
            safety_controls=safety_controls,
            workspace_authorization=resolve_workspace_request_authorization(http_request),
        ))
    except RegistryResolutionError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="An active Agent registry contract is required.",
        ) from error
    except RunInProgress as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The Ask request is already running.",
        ) from error
    except RequestIdConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The request ID was already used for another Ask query.",
        ) from error
    except RunStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The Agent run store is unavailable.",
        ) from error
    except ConversationNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.post(
    "/v1/ask/stream",
    tags=["ask"],
    dependencies=[Depends(require_gateway_service), Depends(require_ask_access), Depends(document_expected_decision_revision)],
)
def ask_stream(
    request: AskRequest,
    http_request: Request,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    runtime: AskRuntime = Depends(get_ask_runtime),
) -> StreamingResponse:
    require_operational_delivery(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        capability=DeliveryCapability.ASK,
    )
    safety_controls = runtime_safety_controls(request, identity)

    return stream_ask_response(
        request=request,
        identity=identity,
        runtime=runtime,
        safety_controls=safety_controls,
        encode_event=_sse,
        error_code=_stream_error_code,
        workspace_authorization=resolve_workspace_request_authorization(http_request),
    )


@app.get(
    "/v1/conversations",
    response_model=ConversationListEnvelope,
    response_model_by_alias=True,
    tags=["conversations"],
    dependencies=[Depends(require_gateway_service), Depends(require_ask_access)],
)
def list_conversations(
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    limit: int = 30,
) -> ConversationListEnvelope:
    return ConversationListEnvelope(
        data=get_conversation_store().list(
            tenant_id=tenant_id, user_id=user_id, limit=max(1, min(limit, 100))
        )
    )


@app.get(
    "/v1/conversations/{conversation_id}",
    response_model=ConversationEnvelope,
    response_model_by_alias=True,
    tags=["conversations"],
    dependencies=[Depends(require_gateway_service), Depends(require_ask_access)],
)
def get_conversation(
    conversation_id: UUID,
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    agentKey: str | None = None,
) -> ConversationEnvelope:
    try:
        detail = get_conversation_store().get(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        verify_conversation_agent(detail, agentKey)
        return ConversationEnvelope(data=detail)
    except ConversationNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.patch(
    "/v1/conversations/{conversation_id}",
    response_model=ConversationEnvelope,
    response_model_by_alias=True,
    tags=["conversations"],
    dependencies=[Depends(require_gateway_service), Depends(require_ask_access), Depends(document_expected_decision_revision)],
)
def rename_conversation(
    conversation_id: UUID,
    request: RenameConversationRequest,
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
) -> ConversationEnvelope:
    try:
        detail = get_conversation_store().rename(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            title=request.title,
        )
        return ConversationEnvelope(message="Conversation renamed.", data=detail)
    except ConversationNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.delete(
    "/v1/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["conversations"],
    dependencies=[Depends(require_gateway_service), Depends(require_ask_access), Depends(document_expected_decision_revision)],
)
def delete_conversation(
    conversation_id: UUID,
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
) -> Response:
    try:
        get_conversation_store().delete(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except ConversationNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ConversationRetentionLocked as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@app.put(
    "/v1/runs/{run_id}/feedback",
    response_model=FeedbackEnvelope,
    response_model_by_alias=True,
    tags=["feedback"],
    dependencies=[Depends(require_gateway_service), Depends(require_ask_access)],
)
def record_feedback(
    run_id: UUID,
    request: AnswerFeedbackRequest,
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
) -> FeedbackEnvelope:
    try:
        receipt = get_conversation_store().feedback(
            tenant_id=tenant_id, user_id=user_id, run_id=run_id, request=request
        )
        return FeedbackEnvelope(data=receipt)
    except ConversationNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.post(
    "/v1/plans/preview",
    response_model=PlanPreviewEnvelope,
    response_model_by_alias=True,
    tags=["plans"],
    dependencies=[Depends(require_gateway_service)],
)
def preview_plan(
    request: PlanPreviewRequest,
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1)],
    roles: Annotated[str | None, Header(alias="X-DWP-Roles")] = None,
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
    resource_roles: Annotated[
        str | None, Header(alias="X-DWP-Resource-Roles")
    ] = None,
    identity_plane: Annotated[
        str | None, Header(alias="X-DWP-Identity-Plane")
    ] = None,
) -> PlanPreviewEnvelope:
    authorities = set(header_values(permissions))
    verified_roles = set(header_values(roles))
    verified_resource_roles = set(header_values(resource_roles))
    verified_plane = (identity_plane or "").strip().upper()
    if request.admin_change is not None:
        definition = resolve_admin_command(
            request.admin_change.command_key,
            request.admin_change.target_type,
            request.admin_change.parameters,
        )
        try:
            require_admin_preflight(
                definition,
                request.admin_change.parameters,
                identity_plane=verified_plane,
                permissions=authorities,
                roles=verified_roles,
                resource_roles=verified_resource_roles,
            )
        except AdminPreflightDenied as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(error),
            ) from error
    elif verified_plane != "TENANT" or "APP.ASK:VIEW" not in authorities:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON access is required.",
        )
    require_operational_delivery(
        tenant_id=tenant_id,
        user_id=user_id,
        capability=DeliveryCapability.ACTION,
    )
    plan_roles = sorted(verified_roles)
    try:
        agent_registry = resolve_agent(
            request.agent_key,
            tenant_id=tenant_id,
            user_id=user_id,
            correlation_id=correlation_id,
        )
    except RegistryResolutionError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="An active Agent registry contract is required.",
        ) from error
    plan = build_reference_plan(
        request,
        tenant_id=tenant_id,
        user_id=user_id,
        roles=plan_roles,
        correlation_id=correlation_id,
        agent_registry=agent_registry,
        identity_plane=verified_plane,
        resource_roles=sorted(verified_resource_roles),
    )
    record_plan_preview(
        plan,
        tenant_id=tenant_id,
        user_id=user_id,
        role_count=len(plan_roles),
        roles=plan_roles,
    )
    return PlanPreviewEnvelope(data=plan)


def _sse(event: str, payload: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _stream_error_code(error: Exception) -> str:
    if isinstance(error, ConversationNotFound):
        return "CONVERSATION_NOT_FOUND"
    if isinstance(error, RegistryResolutionError):
        return "AGENT_REGISTRY_UNAVAILABLE"
    if isinstance(error, RunInProgress):
        return "RUN_IN_PROGRESS"
    if isinstance(error, RequestIdConflict):
        return "REQUEST_ID_CONFLICT"
    if isinstance(error, RunStoreUnavailable):
        return "AGENT_STORE_UNAVAILABLE"
    return "ASK_STREAM_FAILED"


install_product_surface_openapi_contract(app)
