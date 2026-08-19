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
from .audit import record_plan_preview
from .contracts import AskEnvelope, AskRequest, PlanPreviewEnvelope, PlanPreviewRequest
from .contracts import (
    AnswerFeedbackRequest,
    ConversationEnvelope,
    ConversationListEnvelope,
    FeedbackEnvelope,
    RenameConversationRequest,
    WorkplaceActionListEnvelope,
    WorkplaceActionPreview,
    WorkplaceActionPreviewEnvelope,
    WorkplaceActionPreviewRequest,
)
from .conversation_store import ConversationNotFound, get_conversation_store
from .policy import AskIdentity
from .planner import build_reference_plan
from .registry import RegistryResolutionError, resolve_agent
from .observability import install_api_history
from .run_store import (
    RequestIdConflict,
    RunInProgress,
    RunStoreUnavailable,
    database_status,
    initialize_database,
)
from .security import require_gateway_service
from .workplace_actions import (
    WorkplaceActionForbidden,
    WorkplaceActionNotFound,
    available_workplace_actions,
    resolve_workplace_action,
)


SERVICE_NAME = os.getenv("APP_NAME", "DWP Agent Runtime")
SERVICE_VERSION = os.getenv("APP_VERSION", "0.2.0")

@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION, lifespan=lifespan)
install_api_history(app)


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
    if "APP.ASK:VIEW" not in _header_values(permissions):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON access is required.",
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
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1)],
    roles: Annotated[str | None, Header(alias="X-DWP-Roles")] = None,
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
    runtime: AskRuntime = Depends(get_ask_runtime),
) -> AskEnvelope:
    identity = _identity(user_id, tenant_id, correlation_id, roles, permissions)
    try:
        return AskEnvelope(data=runtime.answer(request, identity=identity))
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
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1)],
    roles: Annotated[str | None, Header(alias="X-DWP-Roles")] = None,
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
    runtime: AskRuntime = Depends(get_ask_runtime),
) -> StreamingResponse:
    identity = _identity(user_id, tenant_id, correlation_id, roles, permissions)

    def stream():
        events: Queue[tuple[str, dict[str, object] | None]] = Queue()

        def worker() -> None:
            try:
                response = runtime.answer(
                    request,
                    identity=identity,
                    on_progress=lambda stage: events.put(("progress", {"stage": stage})),
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


@app.get(
    "/v1/actions",
    response_model=WorkplaceActionListEnvelope,
    response_model_by_alias=True,
    tags=["actions"],
    dependencies=[Depends(require_gateway_service), Depends(require_ask_access)],
)
def list_actions(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> WorkplaceActionListEnvelope:
    return WorkplaceActionListEnvelope(
        data=available_workplace_actions(_header_values(permissions))
    )


@app.post(
    "/v1/actions/{action_key}/preview",
    response_model=WorkplaceActionPreviewEnvelope,
    response_model_by_alias=True,
    tags=["actions"],
    dependencies=[Depends(require_gateway_service), Depends(require_ask_access)],
)
def preview_action(
    action_key: str,
    request: WorkplaceActionPreviewRequest,
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1)],
    roles: Annotated[str | None, Header(alias="X-DWP-Roles")] = None,
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> WorkplaceActionPreviewEnvelope:
    try:
        action = resolve_workplace_action(action_key, _header_values(permissions))
    except WorkplaceActionNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except WorkplaceActionForbidden as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    try:
        registry = resolve_agent(
            "REFERENCE_PLANNER",
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
        PlanPreviewRequest(
            request_id=request.request_id,
            intent=f"Prepare governed handoff for {action.action_key}",
            action=action.action_key,
            target=action.target_route,
            source_references=request.source_references,
            agent_key="REFERENCE_PLANNER",
        ),
        tenant_id=tenant_id,
        user_id=user_id,
        roles=list(_header_values(roles)),
        correlation_id=correlation_id,
        agent_registry=registry,
    ).model_copy(update={"risk_tier": action.risk_tier})
    return WorkplaceActionPreviewEnvelope(
        data=WorkplaceActionPreview(action=action, plan=plan)
    )


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


def _header_values(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(
        sorted({item.strip().upper() for item in value.split(",") if item.strip()})
    )


def _identity(
    user_id: str,
    tenant_id: str,
    correlation_id: str,
    roles: str | None,
    permissions: str | None,
) -> AskIdentity:
    return AskIdentity(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=_header_values(roles),
        permissions=_header_values(permissions),
        correlation_id=correlation_id,
    )


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
