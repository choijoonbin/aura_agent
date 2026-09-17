from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from .ai_control_contracts import (
    AIControlOverviewEnvelope,
    BootstrapAIExecutionPolicyRequest,
    SetAIEmergencyDisableRequest,
    UpdateAIExecutionPolicyRequest,
)
from .ai_control_runtime import (
    AIControlConflict,
    AIControlPolicyNotConfigured,
    AIControlUnavailable,
)
from .ai_control_store import get_ai_control_store
from .ai_control_surface_guard import require_ai_control_product_surface
from .security import header_values, require_gateway_service


router = APIRouter(prefix="/v1/admin/ai-control", tags=["administration"])


def require_ai_control_view(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> tuple[str, ...]:
    verified = header_values(permissions)
    if not ({"ADMIN.DWAION_SAFETY:VIEW", "ADMIN.DWAION_SAFETY:MANAGE"} & set(verified)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON AI runtime control access is required.",
        )
    return verified


def require_ai_control_update(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> tuple[str, ...]:
    verified = header_values(permissions)
    if not ({"ADMIN.DWAION_SAFETY:UPDATE", "ADMIN.DWAION_SAFETY:MANAGE"} & set(verified)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON AI runtime policy update access is required.",
        )
    return verified


def require_ai_emergency_control(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> tuple[str, ...]:
    verified = header_values(permissions)
    if "ADMIN.DWAION_SAFETY:MANAGE" not in set(verified):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON emergency stop access is required.",
        )
    return verified


@router.get(
    "",
    response_model=AIControlOverviewEnvelope,
    response_model_by_alias=True,
    dependencies=[
        Depends(require_gateway_service),
        Depends(require_ai_control_product_surface),
        Depends(require_ai_control_view),
    ],
)
def get_ai_control_overview(
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
) -> AIControlOverviewEnvelope:
    return _overview(tenant_id)


@router.post(
    "/bootstrap",
    response_model=AIControlOverviewEnvelope,
    response_model_by_alias=True,
    dependencies=[
        Depends(require_gateway_service),
        Depends(require_ai_control_product_surface),
        Depends(require_ai_control_update),
    ],
)
def bootstrap_ai_control(
    request: BootstrapAIExecutionPolicyRequest,
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1)],
) -> AIControlOverviewEnvelope:
    try:
        store = get_ai_control_store()
        store.bootstrap(
            tenant_id=tenant_id,
            actor_user_id=user_id,
            correlation_id=correlation_id,
            request=request,
        )
        return AIControlOverviewEnvelope(
            message="DWAI-ON AI runtime policy initialized.",
            data=store.overview(tenant_id=tenant_id, now=datetime.now(timezone.utc)),
        )
    except (AIControlConflict, AIControlPolicyNotConfigured) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except AIControlUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error


@router.put(
    "/policy",
    response_model=AIControlOverviewEnvelope,
    response_model_by_alias=True,
    dependencies=[
        Depends(require_gateway_service),
        Depends(require_ai_control_product_surface),
        Depends(require_ai_control_update),
    ],
)
def update_ai_control_policy(
    request: UpdateAIExecutionPolicyRequest,
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1)],
) -> AIControlOverviewEnvelope:
    try:
        store = get_ai_control_store()
        store.update(
            tenant_id=tenant_id,
            actor_user_id=user_id,
            correlation_id=correlation_id,
            request=request,
        )
        return AIControlOverviewEnvelope(
            message="DWAI-ON AI runtime policy updated.",
            data=store.overview(tenant_id=tenant_id, now=datetime.now(timezone.utc)),
        )
    except (AIControlConflict, AIControlPolicyNotConfigured) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except AIControlUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error


@router.post(
    "/emergency",
    response_model=AIControlOverviewEnvelope,
    response_model_by_alias=True,
    dependencies=[
        Depends(require_gateway_service),
        Depends(require_ai_control_product_surface),
        Depends(require_ai_emergency_control),
    ],
)
def set_ai_emergency_control(
    request: SetAIEmergencyDisableRequest,
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1)],
) -> AIControlOverviewEnvelope:
    try:
        store = get_ai_control_store()
        store.set_emergency_disabled(
            tenant_id=tenant_id,
            actor_user_id=user_id,
            correlation_id=correlation_id,
            request=request,
        )
        message = (
            "DWAI-ON Ask runtime emergency stop enabled."
            if request.disabled
            else "DWAI-ON Ask runtime emergency stop released."
        )
        return AIControlOverviewEnvelope(
            message=message,
            data=store.overview(tenant_id=tenant_id, now=datetime.now(timezone.utc)),
        )
    except (AIControlConflict, AIControlPolicyNotConfigured) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except AIControlUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error


def _overview(tenant_id: str) -> AIControlOverviewEnvelope:
    try:
        return AIControlOverviewEnvelope(
            data=get_ai_control_store().overview(
                tenant_id=tenant_id, now=datetime.now(timezone.utc)
            )
        )
    except AIControlUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error
