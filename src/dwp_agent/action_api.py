from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from .audit import record_plan_preview
from .contracts import (
    PlanPreviewRequest,
    WorkplaceActionListEnvelope,
    WorkplaceActionPreview,
    WorkplaceActionPreviewEnvelope,
    WorkplaceActionPreviewRequest,
)
from .governance_contracts import ActionExecutionPolicy
from .governance_store import GovernanceStoreUnavailable, get_governance_store
from .planner import build_reference_plan
from .registry import RegistryResolutionError, resolve_agent
from .security import header_values, require_gateway_service
from .workplace_actions import (
    WorkplaceActionForbidden,
    WorkplaceActionInputInvalid,
    WorkplaceActionNotFound,
    available_workplace_actions,
    review_workplace_action_inputs,
    resolve_workplace_action,
)


router = APIRouter(
    prefix="/v1/actions",
    tags=["actions"],
    dependencies=[Depends(require_gateway_service)],
)


def require_ask_access(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> None:
    if "APP.ASK:VIEW" not in header_values(permissions):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON access is required.",
        )


@router.get(
    "",
    response_model=WorkplaceActionListEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_ask_access)],
)
def list_actions(
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> WorkplaceActionListEnvelope:
    try:
        policies = {
            policy.action_key: policy
            for policy in get_governance_store().action_policies(
                tenant_id=tenant_id, actor_user_id=user_id)
        }
    except GovernanceStoreUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    actions = []
    for action in available_workplace_actions(header_values(permissions)):
        policy = policies.get(action.action_key)
        if (
            not policy
            or not policy.enabled
            or policy.execution_policy == ActionExecutionPolicy.BLOCKED
        ):
            continue
        actions.append(action.model_copy(
            update={"confirmation_required": policy.confirmation_required}))
    return WorkplaceActionListEnvelope(data=actions)


@router.post(
    "/{action_key}/preview",
    response_model=WorkplaceActionPreviewEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_ask_access)],
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
        action_policy = next(
            (
                policy
                for policy in get_governance_store().action_policies(
                    tenant_id=tenant_id, actor_user_id=user_id)
                if policy.action_key == action_key.strip().upper()
            ),
            None,
        )
    except GovernanceStoreUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if (
        action_policy is None
        or not action_policy.enabled
        or action_policy.execution_policy == ActionExecutionPolicy.BLOCKED
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The workplace action is disabled by DWAI-ON governance.",
        )
    try:
        action = resolve_workplace_action(action_key, header_values(permissions))
        action = action.model_copy(
            update={"confirmation_required": action_policy.confirmation_required})
    except WorkplaceActionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkplaceActionForbidden as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    try:
        reviewed_inputs = review_workplace_action_inputs(action, request.inputs)
    except WorkplaceActionInputInvalid as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        registry = resolve_agent(
            "REFERENCE_PLANNER",
            tenant_id=tenant_id,
            user_id=user_id,
            correlation_id=correlation_id,
        )
    except RegistryResolutionError as error:
        raise HTTPException(
            status_code=503,
            detail="An active Agent registry contract is required.",
        ) from error
    plan = build_reference_plan(
        PlanPreviewRequest(
            request_id=request.request_id,
            intent=f"Prepare governed handoff for {action.action_key}",
            action=action.action_key,
            target=action.target_route,
            source_references=request.source_references,
            inputs=reviewed_inputs,
            agent_key="REFERENCE_PLANNER",
        ),
        tenant_id=tenant_id,
        user_id=user_id,
        roles=list(header_values(roles)),
        correlation_id=correlation_id,
        agent_registry=registry,
    ).model_copy(update={"risk_tier": action.risk_tier})
    record_plan_preview(
        plan,
        tenant_id=tenant_id,
        user_id=user_id,
        role_count=len(set(header_values(roles))),
        roles=list(header_values(roles)),
    )
    return WorkplaceActionPreviewEnvelope(
        data=WorkplaceActionPreview(
            action=action, reviewed_inputs=reviewed_inputs, plan=plan))
