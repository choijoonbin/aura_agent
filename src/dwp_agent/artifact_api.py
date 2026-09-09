from __future__ import annotations

from typing import Annotated, Callable, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status

from .artifact_contracts import (
    ArtifactCapabilitiesEnvelope,
    ArtifactEnvelope,
    ArtifactExportEnvelope,
    ArtifactListEnvelope,
    ArtifactPreflightEnvelope,
    ArtifactPublicationEnvelope,
    ArtifactSourceReference,
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
from .artifact_runtime_capabilities import artifact_runtime_capabilities
from .artifact_store import get_artifact_store
from .contracts import ConversationRole
from .conversation_store import (
    ConversationNotFound,
    ConversationStoreUnavailable,
    get_conversation_store,
)
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
)
from .grounded_response_status import (
    GROUNDED_ANSWER_STATUS,
    GROUNDED_FALLBACK_STATUS,
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


@router.get("/capabilities", response_model=ArtifactCapabilitiesEnvelope)
def get_artifact_capabilities(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ArtifactCapabilitiesEnvelope:
    _access(identity, "VIEW")
    _no_store(response)
    return ArtifactCapabilitiesEnvelope(data=artifact_runtime_capabilities())


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


@router.get(
    "/{artifact_id}/exports/{export_job_id}",
    response_model=ArtifactExportEnvelope,
)
def get_artifact_export(
    artifact_id: UUID,
    export_job_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> ArtifactExportEnvelope:
    _access(identity, "EXPORT")
    _no_store(response)
    return _run(
        lambda: ArtifactExportEnvelope(
            data=get_artifact_store().export_job(
                identity, artifact_id, export_job_id
            )
        )
    )


@router.get("/{artifact_id}/exports/{export_job_id}/download")
def download_artifact_export(
    artifact_id: UUID,
    export_job_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
) -> Response:
    _access(identity, "EXPORT")
    file = _run(
        lambda: get_artifact_store().export_file(
            identity, artifact_id, export_job_id
        )
    )
    return Response(
        content=file.content,
        media_type=file.media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{file.file_name}"',
            "X-Content-Type-Options": "nosniff",
            "X-DWP-Content-Fingerprint": file.content_fingerprint,
        },
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
        lambda: ArtifactEnvelope(
            data=get_artifact_store().create(
                identity, _resolve_conversation_sources(identity, request)
            )
        )
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


def _resolve_conversation_sources(
    identity: PersonalDomainIdentity,
    request: CreateArtifactRequest,
) -> CreateArtifactRequest:
    binding = request.source_conversation
    if binding is None:
        return request
    try:
        conversation = get_conversation_store().get(
            tenant_id=str(identity.tenant_id),
            user_id=identity.user_id,
            conversation_id=binding.conversation_id,
        )
    except ConversationNotFound as error:
        raise GovernedDomainNotFound(str(error)) from error
    except ConversationStoreUnavailable as error:
        raise GovernedDomainUnavailable(
            "Conversation provenance is unavailable."
        ) from error

    if conversation.summary.conversation_id != binding.conversation_id:
        raise GovernedDomainNotFound(
            "Conversation was not found in the verified user scope."
        )
    message = next(
        (
            item
            for item in conversation.messages
            if item.message_id == binding.assistant_message_id
        ),
        None,
    )
    if message is None or message.role != ConversationRole.ASSISTANT:
        raise GovernedDomainNotFound(
            "The assistant message was not found in the verified conversation scope."
        )
    if message.status_code not in {
        GROUNDED_ANSWER_STATUS,
        GROUNDED_FALLBACK_STATUS,
    }:
        raise GovernedDomainConflict(
            "Only a grounded assistant answer can create a conversation-bound artifact."
        )
    if not message.citations:
        raise GovernedDomainConflict(
            "A conversation-bound artifact requires grounded citations."
        )
    if request.content.body != message.content:
        raise GovernedDomainConflict(
            "Artifact content must match the bound assistant answer exactly."
        )

    sources = [
        ArtifactSourceReference(
            source_type=citation.source_type,
            reference=(
                f"conversation:{binding.conversation_id}:"
                f"message:{binding.assistant_message_id}:"
                f"citation:{citation.source_id}"
            ),
        )
        for citation in message.citations
    ]
    resolved = request.model_copy(update={"sources": sources, "source_conversation": None})
    resolved._verified_source_references = frozenset(
        source.reference for source in sources
    )
    return resolved


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
