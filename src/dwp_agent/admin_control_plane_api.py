from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from hmac import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from .admin_control_plane_contracts import (
    CreateGovernedCommandRequest,
    GovernedCommandDecisionRequest,
    GovernedCommandEnvelope,
    GovernedCommandKind,
    GovernedCommandObservation,
    GovernedCommandRestartRequest,
    GovernedCommandsEnvelope,
    GovernedCommandsSnapshot,
    GovernedCommandState,
    GovernedCommandTransitionRequest,
    SnapshotEnvelope,
)
from .admin_control_plane_errors import (
    AdminControlPlaneConflict,
    AdminControlPlaneDenied,
    AdminControlPlaneNotFound,
    AdminControlPlaneUnavailable,
)
from .admin_control_plane_snapshots import get_admin_control_plane_snapshots
from .admin_control_plane_store import get_admin_control_plane_store
from .admin_control_plane_worker import get_admin_control_plane_worker_store
from .governed_domain_core import GovernedDomainForbidden, tenant_number
from .policy import AskIdentity
from .security import require_gateway_service, verified_ask_identity


@dataclass(frozen=True)
class AdminControlIdentity:
    tenant_id: int
    user_id: str
    correlation_id: str
    permissions: frozenset[str]

    def require_view(self) -> None:
        if not self.permissions.intersection({
            "ADMIN.DWAION_OPERATIONS:VIEW", "ADMIN.DWAION_OPERATIONS:MANAGE",
            "ADMIN.DWAION_SAFETY:VIEW", "ADMIN.DWAION_SAFETY:MANAGE",
        }):
            _deny("DWAI-ON control-plane view access is required.")

    def require_manage(self) -> None:
        if "ADMIN.DWAION_OPERATIONS:MANAGE" not in self.permissions:
            _deny("DWAI-ON control-plane management access is required.")


def require_admin_control_identity(
    request: Request,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
) -> AdminControlIdentity:
    if request.headers.get("X-DWP-Identity-Plane", "TENANT") != "TENANT":
        _deny("Tenant administration identity is required.")
    if request.headers.get("X-DWP-Access-Mode", "NORMAL") != "NORMAL":
        _deny("Normal tenant access mode is required.")
    try:
        tenant_id = tenant_number(identity.tenant_id)
    except GovernedDomainForbidden:
        _deny("Canonical tenant identity is required.")
    return AdminControlIdentity(
        tenant_id=tenant_id, user_id=identity.user_id,
        correlation_id=identity.correlation_id,
        permissions=frozenset(value.strip().upper() for value in identity.permissions),
    )


router = APIRouter(
    prefix="/v1/admin/control-plane", tags=["administration"],
    dependencies=[Depends(require_gateway_service)],
)


@router.get("/models-routing", response_model=SnapshotEnvelope, response_model_by_alias=True)
def models_routing(identity: Annotated[AdminControlIdentity, Depends(require_admin_control_identity)]) -> SnapshotEnvelope:
    identity.require_view()
    return _snapshot(_call(get_admin_control_plane_snapshots().models_routing,
                           tenant_id=identity.tenant_id))


@router.get("/connectors", response_model=SnapshotEnvelope, response_model_by_alias=True)
def connectors(identity: Annotated[AdminControlIdentity, Depends(require_admin_control_identity)]) -> SnapshotEnvelope:
    identity.require_view()
    return _snapshot(_call(get_admin_control_plane_snapshots().connectors,
                           tenant_id=identity.tenant_id))


@router.get("/evaluation-safety", response_model=SnapshotEnvelope, response_model_by_alias=True)
def evaluation_safety(identity: Annotated[AdminControlIdentity, Depends(require_admin_control_identity)]) -> SnapshotEnvelope:
    identity.require_view()
    return _snapshot(_call(get_admin_control_plane_snapshots().evaluation_safety,
                           tenant_id=identity.tenant_id))


@router.get("/incidents", response_model=SnapshotEnvelope, response_model_by_alias=True)
def incidents(identity: Annotated[AdminControlIdentity, Depends(require_admin_control_identity)]) -> SnapshotEnvelope:
    identity.require_view()
    return _snapshot(_call(get_admin_control_plane_snapshots().incidents,
                           tenant_id=identity.tenant_id))


@router.get("/outcomes", response_model=SnapshotEnvelope, response_model_by_alias=True)
def outcomes(
    identity: Annotated[AdminControlIdentity, Depends(require_admin_control_identity)],
    period_days: Annotated[int, Query(ge=1, le=90)] = 30,
    organization: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
    work_type: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
) -> SnapshotEnvelope:
    identity.require_view()
    return _snapshot(_call(
        get_admin_control_plane_snapshots().outcomes,
        tenant_id=identity.tenant_id, period_days=period_days,
        organization=organization, work_type=work_type,
    ))


@router.get("/commands", response_model=GovernedCommandsEnvelope, response_model_by_alias=True)
def list_commands(
    identity: Annotated[AdminControlIdentity, Depends(require_admin_control_identity)],
    state_filter: Annotated[GovernedCommandState | None, Query(alias="state")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> GovernedCommandsEnvelope:
    identity.require_view()
    commands = _call(get_admin_control_plane_store().list,
                     tenant_id=identity.tenant_id, actor_user_id=identity.user_id,
                     state=state_filter, limit=limit)
    return GovernedCommandsEnvelope(data=GovernedCommandsSnapshot(
        generated_at=datetime.now(UTC), commands=commands,
    ))


@router.post("/commands", response_model=GovernedCommandEnvelope, response_model_by_alias=True,
             status_code=status.HTTP_201_CREATED)
def create_command(
    request: CreateGovernedCommandRequest,
    identity: Annotated[AdminControlIdentity, Depends(require_admin_control_identity)],
    auth_session_id: Annotated[str, Header(alias="X-DWP-Auth-Session-ID", min_length=1, max_length=160)],
    step_up_challenge: Annotated[
        str | None, Header(alias="X-DWP-Step-Up-Challenge", min_length=16, max_length=16_384)
    ] = None,
) -> GovernedCommandEnvelope:
    identity.require_manage()
    if request.kind == GovernedCommandKind.EMERGENCY_RECOVERY and not step_up_challenge:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="Command-bound independent step-up authentication is required.",
        )
    command = _call(
        get_admin_control_plane_store().create,
        tenant_id=identity.tenant_id, actor_user_id=identity.user_id,
        correlation_id=identity.correlation_id, auth_session_id=auth_session_id,
        request=request,
    )
    return GovernedCommandEnvelope(message="DWAI-ON governed command recorded.", data=command)


@router.get("/commands/{command_id}", response_model=GovernedCommandEnvelope, response_model_by_alias=True)
def get_command(
    command_id: UUID,
    identity: Annotated[AdminControlIdentity, Depends(require_admin_control_identity)],
) -> GovernedCommandEnvelope:
    identity.require_view()
    return GovernedCommandEnvelope(data=_call(
        get_admin_control_plane_store().get, tenant_id=identity.tenant_id,
        actor_user_id=identity.user_id, command_id=command_id,
    ))


@router.post("/commands/{command_id}/decision", response_model=GovernedCommandEnvelope, response_model_by_alias=True)
def decide_command(command_id: UUID, request: GovernedCommandDecisionRequest,
                   identity: Annotated[AdminControlIdentity, Depends(require_admin_control_identity)]) -> GovernedCommandEnvelope:
    identity.require_manage()
    return GovernedCommandEnvelope(message="Governed command decision recorded.", data=_call(
        get_admin_control_plane_store().decide, tenant_id=identity.tenant_id,
        actor_user_id=identity.user_id, correlation_id=identity.correlation_id,
        command_id=command_id, request=request,
    ))


@router.post("/commands/{command_id}/cancel", response_model=GovernedCommandEnvelope, response_model_by_alias=True)
def cancel_command(command_id: UUID, request: GovernedCommandTransitionRequest,
                   identity: Annotated[AdminControlIdentity, Depends(require_admin_control_identity)]) -> GovernedCommandEnvelope:
    identity.require_manage()
    return _transition("cancel", command_id, request, identity)


@router.post("/commands/{command_id}/retry", response_model=GovernedCommandEnvelope, response_model_by_alias=True)
def retry_command(command_id: UUID, request: GovernedCommandRestartRequest,
                  identity: Annotated[AdminControlIdentity, Depends(require_admin_control_identity)]) -> GovernedCommandEnvelope:
    identity.require_manage()
    return _transition("retry", command_id, request, identity)


@router.post("/commands/{command_id}/rollback", response_model=GovernedCommandEnvelope, response_model_by_alias=True)
def rollback_command(command_id: UUID, request: GovernedCommandRestartRequest,
                     identity: Annotated[AdminControlIdentity, Depends(require_admin_control_identity)]) -> GovernedCommandEnvelope:
    identity.require_manage()
    return _transition("rollback", command_id, request, identity)


def require_admin_control_worker(
    token: Annotated[str | None, Header(alias="X-DWP-Admin-Control-Worker-Token")] = None,
) -> None:
    expected = os.getenv("DWP_ADMIN_CONTROL_WORKER_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Admin control worker identity is not configured.")
    if token is None or not compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid admin control worker identity.")


internal_router = APIRouter(
    prefix="/internal/v1/admin/control-plane", tags=["internal-administration"],
    dependencies=[Depends(require_admin_control_worker)],
)


@internal_router.post("/commands/{command_id}/observations", response_model=GovernedCommandEnvelope,
                      response_model_by_alias=True, include_in_schema=False)
def observe_command(
    command_id: UUID, request: GovernedCommandObservation,
    tenant_id: Annotated[int, Header(alias="X-DWP-Tenant-ID", ge=1)],
    worker_id: Annotated[str, Header(alias="X-DWP-Worker-ID", min_length=1, max_length=160)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1, max_length=160)],
) -> GovernedCommandEnvelope:
    return GovernedCommandEnvelope(message="Trusted worker observation recorded.", data=_call(
        get_admin_control_plane_worker_store().observe,
        tenant_id=tenant_id, worker_id=worker_id, correlation_id=correlation_id,
        governed_command_id=command_id, request=request,
    ))


def _transition(operation: str, command_id: UUID, request: object,
                identity: AdminControlIdentity) -> GovernedCommandEnvelope:
    method = getattr(get_admin_control_plane_store(), operation)
    data = _call(method, tenant_id=identity.tenant_id, actor_user_id=identity.user_id,
                 correlation_id=identity.correlation_id, command_id=command_id,
                 request=request)
    return GovernedCommandEnvelope(message=f"Governed command {operation} recorded.", data=data)


def _snapshot(data: object) -> SnapshotEnvelope:
    try:
        return SnapshotEnvelope(data=data)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=503, detail="Authoritative control-plane data is invalid.") from error


def _call(function: object, **kwargs: object) -> object:
    try:
        return function(**kwargs)  # type: ignore[operator]
    except AdminControlPlaneNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except AdminControlPlaneConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except AdminControlPlaneDenied as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except AdminControlPlaneUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _deny(detail: str) -> None:
    raise HTTPException(status_code=403, detail=detail)
