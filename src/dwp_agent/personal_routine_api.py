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
from .personal_routine_contracts import (
    ArchiveRoutineRequest,
    ChangeRoutineConsentRequest,
    ChangeRoutineLifecycleRequest,
    CreateRoutineRequest,
    DryRunRoutineRequest,
    RoutineDryRunEnvelope,
    RoutineEnvelope,
    RoutineListEnvelope,
    UpdateRoutineRequest,
)
from .personal_routine_store import get_personal_routine_store


router = APIRouter(
    prefix="/v1/routines",
    tags=["personal-routines"],
    dependencies=personal_domain_dependencies,
)

T = TypeVar("T")


@router.get("", response_model=RoutineListEnvelope)
def list_routines(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineListEnvelope:
    _access(identity, write=False)
    _no_store(response)
    return _run(lambda: RoutineListEnvelope(data=get_personal_routine_store().list(identity)))


@router.get("/{routine_id}", response_model=RoutineEnvelope)
def get_routine(
    routine_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineEnvelope:
    _access(identity, write=False)
    _no_store(response)
    return _run(
        lambda: RoutineEnvelope(data=get_personal_routine_store().get(identity, routine_id))
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=RoutineEnvelope)
def create_routine(
    request: CreateRoutineRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: RoutineEnvelope(data=get_personal_routine_store().create(identity, request))
    )


@router.put("/{routine_id}", response_model=RoutineEnvelope)
def update_routine(
    routine_id: UUID,
    request: UpdateRoutineRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: RoutineEnvelope(
            data=get_personal_routine_store().update(identity, routine_id, request)
        )
    )


@router.post("/{routine_id}/consent", response_model=RoutineEnvelope)
def change_routine_consent(
    routine_id: UUID,
    request: ChangeRoutineConsentRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: RoutineEnvelope(
            data=get_personal_routine_store().change_consent(identity, routine_id, request)
        )
    )


@router.post("/{routine_id}/lifecycle", response_model=RoutineEnvelope)
def change_routine_lifecycle(
    routine_id: UUID,
    request: ChangeRoutineLifecycleRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: RoutineEnvelope(
            data=get_personal_routine_store().change_lifecycle(
                identity, routine_id, request
            )
        )
    )


@router.post("/{routine_id}/dry-runs", response_model=RoutineDryRunEnvelope)
def dry_run_routine(
    routine_id: UUID,
    request: DryRunRoutineRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineDryRunEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: RoutineDryRunEnvelope(
            data=get_personal_routine_store().dry_run(identity, routine_id, request)
        )
    )


@router.post("/{routine_id}/archive", response_model=RoutineEnvelope)
def archive_routine(
    routine_id: UUID,
    request: ArchiveRoutineRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: RoutineEnvelope(
            data=get_personal_routine_store().archive(identity, routine_id, request)
        )
    )


def _access(identity: PersonalDomainIdentity, *, write: bool) -> None:
    identity.require("APP.ASK:VIEW")
    identity.require("APP.DWAION_ROUTINES:MANAGE" if write else "APP.DWAION_ROUTINES:VIEW")


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
            detail="Personal routines are unavailable.",
        ) from error


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
