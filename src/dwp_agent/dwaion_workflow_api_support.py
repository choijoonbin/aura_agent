from __future__ import annotations

from typing import Callable, TypeVar

from fastapi import HTTPException, Response

from .dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
)
from .personal_domain_security import PersonalDomainIdentity


T = TypeVar("T")


def require_attachment_access(
    identity: PersonalDomainIdentity, *, write: bool
) -> None:
    identity.require("APP.ASK:VIEW")
    identity.require(
        "APP.DWAION_ATTACHMENTS:MANAGE" if write else "APP.DWAION_ATTACHMENTS:VIEW"
    )


def require_research_access(identity: PersonalDomainIdentity, *, write: bool) -> None:
    identity.require("APP.ASK:VIEW")
    identity.require(
        "APP.DWAION_RESEARCH:MANAGE" if write else "APP.DWAION_RESEARCH:VIEW"
    )


def run_workflow(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except DwaionWorkflowNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DwaionWorkflowConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except DwaionWorkflowUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def prevent_response_storage(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
