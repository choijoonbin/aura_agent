from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from .meeting_intelligence_contracts import (
    MeetingIntelligenceAnalysis,
    MeetingIntelligenceCapability,
    MeetingIntelligenceRequest,
)
from .meeting_intelligence_provider import (
    MeetingIntelligenceProvider,
    MeetingIntelligenceUnavailable,
)
from .meeting_intelligence_security import (
    ASSERTION_HEADER,
    TOKEN_HEADER,
    MeetingWorkloadAssertionVerifier,
    MeetingWorkloadIdentityConfiguration,
    MeetingWorkloadIdentityError,
    meeting_assertion_replay_store,
)


router = APIRouter(prefix="/internal/v1/meeting-intelligence", tags=["meeting-intelligence"])


async def require_meeting_service(
    request: Request,
    service_token: Annotated[
        str | None,
        Header(alias=TOKEN_HEADER, include_in_schema=False),
    ] = None,
    assertion: Annotated[
        str | None,
        Header(alias=ASSERTION_HEADER, include_in_schema=False),
    ] = None,
    tenant_id: Annotated[int | None, Header(alias="X-DWP-Tenant-ID", gt=0)] = None,
    meeting_id: Annotated[UUID | None, Header(alias="X-DWP-Meeting-ID")] = None,
    run_id: Annotated[UUID | None, Header(alias="X-DWP-Intelligence-Run-ID")] = None,
) -> None:
    if tenant_id is None or meeting_id is None or run_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid meeting workload identity.",
        )
    try:
        verifier = MeetingWorkloadAssertionVerifier(
            MeetingWorkloadIdentityConfiguration.from_environment(),
            meeting_assertion_replay_store(),
        )
        verifier.verify(
            service_token=service_token,
            assertion=assertion,
            method=request.method,
            path=request.url.path,
            tenant_id=tenant_id,
            meeting_id=meeting_id,
            run_id=run_id,
            body=await request.body(),
        )
    except MeetingWorkloadIdentityError as error:
        unavailable = "configured" in str(error) or "unavailable" in str(error)
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if unavailable
                else status.HTTP_401_UNAUTHORIZED
            ),
            detail=(
                "Meeting workload identity is unavailable."
                if unavailable
                else "Invalid meeting workload identity."
            ),
        ) from error


def get_meeting_intelligence_provider() -> MeetingIntelligenceProvider:
    return MeetingIntelligenceProvider()


@router.get(
    "/capabilities",
    include_in_schema=False,
    response_model=MeetingIntelligenceCapability,
    response_model_by_alias=True,
    dependencies=[Depends(require_meeting_service)],
)
def capabilities(
    response: Response,
    provider: MeetingIntelligenceProvider = Depends(get_meeting_intelligence_provider),
) -> MeetingIntelligenceCapability:
    response.headers["Cache-Control"] = "no-store"
    return provider.capability()


@router.post(
    "/analyze",
    include_in_schema=False,
    response_model=MeetingIntelligenceAnalysis,
    response_model_by_alias=True,
    dependencies=[Depends(require_meeting_service)],
)
def analyze(
    request: MeetingIntelligenceRequest,
    response: Response,
    correlation_id: Annotated[
        str,
        Header(alias="X-Correlation-ID", min_length=1, max_length=160),
    ],
    tenant_id: Annotated[int, Header(alias="X-DWP-Tenant-ID", gt=0)],
    meeting_id: Annotated[UUID, Header(alias="X-DWP-Meeting-ID")],
    run_id: Annotated[UUID, Header(alias="X-DWP-Intelligence-Run-ID")],
    provider: MeetingIntelligenceProvider = Depends(get_meeting_intelligence_provider),
) -> MeetingIntelligenceAnalysis:
    try:
        analysis = provider.analyze(
            request,
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            meeting_id=str(meeting_id),
            run_id=str(run_id),
        )
    except MeetingIntelligenceUnavailable as error:
        code = error.code if error.code in {
            "PROVIDER_UNAVAILABLE",
            "POLICY_BLOCKED",
            "INVALID_PROVIDER_OUTPUT",
        } else "PROVIDER_UNAVAILABLE"
        http_status = (
            status.HTTP_502_BAD_GATEWAY
            if code == "INVALID_PROVIDER_OUTPUT"
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        raise HTTPException(status_code=http_status, detail=code) from error
    response.headers["Cache-Control"] = "no-store"
    return analysis
