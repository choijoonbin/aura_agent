from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header

from audit import record_plan_preview
from contracts import PlanPreviewEnvelope, PlanPreviewRequest
from planner import build_reference_plan
from security import require_gateway_service


SERVICE_NAME = os.getenv("APP_NAME", "DWP Agent Runtime")
SERVICE_VERSION = os.getenv("APP_VERSION", "0.2.0")

app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION)


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "docs": "/docs",
    }


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
    }


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
    plan = build_reference_plan(
        request,
        tenant_id=tenant_id,
        user_id=user_id,
        roles=verified_roles,
        correlation_id=correlation_id,
    )
    record_plan_preview(
        plan,
        tenant_id=tenant_id,
        user_id=user_id,
        role_count=len({role.strip() for role in verified_roles if role.strip()}),
    )
    return PlanPreviewEnvelope(data=plan)
