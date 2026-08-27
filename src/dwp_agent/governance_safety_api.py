from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from .governance_api import _context, _headers
from .governance_contracts import (
    BootstrapGovernancePoliciesRequest,
    SafetyPolicyEnvelope,
    UpdateSafetyPolicyRequest,
)
from .governance_store import (
    GovernancePolicyConflict,
    GovernancePolicyNotInitialized,
    GovernanceStoreUnavailable,
    get_governance_store,
)
from .security import require_gateway_service


router = APIRouter(
    prefix="/v1/admin",
    tags=["governance"],
    dependencies=[Depends(require_gateway_service)],
)


@router.get("/safety", response_model=SafetyPolicyEnvelope, response_model_by_alias=True)
def get_safety_policy(
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_SAFETY", "VIEW")
    try:
        return SafetyPolicyEnvelope(
            data=get_governance_store().safety_policy(
                tenant_id=tenant, actor_user_id=user))
    except GovernancePolicyNotInitialized as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/safety/bootstrap",
    response_model=SafetyPolicyEnvelope,
    response_model_by_alias=True,
)
def bootstrap_safety_policy(
    request: BootstrapGovernancePoliciesRequest,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_SAFETY", "MANAGE")
    try:
        return SafetyPolicyEnvelope(
            message="DWAI-ON safety policy initialized in a fail-closed state.",
            data=get_governance_store().bootstrap_safety_policy(
                tenant_id=tenant,
                actor_user_id=user,
                correlation_id=correlation,
                request=request,
            ),
        )
    except GovernancePolicyConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except GovernanceStoreUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.patch("/safety", response_model=SafetyPolicyEnvelope, response_model_by_alias=True)
def update_safety_policy(
    request: UpdateSafetyPolicyRequest,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_SAFETY", "UPDATE")
    try:
        policy = get_governance_store().update_safety_policy(
            tenant_id=tenant,
            actor_user_id=user,
            correlation_id=correlation,
            request=request,
        )
        return SafetyPolicyEnvelope(
            message="DWAI-ON safety policy updated.", data=policy)
    except GovernancePolicyConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except GovernancePolicyNotInitialized as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
