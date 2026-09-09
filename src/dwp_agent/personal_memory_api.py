from __future__ import annotations

from typing import Annotated, Callable, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

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
from .personal_memory_contracts import (
    AiSourceKey,
    AiSourcePreferenceEnvelope,
    ChangeMemoryStateRequest,
    CreateMemoryRequest,
    DeleteMemoryRequest,
    MemoryEnvelope,
    MemoryListEnvelope,
    PersonalAiControlsEnvelope,
    UpdateMemoryPreferenceRequest,
    UpdateMemoryRuntimePreferenceRequest,
    UpdateAiSourcePreferenceRequest,
    UpdateMemoryRequest,
)
from .personal_memory_store import get_personal_memory_store


router = APIRouter(
    prefix="/v1/ai-controls",
    tags=["personal-ai-controls"],
    dependencies=personal_domain_dependencies,
)

T = TypeVar("T")


@router.get("", response_model=PersonalAiControlsEnvelope)
def get_personal_ai_controls(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> PersonalAiControlsEnvelope:
    _access(identity, write=False)
    _no_store(response)
    return _run(
        lambda: PersonalAiControlsEnvelope(
            data=get_personal_memory_store().controls(identity)
        )
    )


@router.put("", response_model=PersonalAiControlsEnvelope)
def update_personal_ai_controls(
    request: UpdateMemoryPreferenceRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> PersonalAiControlsEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: PersonalAiControlsEnvelope(
            data=get_personal_memory_store().update_controls(identity, request)
        )
    )


@router.put("/runtime", response_model=PersonalAiControlsEnvelope)
def update_personal_ai_runtime_controls(
    request: UpdateMemoryRuntimePreferenceRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> PersonalAiControlsEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: PersonalAiControlsEnvelope(
            data=get_personal_memory_store().update_runtime_controls(identity, request)
        )
    )


@router.put("/sources/{source_key}", response_model=AiSourcePreferenceEnvelope)
def update_ai_source_preference(
    source_key: AiSourceKey,
    request: UpdateAiSourcePreferenceRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> AiSourcePreferenceEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: AiSourcePreferenceEnvelope(
            data=get_personal_memory_store().update_source_preference(
                identity, source_key, request
            )
        )
    )


@router.get("/memories", response_model=MemoryListEnvelope)
def list_personal_memories(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> MemoryListEnvelope:
    _access(identity, write=False)
    _no_store(response)
    return _run(
        lambda: MemoryListEnvelope(data=get_personal_memory_store().list(identity))
    )


@router.post(
    "/memories",
    status_code=status.HTTP_201_CREATED,
    response_model=MemoryEnvelope,
)
def create_personal_memory(
    request: CreateMemoryRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> MemoryEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: MemoryEnvelope(data=get_personal_memory_store().create(identity, request))
    )


@router.put("/memories/{memory_id}", response_model=MemoryEnvelope)
def update_personal_memory(
    memory_id: UUID,
    request: UpdateMemoryRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> MemoryEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: MemoryEnvelope(
            data=get_personal_memory_store().update(identity, memory_id, request)
        )
    )


@router.post("/memories/{memory_id}/state", response_model=MemoryEnvelope)
def change_personal_memory_state(
    memory_id: UUID,
    request: ChangeMemoryStateRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> MemoryEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: MemoryEnvelope(
            data=get_personal_memory_store().change_state(identity, memory_id, request)
        )
    )


@router.post("/memories/{memory_id}/delete", response_model=MemoryEnvelope)
def delete_personal_memory(
    memory_id: UUID,
    request: DeleteMemoryRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> MemoryEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: MemoryEnvelope(
            data=get_personal_memory_store().delete(identity, memory_id, request)
        )
    )


def _access(identity: PersonalDomainIdentity, *, write: bool) -> None:
    identity.require("APP.ASK:VIEW")
    identity.require("APP.DWAION_MEMORY:MANAGE" if write else "APP.DWAION_MEMORY:VIEW")


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
            detail="Personal AI controls are unavailable.",
        ) from error


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
