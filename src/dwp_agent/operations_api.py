from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from .operations_contracts import (
    BootstrapRetentionPolicyRequest,
    DwaionOperationsOverviewEnvelope,
    RetentionPolicyEnvelope,
    UpdateRetentionPolicyRequest,
)
from .operations_store import (
    OperationsStoreUnavailable,
    RetentionPolicyConflict,
    RetentionPolicyNotConfigured,
    get_operations_store,
)
from .security import header_values, require_gateway_service


router = APIRouter(prefix="/v1/admin", tags=["administration"])


def require_dwaion_admin_view(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> tuple[str, ...]:
    verified = header_values(permissions)
    if not ({
        "ADMIN.DWAION_OPERATIONS:VIEW",
        "ADMIN.DWAION_OPERATIONS:MANAGE",
        "ADMIN.DWAION:MANAGE",
    } & set(verified)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON operations access is required.",
        )
    return verified


def require_dwaion_admin_update(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> tuple[str, ...]:
    verified = header_values(permissions)
    if not ({
        "ADMIN.DWAION_RETENTION:UPDATE",
        "ADMIN.DWAION_RETENTION:MANAGE",
        "ADMIN.DWAION:MANAGE",
    } & set(verified)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON policy update access is required.",
        )
    return verified


def require_dwaion_retention_view(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> tuple[str, ...]:
    verified = header_values(permissions)
    if not ({
        "ADMIN.DWAION_RETENTION:VIEW",
        "ADMIN.DWAION_RETENTION:MANAGE",
        "ADMIN.DWAION:MANAGE",
    } & set(verified)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON retention access is required.",
        )
    return verified


def require_dwaion_retention_manage(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> tuple[str, ...]:
    verified = header_values(permissions)
    if not ({"ADMIN.DWAION_RETENTION:MANAGE", "ADMIN.DWAION:MANAGE"} & set(verified)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON retention management access is required.",
        )
    return verified


@router.get(
    "/overview",
    response_model=DwaionOperationsOverviewEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_gateway_service), Depends(require_dwaion_admin_view)],
)
def operations_overview(
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    period_days: int = 30,
) -> DwaionOperationsOverviewEnvelope:
    try:
        return DwaionOperationsOverviewEnvelope(
            data=get_operations_store().overview(
                tenant_id=tenant_id, period_days=max(1, min(period_days, 90))
            )
        )
    except RetentionPolicyNotConfigured as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except OperationsStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error


@router.get(
    "/retention",
    response_model=RetentionPolicyEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_gateway_service), Depends(require_dwaion_retention_view)],
)
def get_retention_policy(
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
) -> RetentionPolicyEnvelope:
    try:
        return RetentionPolicyEnvelope(
            data=get_operations_store().retention_policy(tenant_id=tenant_id)
        )
    except RetentionPolicyNotConfigured as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except OperationsStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error


@router.post(
    "/retention/bootstrap",
    response_model=RetentionPolicyEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_gateway_service), Depends(require_dwaion_retention_manage)],
)
def bootstrap_retention_policy(
    request: BootstrapRetentionPolicyRequest,
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1)],
) -> RetentionPolicyEnvelope:
    try:
        policy = get_operations_store().bootstrap_retention_policy(
            tenant_id=tenant_id,
            actor_user_id=user_id,
            correlation_id=correlation_id,
            request=request,
        )
        return RetentionPolicyEnvelope(
            message="DWAI-ON retention policy initialized.", data=policy
        )
    except RetentionPolicyConflict as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except OperationsStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error


@router.patch(
    "/retention",
    response_model=RetentionPolicyEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_gateway_service)],
)
def update_retention_policy(
    request: UpdateRetentionPolicyRequest,
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1)],
    permissions: Annotated[tuple[str, ...], Depends(require_dwaion_admin_update)],
) -> RetentionPolicyEnvelope:
    if request.legal_hold is not None and not ({
        "ADMIN.DWAION_RETENTION:MANAGE",
        "ADMIN.DWAION:MANAGE",
    } & set(permissions)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON legal hold requires manage access.",
        )
    try:
        policy = get_operations_store().update_retention_policy(
            tenant_id=tenant_id,
            actor_user_id=user_id,
            correlation_id=correlation_id,
            request=request,
        )
        return RetentionPolicyEnvelope(
            message="DWAI-ON retention policy updated.", data=policy
        )
    except (RetentionPolicyConflict, RetentionPolicyNotConfigured) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except OperationsStoreUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error
