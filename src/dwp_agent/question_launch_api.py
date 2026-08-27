from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from .question_launch_contracts import (
    ConsumeQuestionLaunchRequest,
    CreateQuestionLaunchRequest,
    QuestionLaunchPayload,
    QuestionLaunchPayloadEnvelope,
    QuestionLaunchReceipt,
    QuestionLaunchReceiptEnvelope,
)
from .question_launch_store import (
    QuestionLaunchCapacityExceeded,
    QuestionLaunchNotFound,
    QuestionLaunchUnavailable,
    get_question_launch_store,
)
from .security import header_values, require_gateway_service


router = APIRouter(
    prefix="/v1/question-launches",
    tags=["question-launches"],
    dependencies=[Depends(require_gateway_service)],
)


def require_ask_access(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
    identity_plane: Annotated[
        str | None, Header(alias="X-DWP-Identity-Plane")
    ] = None,
) -> None:
    if identity_plane != "TENANT" or "APP.ASK:VIEW" not in header_values(permissions):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON access is required.",
        )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=QuestionLaunchReceiptEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_ask_access)],
)
def create_question_launch(
    request: CreateQuestionLaunchRequest,
    tenant_id: Annotated[
        str, Header(alias="X-DWP-Tenant-ID", min_length=1, max_length=32)
    ],
    user_id: Annotated[
        str, Header(alias="X-DWP-User-ID", min_length=1, max_length=160)
    ],
    session_family_id: Annotated[
        str, Header(alias="X-DWP-Auth-Session-ID", min_length=1, max_length=160)
    ],
) -> QuestionLaunchReceiptEnvelope:
    try:
        launch = get_question_launch_store().create(
            tenant_id=tenant_id,
            user_id=user_id,
            session_family_id=session_family_id,
            question=request.question,
        )
    except QuestionLaunchCapacityExceeded as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Question launch capacity is temporarily exhausted.",
            headers={"Retry-After": str(error.retry_after_seconds)},
        ) from error
    except QuestionLaunchUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Question launch is unavailable.",
        ) from error
    return QuestionLaunchReceiptEnvelope(
        data=QuestionLaunchReceipt(
            launch_id=launch.launch_id,
            expires_at=launch.expires_at,
        )
    )


@router.post(
    "/consume",
    response_model=QuestionLaunchPayloadEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_ask_access)],
)
def consume_question_launch(
    request: ConsumeQuestionLaunchRequest,
    tenant_id: Annotated[
        str, Header(alias="X-DWP-Tenant-ID", min_length=1, max_length=32)
    ],
    user_id: Annotated[
        str, Header(alias="X-DWP-User-ID", min_length=1, max_length=160)
    ],
    session_family_id: Annotated[
        str, Header(alias="X-DWP-Auth-Session-ID", min_length=1, max_length=160)
    ],
) -> QuestionLaunchPayloadEnvelope:
    try:
        question = get_question_launch_store().consume(
            tenant_id=tenant_id,
            user_id=user_id,
            session_family_id=session_family_id,
            launch_id=request.launch_id,
        )
    except QuestionLaunchNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Question launch is unavailable.",
        ) from error
    except QuestionLaunchUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Question launch is unavailable.",
        ) from error
    return QuestionLaunchPayloadEnvelope(data=QuestionLaunchPayload(question=question))
