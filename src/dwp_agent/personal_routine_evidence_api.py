from __future__ import annotations

import json
from typing import Annotated, Callable, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status

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
    RoutineExecutionEnvelope,
)
from .personal_routine_evidence_contracts import (
    RollbackRoutineVersionRequest,
    RoutineHealthEnvelope,
    RoutineRollbackEnvelope,
    RoutineVersionListEnvelope,
    TriggerRoutineWebhookRequest,
)
from .personal_routine_evidence_store import get_personal_routine_evidence_store


router = APIRouter(
    prefix="/v1/routines",
    tags=["personal-routine-evidence"],
    dependencies=personal_domain_dependencies,
)
T = TypeVar("T")


@router.post(
    "/{routine_id}/webhook-events",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RoutineExecutionEnvelope,
)
def trigger_routine_webhook(
    routine_id: UUID,
    request: TriggerRoutineWebhookRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineExecutionEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return RoutineExecutionEnvelope(data=_run(
        lambda: get_personal_routine_evidence_store().trigger_webhook(
            identity, routine_id, request
        )
    ))


@router.get("/{routine_id}/versions", response_model=RoutineVersionListEnvelope)
def list_routine_versions(
    routine_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineVersionListEnvelope:
    _access(identity, write=False)
    _no_store(response)
    return RoutineVersionListEnvelope(data=_run(
        lambda: get_personal_routine_evidence_store().versions(identity, routine_id)
    ))


@router.post(
    "/{routine_id}/versions/{revision}/rollback",
    response_model=RoutineRollbackEnvelope,
)
def rollback_routine_version(
    routine_id: UUID,
    revision: Annotated[int, Path(ge=1)],
    request: RollbackRoutineVersionRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineRollbackEnvelope:
    _access(identity, write=True)
    _no_store(response)
    return RoutineRollbackEnvelope(data=_run(
        lambda: get_personal_routine_evidence_store().rollback(
            identity, routine_id, revision, request
        )
    ))


@router.get("/{routine_id}/health", response_model=RoutineHealthEnvelope)
def get_routine_health(
    routine_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RoutineHealthEnvelope:
    _access(identity, write=False)
    _no_store(response)
    return RoutineHealthEnvelope(data=_run(
        lambda: get_personal_routine_evidence_store().health(identity, routine_id)
    ))


@router.get(
    "/{routine_id}/telemetry/download",
    response_class=Response,
    responses={200: {"content": {"application/x-ndjson": {"schema": {"type": "string"}}}}},
)
def download_routine_telemetry(
    routine_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
) -> Response:
    _access(identity, write=False)
    events = _run(
        lambda: get_personal_routine_evidence_store().telemetry(identity, routine_id)
    )
    content = "\n".join(
        json.dumps(event.model_dump(mode="json", by_alias=True), ensure_ascii=False)
        for event in events
    ) + "\n"
    return Response(
        content=content,
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="routine-{routine_id}-telemetry.jsonl"',
        },
    )


def _access(identity: PersonalDomainIdentity, *, write: bool) -> None:
    identity.require("APP.ASK:VIEW")
    identity.require("APP.DWAION_ROUTINES:MANAGE" if write else "APP.DWAION_ROUTINES:VIEW")


def _run(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except GovernedDomainNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except GovernedDomainConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except GovernedDomainUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
