from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from .contracts import CitationSourceType
from .evaluation_runner import run_evaluation
from .evaluation_store import (
    EvaluationSetNotFound,
    EvaluationSetNotRunnable,
    get_evaluation_store,
)
from .governance_contracts import (
    ActionPolicyEnvelope,
    ActionPolicyListEnvelope,
    CreateEvaluationCaseRequest,
    CreateEvaluationSetRequest,
    DataSourcePolicyEnvelope,
    DataSourcePolicyListEnvelope,
    EvaluationRunEnvelope,
    EvaluationSetEnvelope,
    EvaluationSetListEnvelope,
    GovernanceAuditEnvelope,
    SafetyPolicyEnvelope,
    UpdateActionPolicyRequest,
    UpdateDataSourcePolicyRequest,
    UpdateEvaluationLifecycleRequest,
    UpdateSafetyPolicyRequest,
)
from .governance_store import (
    GovernancePolicyConflict,
    GovernanceStoreUnavailable,
    get_governance_store,
)
from .policy import AskIdentity
from .security import header_values, require_gateway_service, verified_ask_identity


router = APIRouter(
    prefix="/v1/admin",
    tags=["governance"],
    dependencies=[Depends(require_gateway_service)],
)


def _context(
    tenant_id: str,
    user_id: str,
    correlation_id: str,
    permissions: str | None,
    resource: str,
    *allowed: str,
) -> tuple[str, str, str]:
    authorities = set(header_values(permissions))
    if f"{resource}:MANAGE" not in authorities and not any(
        f"{resource}:{permission}" in authorities for permission in allowed
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{resource} permission is required.",
        )
    return tenant_id, user_id, correlation_id


def _headers(
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1)],
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> tuple[str, str, str, str | None]:
    return tenant_id, user_id, correlation_id, permissions


@router.get("/sources", response_model=DataSourcePolicyListEnvelope, response_model_by_alias=True)
def list_source_policies(
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_SOURCES", "VIEW")
    try:
        return DataSourcePolicyListEnvelope(
            data=get_governance_store().source_policies(
                tenant_id=tenant, actor_user_id=user))
    except GovernanceStoreUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.patch(
    "/sources/{source_key}",
    response_model=DataSourcePolicyEnvelope,
    response_model_by_alias=True,
)
def update_source_policy(
    source_key: CitationSourceType,
    request: UpdateDataSourcePolicyRequest,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_SOURCES", "UPDATE")
    try:
        policy = get_governance_store().update_source_policy(
            tenant_id=tenant,
            actor_user_id=user,
            correlation_id=correlation,
            source_key=source_key,
            request=request,
        )
        return DataSourcePolicyEnvelope(
            message="DWAI-ON source policy updated.", data=policy)
    except GovernancePolicyConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except GovernanceStoreUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/actions", response_model=ActionPolicyListEnvelope, response_model_by_alias=True)
def list_action_policies(
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_ACTIONS", "VIEW")
    return ActionPolicyListEnvelope(
        data=get_governance_store().action_policies(
            tenant_id=tenant, actor_user_id=user))


@router.patch(
    "/actions/{action_key}",
    response_model=ActionPolicyEnvelope,
    response_model_by_alias=True,
)
def update_action_policy(
    action_key: str,
    request: UpdateActionPolicyRequest,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_ACTIONS", "UPDATE")
    try:
        policy = get_governance_store().update_action_policy(
            tenant_id=tenant,
            actor_user_id=user,
            correlation_id=correlation,
            action_key=action_key,
            request=request,
        )
        return ActionPolicyEnvelope(
            message="DWAI-ON action policy updated.", data=policy)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="The action is not registered.") from error
    except GovernancePolicyConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/safety", response_model=SafetyPolicyEnvelope, response_model_by_alias=True)
def get_safety_policy(
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_SAFETY", "VIEW")
    return SafetyPolicyEnvelope(
        data=get_governance_store().safety_policy(
            tenant_id=tenant, actor_user_id=user))


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


@router.get(
    "/evaluations",
    response_model=EvaluationSetListEnvelope,
    response_model_by_alias=True,
)
def list_evaluation_sets(
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_EVALUATION", "VIEW")
    return EvaluationSetListEnvelope(
        data=get_evaluation_store().list_sets(tenant_id=tenant))


@router.get(
    "/evaluations/{evaluation_set_id}",
    response_model=EvaluationSetEnvelope,
    response_model_by_alias=True,
)
def get_evaluation_set(
    evaluation_set_id: UUID,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_EVALUATION", "VIEW")
    try:
        return EvaluationSetEnvelope(
            data=get_evaluation_store().detail(
                tenant_id=tenant, evaluation_set_id=evaluation_set_id))
    except EvaluationSetNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/evaluations",
    response_model=EvaluationSetEnvelope,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
def create_evaluation_set(
    request: CreateEvaluationSetRequest,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_EVALUATION", "CREATE")
    return EvaluationSetEnvelope(
        message="DWAI-ON evaluation set created.",
        data=get_evaluation_store().create_set(
            tenant_id=tenant,
            actor_user_id=user,
            correlation_id=correlation,
            request=request,
        ),
    )


@router.post(
    "/evaluations/{evaluation_set_id}/cases",
    response_model=EvaluationSetEnvelope,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
def add_evaluation_case(
    evaluation_set_id: UUID,
    request: CreateEvaluationCaseRequest,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_EVALUATION", "UPDATE")
    try:
        return EvaluationSetEnvelope(
            message="DWAI-ON evaluation case created.",
            data=get_evaluation_store().add_case(
                tenant_id=tenant,
                actor_user_id=user,
                correlation_id=correlation,
                evaluation_set_id=evaluation_set_id,
                request=request,
            ),
        )
    except EvaluationSetNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except EvaluationSetNotRunnable as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.patch(
    "/evaluations/{evaluation_set_id}/lifecycle",
    response_model=EvaluationSetEnvelope,
    response_model_by_alias=True,
)
def transition_evaluation_set(
    evaluation_set_id: UUID,
    request: UpdateEvaluationLifecycleRequest,
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_EVALUATION", "MANAGE")
    try:
        return EvaluationSetEnvelope(
            message="DWAI-ON evaluation lifecycle updated.",
            data=get_evaluation_store().transition(
                tenant_id=tenant,
                actor_user_id=user,
                correlation_id=correlation,
                evaluation_set_id=evaluation_set_id,
                request=request,
            ),
        )
    except EvaluationSetNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (EvaluationSetNotRunnable, GovernancePolicyConflict) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/evaluations/{evaluation_set_id}/runs",
    response_model=EvaluationRunEnvelope,
    response_model_by_alias=True,
)
def execute_evaluation(
    evaluation_set_id: UUID,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
):
    _context(
        identity.tenant_id,
        identity.user_id,
        identity.correlation_id,
        ",".join(identity.permissions),
        "ADMIN.DWAION_EVALUATION",
        "EXECUTE",
    )
    try:
        return EvaluationRunEnvelope(
            data=run_evaluation(
                store=get_evaluation_store(),
                tenant_id=identity.tenant_id,
                actor_user_id=identity.user_id,
                correlation_id=identity.correlation_id,
                evaluation_set_id=evaluation_set_id,
                identity=identity,
            )
        )
    except EvaluationSetNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except EvaluationSetNotRunnable as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/audit", response_model=GovernanceAuditEnvelope, response_model_by_alias=True)
def list_audit_events(
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
    category: str | None = None,
    query: str | None = None,
    page: int = 0,
    size: int = 50,
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_AUDIT", "VIEW")
    return GovernanceAuditEnvelope(
        data=get_governance_store().audit_events(
            tenant_id=tenant, category=category, query=query, page=page, size=size))


@router.get("/audit/export")
def export_audit_events(
    headers: Annotated[tuple[str, str, str, str | None], Depends(_headers)],
    category: str | None = None,
    query: str | None = None,
):
    tenant, user, correlation, permissions = headers
    _context(tenant, user, correlation, permissions, "ADMIN.DWAION_AUDIT", "EXPORT")
    content, truncated = get_governance_store().audit_csv(
        tenant_id=tenant, category=category, query=query)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=dwaion-governance-audit.csv",
            "X-DWP-Export-Limit": "10000",
            "X-DWP-Export-Truncated": str(truncated).lower(),
        },
    )
