from __future__ import annotations

from typing import Annotated, Callable, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status

from .artifact_contracts import (
    ArtifactEnvelope,
    ArtifactExportEnvelope,
    ArtifactListEnvelope,
    ArtifactPreflightEnvelope,
    ArtifactPublicationEnvelope,
    ArtifactVersionEnvelope,
    ArtifactVersionDetailEnvelope,
    ArtifactVersionSummaryListEnvelope,
    AutosaveArtifactRequest,
    CreateArtifactRequest,
    CreateArtifactVersionRequest,
    ExportArtifactRequest,
    PublishArtifactRequest,
    RunArtifactPreflightRequest,
)
from .artifact_store import get_artifact_store
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
    prefix="/v1/artifacts",
    tags=["governed-artifacts"],
    dependencies=personal_domain_dependencies,
)

T = TypeVar("T")


@router.get("", response_model=ArtifactListEnvelope)
def list_artifacts(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ArtifactListEnvelope:
    _access(identity, "VIEW")
    _no_store(response)
    return _run(lambda: ArtifactListEnvelope(data=get_artifact_store().list(identity)))


@router.get("/{artifact_id}", response_model=ArtifactEnvelope)
def get_artifact(
    artifact_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ArtifactEnvelope:
    _access(identity, "VIEW")
    _no_store(response)
    return _run(
        lambda: ArtifactEnvelope(data=get_artifact_store().get(identity, artifact_id))
    )


@router.get(
    "/{artifact_id}/versions",
    response_model=ArtifactVersionSummaryListEnvelope,
)
def list_artifact_versions(
    artifact_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    before_version: Annotated[int | None, Query(alias="beforeVersion", ge=1)] = None,
) -> ArtifactVersionSummaryListEnvelope:
    _access(identity, "VIEW")
    _no_store(response)
    return _run(
        lambda: ArtifactVersionSummaryListEnvelope(
            data=get_artifact_store().list_versions(
                identity,
                artifact_id,
                limit=limit,
                before_version=before_version,
            )
        )
    )


@router.get(
    "/{artifact_id}/versions/{version_number}",
    response_model=ArtifactVersionDetailEnvelope,
)
def get_artifact_version(
    artifact_id: UUID,
    version_number: Annotated[int, Path(ge=1)],
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ArtifactVersionDetailEnvelope:
    _access(identity, "VIEW")
    _no_store(response)
    return _run(
        lambda: ArtifactVersionDetailEnvelope(
            data=get_artifact_store().get_version(
                identity, artifact_id, version_number
            )
        )
    )


@router.get(
    "/{artifact_id}/preflights/current",
    response_model=ArtifactPreflightEnvelope,
)
def get_current_artifact_preflight(
    artifact_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ArtifactPreflightEnvelope:
    _access(identity, "VIEW")
    _no_store(response)
    return _run(
        lambda: ArtifactPreflightEnvelope(
            data=get_artifact_store().current_preflight(identity, artifact_id)
        )
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ArtifactEnvelope)
def create_artifact(
    request: CreateArtifactRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ArtifactEnvelope:
    _access(identity, "CREATE")
    _no_store(response)
    return _run(
        lambda: ArtifactEnvelope(data=get_artifact_store().create(identity, request))
    )


@router.put("/{artifact_id}/draft", response_model=ArtifactEnvelope)
def autosave_artifact(
    artifact_id: UUID,
    request: AutosaveArtifactRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ArtifactEnvelope:
    _access(identity, "UPDATE")
    _no_store(response)
    return _run(
        lambda: ArtifactEnvelope(
            data=get_artifact_store().autosave(identity, artifact_id, request)
        )
    )


@router.post("/{artifact_id}/versions", response_model=ArtifactVersionEnvelope)
def create_artifact_version(
    artifact_id: UUID,
    request: CreateArtifactVersionRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ArtifactVersionEnvelope:
    _access(identity, "UPDATE")
    _no_store(response)
    return _run(
        lambda: ArtifactVersionEnvelope(
            data=get_artifact_store().create_version(identity, artifact_id, request)
        )
    )


@router.post("/{artifact_id}/preflights", response_model=ArtifactPreflightEnvelope)
def run_artifact_preflight(
    artifact_id: UUID,
    request: RunArtifactPreflightRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ArtifactPreflightEnvelope:
    _access(identity, "UPDATE")
    _no_store(response)
    return _run(
        lambda: ArtifactPreflightEnvelope(
            data=get_artifact_store().preflight(identity, artifact_id, request)
        )
    )


@router.post("/{artifact_id}/publish", response_model=ArtifactPublicationEnvelope)
def publish_artifact(
    artifact_id: UUID,
    request: PublishArtifactRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ArtifactPublicationEnvelope:
    _access(identity, "PUBLISH")
    _no_store(response)
    return _run(
        lambda: ArtifactPublicationEnvelope(
            data=get_artifact_store().publish(identity, artifact_id, request)
        )
    )


@router.post(
    "/{artifact_id}/exports",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ArtifactExportEnvelope,
)
def export_artifact(
    artifact_id: UUID,
    request: ExportArtifactRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ArtifactExportEnvelope:
    _access(identity, "EXPORT")
    _no_store(response)
    return _run(
        lambda: ArtifactExportEnvelope(
            data=get_artifact_store().export(identity, artifact_id, request)
        )
    )


def _access(identity: PersonalDomainIdentity, capability: str) -> None:
    identity.require("APP.ASK:VIEW", f"APP.DWAION_ARTIFACTS:{capability}")


def _run(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except GovernedDomainNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except GovernedDomainConflict as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except GovernedDomainUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Governed artifacts are unavailable.",
        ) from error


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
