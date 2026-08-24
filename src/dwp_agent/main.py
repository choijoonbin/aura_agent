from __future__ import annotations

import os
import json
from contextlib import asynccontextmanager
from queue import Queue
from threading import Thread
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.responses import StreamingResponse

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
from .policy import AskIdentity, SafetyControls
from .planner import build_reference_plan
from .registry import RegistryResolutionError, resolve_agent
from .readiness import validate_runtime_configuration
from .observability import install_api_history
from .operations_api import router as operations_router
from .governance_api import router as governance_router
from .operational_gate_api import (
    install_operational_gate_problem_handler,
    router as operational_gate_router,
)
from .governance_store import GovernanceStoreUnavailable
from .run_store import (
    RequestIdConflict,
    RunInProgress,
    RunStoreUnavailable,
    database_status,
    initialize_database,
)
from .security import header_values, require_gateway_service, verified_ask_identity
from .runtime_policy import (
    SourcePolicyBlocked,
    SourceScopeLimitExceeded,
    resolve_runtime_safety_controls,
)


SERVICE_NAME = os.getenv("APP_NAME", "DWP Agent Runtime")
SERVICE_VERSION = os.getenv("APP_VERSION", "0.2.0")

@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_runtime_configuration()
    initialize_database()
    yield


app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION, lifespan=lifespan)
install_api_history(app)
install_operational_gate_problem_handler(app)
app.include_router(operations_router)
app.include_router(governance_router)
app.include_router(operational_gate_router)
app.include_router(action_router)


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "docs": "/docs",
    }


@app.get("/health", tags=["system"])
def health() -> dict[str, str | dict[str, str]]:
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "components": {"database": database_status()},
    }


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
    dependencies=[Depends(require_gateway_service)],
)
def ask(
    request: AskRequest,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    runtime: AskRuntime = Depends(get_ask_runtime),
) -> AskEnvelope:
    try:
        safety_controls = runtime_safety_controls(request, identity)
        return AskEnvelope(data=runtime.answer(
            request, identity=identity, safety_controls=safety_controls))
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
    dependencies=[Depends(require_gateway_service)],
)
def ask_stream(
    request: AskRequest,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    runtime: AskRuntime = Depends(get_ask_runtime),
) -> StreamingResponse:
    safety_controls = runtime_safety_controls(request, identity)

    def stream():
        events: Queue[tuple[str, dict[str, object] | None]] = Queue()

        def worker() -> None:
            try:
                response = runtime.answer(
                    request,
                    identity=identity,
                    on_progress=lambda stage: events.put(("progress", {"stage": stage})),
                    safety_controls=safety_controls,
                )
                envelope = AskEnvelope(data=response)
                events.put(
                    (
                        "result",
                        envelope.model_dump(mode="json", by_alias=True),
                    )
                )
            except Exception as error:  # Stream headers are already committed; emit safe evidence.
                events.put(("error", {"code": _stream_error_code(error)}))
            finally:
                events.put(("done", None))

        Thread(target=worker, name="dwaion-ask-stream", daemon=True).start()
        while True:
            event, payload = events.get()
            if event == "done":
                break
            yield _sse(event, payload or {})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
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
) -> ConversationEnvelope:
    try:
        detail = get_conversation_store().get(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        return ConversationEnvelope(data=detail)
    except ConversationNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@app.patch(
    "/v1/conversations/{conversation_id}",
    response_model=ConversationEnvelope,
    response_model_by_alias=True,
    tags=["conversations"],
    dependencies=[Depends(require_gateway_service), Depends(require_ask_access)],
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
    dependencies=[Depends(require_gateway_service), Depends(require_ask_access)],
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
) -> PlanPreviewEnvelope:
    verified_roles = [] if roles is None else roles.split(",")
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
        roles=verified_roles,
        correlation_id=correlation_id,
        agent_registry=agent_registry,
    )
    record_plan_preview(
        plan,
        tenant_id=tenant_id,
        user_id=user_id,
        role_count=len({role.strip() for role in verified_roles if role.strip()}),
        roles=verified_roles,
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
