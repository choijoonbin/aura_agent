from __future__ import annotations

import os
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import Field

from .contract_model import ContractModel
from .personal_domain_security import (
    PersonalDomainIdentity,
    personal_domain_dependencies,
    require_personal_domain_identity,
)


class DwaionPageBootstrap(ContractModel):
    page_key: str
    available: bool
    reason: str | None = None
    resource_id: UUID | None = None
    data_routes: list[str] = Field(min_length=1)


class DwaionPageBootstrapEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: DwaionPageBootstrap


router = APIRouter(
    prefix="/v1/navigation", tags=["navigation"],
    dependencies=personal_domain_dependencies,
)


@router.get("/new", response_model=DwaionPageBootstrapEnvelope)
def new_page(identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
             response: Response) -> DwaionPageBootstrapEnvelope:
    return _page(identity, response, "NEW", ["/v1/ask", "/v1/attachments/capabilities"],
                 attachment_required=True)


@router.get("/conversations", response_model=DwaionPageBootstrapEnvelope)
def conversations_page(identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
                       response: Response) -> DwaionPageBootstrapEnvelope:
    return _page(identity, response, "CONVERSATIONS", ["/v1/conversations"])


@router.get("/conversations/{conversation_id}", response_model=DwaionPageBootstrapEnvelope)
def conversation_page(conversation_id: UUID,
                      identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
                      response: Response) -> DwaionPageBootstrapEnvelope:
    return _page(identity, response, "CONVERSATION_DETAIL",
                 [f"/v1/conversations/{conversation_id}"], resource_id=conversation_id)


@router.get("/activity", response_model=DwaionPageBootstrapEnvelope)
def activity_page(identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
                  response: Response) -> DwaionPageBootstrapEnvelope:
    return _page(identity, response, "ACTIVITY", ["/v1/activity/events", "/v1/runs"])


@router.get("/agents", response_model=DwaionPageBootstrapEnvelope)
def agents_page(identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
                response: Response) -> DwaionPageBootstrapEnvelope:
    configured = os.getenv("DWP_AGENT_REGISTRY_MODE", "required").strip().lower() in {"required", "optional"}
    return _page(identity, response, "AGENTS", ["/v1/actions"], available=configured,
                 reason=None if configured else "The governed Agent registry is not configured.")


def _page(identity: PersonalDomainIdentity, response: Response, page_key: str,
          data_routes: list[str], *, resource_id: UUID | None = None,
          attachment_required: bool = False, available: bool | None = None,
          reason: str | None = None) -> DwaionPageBootstrapEnvelope:
    identity.require("APP.ASK:VIEW")
    response.headers["Cache-Control"] = "no-store"
    database = bool(os.getenv("DWP_AGENT_DATABASE_URL", "").strip())
    ready = database if available is None else database and available
    if attachment_required:
        ready = ready and os.getenv("DWP_ATTACHMENT_BROKER_ENABLED", "false").lower() == "true"
    if not ready and reason is None:
        reason = "Required DWAI-ON runtime capability is not configured."
    return DwaionPageBootstrapEnvelope(data=DwaionPageBootstrap(
        page_key=page_key, available=ready, reason=reason,
        resource_id=resource_id, data_routes=data_routes,
    ))
