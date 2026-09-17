from __future__ import annotations

from typing import Annotated, Callable, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from .artifact_collaboration_contracts import (
    CreateTeamArtifactAccessRequest,
    CreateTeamArtifactShareRequest,
    CreateTeamArtifactWorkspaceRequest,
    ResolveTeamArtifactConflictRequest,
    RevokeTeamArtifactShareRequest,
    RunTeamArtifactPreflightRequest,
    SubmitTeamArtifactEditRequest,
    TeamArtifactAccessRequestEnvelope,
    TeamArtifactCapabilitiesEnvelope,
    TeamArtifactEditEnvelope,
    TeamArtifactPreflightEnvelope,
    TeamArtifactShareEnvelope,
    TeamArtifactWorkspaceEnvelope,
    UpdateTeamArtifactMembersRequest,
)
from .artifact_collaboration_capabilities import (
    artifact_collaboration_runtime_capabilities,
)
from .artifact_collaboration_store import get_artifact_collaboration_store
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
)
from .personal_domain_security import (
    PersonalDomainIdentity,
    personal_domain_dependencies,
    require_personal_domain_identity,
)


router = APIRouter(
    prefix="/v1/artifact-collaboration",
    tags=["governed-artifact-collaboration"],
    dependencies=personal_domain_dependencies,
)

T = TypeVar("T")


@router.get("/capabilities", response_model=TeamArtifactCapabilitiesEnvelope)
def get_artifact_collaboration_capabilities(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> TeamArtifactCapabilitiesEnvelope:
    _access(identity, "VIEW")
    _no_store(response)
    return TeamArtifactCapabilitiesEnvelope(
        data=artifact_collaboration_runtime_capabilities()
    )


@router.post(
    "/{artifact_id}/preflights", response_model=TeamArtifactPreflightEnvelope
)
def run_team_artifact_preflight(
    artifact_id: UUID,
    request: RunTeamArtifactPreflightRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> TeamArtifactPreflightEnvelope:
    _access(identity, "UPDATE")
    _no_store(response)
    return _run(
        lambda: TeamArtifactPreflightEnvelope(
            data=get_artifact_collaboration_store().preflight(
                identity, artifact_id, request
            )
        )
    )


@router.post(
    "/{artifact_id}/access-requests",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TeamArtifactAccessRequestEnvelope,
)
def request_team_artifact_access(
    artifact_id: UUID,
    request: CreateTeamArtifactAccessRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> TeamArtifactAccessRequestEnvelope:
    _access(identity, "UPDATE")
    _no_store(response)
    return _run(
        lambda: TeamArtifactAccessRequestEnvelope(
            data=get_artifact_collaboration_store().request_access(
                identity, artifact_id, request
            )
        )
    )


@router.get("/{artifact_id}/workspace", response_model=TeamArtifactWorkspaceEnvelope)
def get_team_artifact_workspace(
    artifact_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> TeamArtifactWorkspaceEnvelope:
    _access(identity, "VIEW")
    _no_store(response)
    return _run(
        lambda: TeamArtifactWorkspaceEnvelope(
            data=get_artifact_collaboration_store().get_workspace(
                identity, artifact_id
            )
        )
    )


@router.post(
    "/{artifact_id}/workspace",
    status_code=status.HTTP_201_CREATED,
    response_model=TeamArtifactWorkspaceEnvelope,
)
def create_team_artifact_workspace(
    artifact_id: UUID,
    request: CreateTeamArtifactWorkspaceRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> TeamArtifactWorkspaceEnvelope:
    _access(identity, "UPDATE")
    _no_store(response)
    return _run(
        lambda: TeamArtifactWorkspaceEnvelope(
            data=get_artifact_collaboration_store().create_workspace(
                identity, artifact_id, request
            )
        )
    )


@router.put(
    "/{artifact_id}/workspace/members",
    response_model=TeamArtifactWorkspaceEnvelope,
)
def update_team_artifact_members(
    artifact_id: UUID,
    request: UpdateTeamArtifactMembersRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> TeamArtifactWorkspaceEnvelope:
    _access(identity, "PUBLISH")
    _no_store(response)
    return _run(
        lambda: TeamArtifactWorkspaceEnvelope(
            data=get_artifact_collaboration_store().update_members(
                identity, artifact_id, request
            )
        )
    )


@router.post(
    "/{artifact_id}/workspace/edits", response_model=TeamArtifactEditEnvelope
)
def submit_team_artifact_edit(
    artifact_id: UUID,
    request: SubmitTeamArtifactEditRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> TeamArtifactEditEnvelope:
    _access(identity, "UPDATE")
    _no_store(response)
    return _run(
        lambda: TeamArtifactEditEnvelope(
            data=get_artifact_collaboration_store().edit(
                identity, artifact_id, request
            )
        )
    )


@router.post(
    "/{artifact_id}/workspace/conflicts/{conflict_id}/resolve",
    response_model=TeamArtifactWorkspaceEnvelope,
)
def resolve_team_artifact_conflict(
    artifact_id: UUID,
    conflict_id: UUID,
    request: ResolveTeamArtifactConflictRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> TeamArtifactWorkspaceEnvelope:
    _access(identity, "UPDATE")
    _no_store(response)
    return _run(
        lambda: TeamArtifactWorkspaceEnvelope(
            data=get_artifact_collaboration_store().resolve_conflict(
                identity, artifact_id, conflict_id, request
            )
        )
    )


@router.post(
    "/{artifact_id}/workspace/shares",
    status_code=status.HTTP_201_CREATED,
    response_model=TeamArtifactShareEnvelope,
)
def create_team_artifact_share(
    artifact_id: UUID,
    request: CreateTeamArtifactShareRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> TeamArtifactShareEnvelope:
    _access(identity, "PUBLISH")
    _no_store(response)
    return _run(
        lambda: TeamArtifactShareEnvelope(
            data=get_artifact_collaboration_store().create_share(
                identity, artifact_id, request
            )
        )
    )


@router.post(
    "/{artifact_id}/workspace/shares/{share_id}/revoke",
    response_model=TeamArtifactShareEnvelope,
)
def revoke_team_artifact_share(
    artifact_id: UUID,
    share_id: UUID,
    request: RevokeTeamArtifactShareRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> TeamArtifactShareEnvelope:
    _access(identity, "PUBLISH")
    _no_store(response)
    return _run(
        lambda: TeamArtifactShareEnvelope(
            data=get_artifact_collaboration_store().revoke_share(
                identity, artifact_id, share_id, request
            )
        )
    )


def _access(identity: PersonalDomainIdentity, capability: str) -> None:
    identity.require("APP.ASK:VIEW", f"APP.DWAION_ARTIFACTS:{capability}")


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _run(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except GovernedDomainNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    except GovernedDomainConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    except GovernedDomainUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error
