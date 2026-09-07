from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from .domain_retention_store import get_domain_retention_store
from .governed_domain_contracts import (
    DeletionJobEnvelope,
    DomainKey,
    PersonalDataGovernanceCapabilities,
    PersonalDataGovernanceCapabilitiesEnvelope,
    RequestDeletionRequest,
    RetentionPoliciesEnvelope,
    RetentionPolicyEnvelope,
    UpsertRetentionPolicyRequest,
)
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
    prefix="/v1/personal-data",
    tags=["personal-data-governance"],
    dependencies=personal_domain_dependencies,
)

admin_router = APIRouter(
    prefix="/v1/admin/personal-data",
    tags=["personal-data-governance-admin"],
    dependencies=personal_domain_dependencies,
)


@router.get("/retention", response_model=RetentionPoliciesEnvelope)
def list_retention_policies(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RetentionPoliciesEnvelope:
    identity.require("APP.ASK:VIEW", "APP.DWAION_PRIVACY:VIEW")
    response.headers["Cache-Control"] = "no-store"
    try:
        return RetentionPoliciesEnvelope(data=get_domain_retention_store().policies(identity))
    except GovernedDomainUnavailable as error:
        _unavailable(error)


@router.get("/capabilities", response_model=PersonalDataGovernanceCapabilitiesEnvelope)
def get_personal_data_governance_capabilities(
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> PersonalDataGovernanceCapabilitiesEnvelope:
    identity.require("APP.ASK:VIEW", "APP.DWAION_PRIVACY:VIEW")
    response.headers["Cache-Control"] = "no-store"
    return PersonalDataGovernanceCapabilitiesEnvelope(
        data=PersonalDataGovernanceCapabilities()
    )


@admin_router.put("/retention/{domain}", response_model=RetentionPolicyEnvelope)
def upsert_retention_policy(
    domain: DomainKey,
    request: UpsertRetentionPolicyRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> RetentionPolicyEnvelope:
    identity.require("ADMIN.DWAION_RETENTION:MANAGE")
    if not identity.roles & {"DWAION_ADMIN", "DWAION_GOVERNANCE_MANAGER"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant data-governance administrator role is required.",
        )
    response.headers["Cache-Control"] = "no-store"
    try:
        return RetentionPolicyEnvelope(
            data=get_domain_retention_store().upsert_policy(identity, domain, request)
        )
    except GovernedDomainConflict as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except GovernedDomainUnavailable as error:
        _unavailable(error)


@router.post(
    "/deletions",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DeletionJobEnvelope,
)
def request_personal_data_deletion(
    request: RequestDeletionRequest,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> DeletionJobEnvelope:
    identity.require("APP.ASK:VIEW", "APP.DWAION_PRIVACY:MANAGE")
    response.headers["Cache-Control"] = "no-store"
    try:
        return DeletionJobEnvelope(
            data=get_domain_retention_store().request_deletion(identity, request)
        )
    except GovernedDomainConflict as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except GovernedDomainUnavailable as error:
        _unavailable(error)


@router.get("/deletions/{deletion_job_id}", response_model=DeletionJobEnvelope)
def get_personal_data_deletion(
    deletion_job_id: UUID,
    identity: Annotated[PersonalDomainIdentity, Depends(require_personal_domain_identity)],
    response: Response,
) -> DeletionJobEnvelope:
    identity.require("APP.ASK:VIEW", "APP.DWAION_PRIVACY:VIEW")
    response.headers["Cache-Control"] = "no-store"
    try:
        return DeletionJobEnvelope(
            data=get_domain_retention_store().deletion_job(identity, deletion_job_id)
        )
    except GovernedDomainNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except GovernedDomainUnavailable as error:
        _unavailable(error)


def _unavailable(error: Exception) -> None:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Personal data governance is unavailable.",
    ) from error
