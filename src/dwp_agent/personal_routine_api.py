from __future__ import annotations

from typing import Annotated, Callable, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

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
    ChangeRoutineActivationRequest,
    ChangeRoutineConsentRequest,
    ChangeRoutineLifecycleRequest,
    CommandRoutineRunRequest,
    CreateRoutineRequest,
    DryRunRoutineRequest,
    RoutineDryRunEnvelope,
    RoutineEnvelope,
    RoutineCapabilitiesEnvelope,
    RoutineExecutionEnvelope,
    RoutineExecutionListEnvelope,
    RoutineListEnvelope,
    TriggerRoutineRunRequest,
    UpdateRoutineRequest,
)
from .personal_routine_capabilities import routine_runtime_capabilities
from .personal_routine_advanced_contracts import (
    CreateRoutineAdvancedCommandRequest,
    DecideRoutineAdvancedCommandRequest,
    RoutineAdvancedCommandEnvelope,
    RoutineAdvancedCommandListEnvelope,
)
from .personal_routine_advanced_store import get_personal_routine_advanced_store
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


@router.get("/capabilities", response_model=RoutineCapabilitiesEnvelope)
def get_routine_capabilities(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineCapabilitiesEnvelope:
    _access(identity, write=False)
    _no_store(response)
    return RoutineCapabilitiesEnvelope(data=routine_runtime_capabilities())


@router.get(
    "/advanced-commands/pending-approvals",
    response_model=RoutineAdvancedCommandListEnvelope,
)
def list_pending_routine_approvals(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RoutineAdvancedCommandListEnvelope:
    _approval_access(identity)
    _no_store(response)
    return _run(
        lambda: RoutineAdvancedCommandListEnvelope(
            data=get_personal_routine_advanced_store().list_pending_for_checker(
                identity, limit=limit
            )
        )
    )


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


@router.post("/{routine_id}/activation", response_model=RoutineEnvelope)
def change_routine_activation(
    routine_id: UUID,
    request: ChangeRoutineActivationRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: RoutineEnvelope(
            data=get_personal_routine_store().change_activation(
                identity, routine_id, request
            )
        )
    )


@router.get(
    "/{routine_id}/advanced-commands",
    response_model=RoutineAdvancedCommandListEnvelope,
)
def list_routine_advanced_commands(
    routine_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineAdvancedCommandListEnvelope:
    _access(identity, write=False)
    _no_store(response)
    return _run(
        lambda: RoutineAdvancedCommandListEnvelope(
            data=get_personal_routine_advanced_store().list(identity, routine_id)
        )
    )


@router.post(
    "/{routine_id}/advanced-commands",
    response_model=RoutineAdvancedCommandEnvelope,
)
def create_routine_advanced_command(
    routine_id: UUID,
    request: CreateRoutineAdvancedCommandRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineAdvancedCommandEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: RoutineAdvancedCommandEnvelope(
            data=get_personal_routine_advanced_store().create(
                identity, routine_id, request
            )
        )
    )


@router.get(
    "/{routine_id}/advanced-commands/{command_id}",
    response_model=RoutineAdvancedCommandEnvelope,
)
def get_routine_advanced_command(
    routine_id: UUID,
    command_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineAdvancedCommandEnvelope:
    _access(identity, write=False)
    _no_store(response)
    return _run(
        lambda: RoutineAdvancedCommandEnvelope(
            data=get_personal_routine_advanced_store().get(
                identity, routine_id, command_id
            )
        )
    )


@router.post(
    "/advanced-commands/{command_id}/decision",
    response_model=RoutineAdvancedCommandEnvelope,
)
def decide_routine_advanced_command(
    command_id: UUID,
    request: DecideRoutineAdvancedCommandRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineAdvancedCommandEnvelope:
    _approval_access(identity)
    _no_store(response)
    return _run(
        lambda: RoutineAdvancedCommandEnvelope(
            data=get_personal_routine_advanced_store().decide(
                identity, command_id, request
            )
        )
    )


@router.get("/{routine_id}/runs", response_model=RoutineExecutionListEnvelope)
def list_routine_runs(
    routine_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> RoutineExecutionListEnvelope:
    _access(identity, write=False)
    _no_store(response)
    return _run(
        lambda: RoutineExecutionListEnvelope(
            data=get_personal_routine_store().list_runs(
                identity, routine_id, limit=limit
            )
        )
    )


@router.post(
    "/{routine_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RoutineExecutionEnvelope,
)
def trigger_routine_run(
    routine_id: UUID,
    request: TriggerRoutineRunRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineExecutionEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: RoutineExecutionEnvelope(
            data=get_personal_routine_store().trigger_run(
                identity, routine_id, request
            )
        )
    )


@router.get(
    "/{routine_id}/runs/{routine_run_id}", response_model=RoutineExecutionEnvelope
)
def get_routine_run(
    routine_id: UUID,
    routine_run_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineExecutionEnvelope:
    _access(identity, write=False)
    _no_store(response)
    return _run(
        lambda: RoutineExecutionEnvelope(
            data=get_personal_routine_store().get_run(
                identity, routine_id, routine_run_id
            )
        )
    )


@router.post(
    "/{routine_id}/runs/{routine_run_id}/commands",
    response_model=RoutineExecutionEnvelope,
)
def command_routine_run(
    routine_id: UUID,
    routine_run_id: UUID,
    request: CommandRoutineRunRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineExecutionEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return _run(
        lambda: RoutineExecutionEnvelope(
            data=get_personal_routine_store().command_run(
                identity, routine_id, routine_run_id, request
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


def _approval_access(identity: PersonalDomainIdentity) -> None:
    identity.require("APP.ASK:VIEW")
    identity.require("APP.DWAION_ROUTINES:APPROVE")


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
