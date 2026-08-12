from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status

from ask_runtime import AskRuntime
from audit import record_plan_preview
from contracts import AskEnvelope, AskRequest, PlanPreviewEnvelope, PlanPreviewRequest
from policy import AskIdentity
from planner import build_reference_plan
from registry import RegistryResolutionError, resolve_agent
from observability import install_api_history
from run_store import (
    RequestIdConflict,
    RunInProgress,
    RunStoreUnavailable,
    database_status,
    initialize_database,
)
from security import require_gateway_service


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
    identity = AskIdentity(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=_header_values(roles),
        permissions=_header_values(permissions),
        correlation_id=correlation_id,
    )
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
