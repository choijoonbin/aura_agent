from __future__ import annotations

from typing import TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ValidationError

from .meeting_media_contracts import (
    RecordingAccessTicketRequest,
    RecordingAccessTicketResponse,
    RecordingCapability,
    RecordingCommandRequest,
    RecordingCommandResponse,
    RecordingDeleteRequest,
    RecordingDeleteResponse,
    TranscriptDeleteRequest,
    TranscriptDeleteResponse,
    TranscriptReadRequest,
    TranscriptReadResponse,
    TranscriptRetentionCapability,
)
from .meeting_media_provider import (
    RECORDING_ACCESS_PATH,
    RECORDING_CAPABILITY_PATH,
    RECORDING_DELETE_PATH,
    RECORDING_START_PATH,
    RECORDING_STOP_PATH,
    TRANSCRIPT_DELETE_PATH,
    TRANSCRIPT_READ_PATH,
    TRANSCRIPT_RETENTION_PATH,
    MeetingMediaBroker,
    MeetingMediaUnavailable,
)
from .meeting_media_security import (
    ASSERTION_HEADER,
    RECORDING_TOKEN_HEADER,
    TRANSCRIPT_TOKEN_HEADER,
    MeetingMediaAssertionVerifier,
    MeetingMediaIdentityConfiguration,
    MeetingMediaIdentityError,
    MeetingMediaPurpose,
    meeting_media_replay_store,
)


router = APIRouter()
_Model = TypeVar("_Model", bound=BaseModel)


def get_meeting_media_broker() -> MeetingMediaBroker:
    return MeetingMediaBroker()


@router.get(
    RECORDING_CAPABILITY_PATH,
    include_in_schema=False,
    response_model=RecordingCapability,
    response_model_by_alias=True,
)
async def recording_capability(
    request: Request,
    response: Response,
    broker: MeetingMediaBroker = Depends(get_meeting_media_broker),
) -> RecordingCapability:
    _verify_service(request, MeetingMediaPurpose.RECORDING, b"")
    response.headers["Cache-Control"] = "no-store"
    return broker.recording_capability()


@router.post(
    RECORDING_START_PATH,
    include_in_schema=False,
    response_model=RecordingCommandResponse,
    response_model_by_alias=True,
)
async def start_recording(
    request: Request,
    response: Response,
    broker: MeetingMediaBroker = Depends(get_meeting_media_broker),
) -> RecordingCommandResponse:
    return await _recording_command(request, response, broker, RECORDING_START_PATH)


@router.post(
    RECORDING_STOP_PATH,
    include_in_schema=False,
    response_model=RecordingCommandResponse,
    response_model_by_alias=True,
)
async def stop_recording(
    request: Request,
    response: Response,
    broker: MeetingMediaBroker = Depends(get_meeting_media_broker),
) -> RecordingCommandResponse:
    return await _recording_command(request, response, broker, RECORDING_STOP_PATH)


@router.post(
    RECORDING_ACCESS_PATH,
    include_in_schema=False,
    response_model=RecordingAccessTicketResponse,
    response_model_by_alias=True,
)
async def recording_access_ticket(
    request: Request,
    response: Response,
    broker: MeetingMediaBroker = Depends(get_meeting_media_broker),
) -> RecordingAccessTicketResponse:
    body = await request.body()
    tenant_id, meeting_id = _resource_headers(request)
    artifact_id = _uuid_header(request, "X-DWP-Recording-Artifact-ID")
    requester_user_id = _positive_integer_header(request, "X-DWP-Requester-User-ID")
    _verify_resource(
        request,
        MeetingMediaPurpose.RECORDING,
        body,
        tenant_id,
        meeting_id,
        artifact_id,
    )
    parsed = _parse(body, RecordingAccessTicketRequest, "INVALID_RECORDING_ACCESS_REQUEST")
    _require_equal(
        (parsed.tenant_id, parsed.meeting_id, parsed.artifact_id, parsed.requester_user_id),
        (tenant_id, meeting_id, artifact_id, requester_user_id),
    )
    try:
        result = broker.recording_access_ticket(
            parsed, correlation_id=_correlation_id(request)
        )
    except MeetingMediaUnavailable as error:
        raise _unavailable("RECORDING_ACCESS_UNAVAILABLE") from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    RECORDING_DELETE_PATH,
    include_in_schema=False,
    response_model=RecordingDeleteResponse,
    response_model_by_alias=True,
)
async def delete_recording(
    request: Request,
    response: Response,
    broker: MeetingMediaBroker = Depends(get_meeting_media_broker),
) -> RecordingDeleteResponse:
    body = await request.body()
    tenant_id, meeting_id = _resource_headers(request)
    artifact_id = _uuid_header(request, "X-DWP-Recording-Artifact-ID")
    _verify_resource(
        request,
        MeetingMediaPurpose.RECORDING,
        body,
        tenant_id,
        meeting_id,
        artifact_id,
    )
    parsed = _parse(body, RecordingDeleteRequest, "INVALID_RECORDING_DELETE_REQUEST")
    _require_equal(
        (parsed.tenant_id, parsed.meeting_id, parsed.artifact_id),
        (tenant_id, meeting_id, artifact_id),
    )
    idempotency_key = _idempotency_key(request, f"DELETE:{artifact_id}")
    try:
        result = broker.recording_delete(
            parsed,
            correlation_id=_correlation_id(request),
            idempotency_key=idempotency_key,
        )
    except MeetingMediaUnavailable as error:
        raise _unavailable("RECORDING_DELETE_UNAVAILABLE") from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    TRANSCRIPT_RETENTION_PATH,
    include_in_schema=False,
    response_model=TranscriptRetentionCapability,
    response_model_by_alias=True,
)
async def transcript_retention_capability(
    request: Request,
    response: Response,
    broker: MeetingMediaBroker = Depends(get_meeting_media_broker),
) -> TranscriptRetentionCapability:
    _verify_service(request, MeetingMediaPurpose.TRANSCRIPT, b"")
    response.headers["Cache-Control"] = "no-store"
    return broker.transcript_retention_capability()


@router.post(
    TRANSCRIPT_READ_PATH,
    include_in_schema=False,
    response_model=TranscriptReadResponse,
    response_model_by_alias=True,
)
async def read_transcript(
    request: Request,
    response: Response,
    broker: MeetingMediaBroker = Depends(get_meeting_media_broker),
) -> TranscriptReadResponse:
    body = await request.body()
    tenant_id, meeting_id = _resource_headers(request)
    run_id = _uuid_header(request, "X-DWP-Intelligence-Run-ID")
    artifact_id = _uuid_header(request, "X-DWP-Transcript-Artifact-ID")
    source_sha256 = _required_header(request, "X-DWP-Source-SHA256")
    _verify_resource(
        request,
        MeetingMediaPurpose.TRANSCRIPT,
        body,
        tenant_id,
        meeting_id,
        run_id,
    )
    parsed = _parse(body, TranscriptReadRequest, "INVALID_TRANSCRIPT_READ_REQUEST")
    _require_equal(
        (
            parsed.tenant_id,
            parsed.meeting_id,
            parsed.run_id,
            parsed.artifact_id,
            parsed.source_sha256,
        ),
        (tenant_id, meeting_id, run_id, artifact_id, source_sha256),
    )
    try:
        result = broker.transcript_read(parsed, correlation_id=_correlation_id(request))
    except MeetingMediaUnavailable as error:
        raise _unavailable("TRANSCRIPT_READ_UNAVAILABLE") from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    TRANSCRIPT_DELETE_PATH,
    include_in_schema=False,
    response_model=TranscriptDeleteResponse,
    response_model_by_alias=True,
)
async def delete_transcript(
    request: Request,
    response: Response,
    broker: MeetingMediaBroker = Depends(get_meeting_media_broker),
) -> TranscriptDeleteResponse:
    body = await request.body()
    tenant_id, meeting_id = _resource_headers(request)
    artifact_id = _uuid_header(request, "X-DWP-Transcript-Artifact-ID")
    _verify_resource(
        request,
        MeetingMediaPurpose.TRANSCRIPT,
        body,
        tenant_id,
        meeting_id,
        artifact_id,
    )
    parsed = _parse(body, TranscriptDeleteRequest, "INVALID_TRANSCRIPT_DELETE_REQUEST")
    _require_equal(
        (parsed.tenant_id, parsed.meeting_id, parsed.artifact_id),
        (tenant_id, meeting_id, artifact_id),
    )
    idempotency_key = _idempotency_key(request, f"DELETE:{artifact_id}")
    try:
        result = broker.transcript_delete(
            parsed,
            correlation_id=_correlation_id(request),
            idempotency_key=idempotency_key,
        )
    except MeetingMediaUnavailable as error:
        raise _unavailable("TRANSCRIPT_DELETE_UNAVAILABLE") from error
    response.headers["Cache-Control"] = "no-store"
    return result


async def _recording_command(
    request: Request,
    response: Response,
    broker: MeetingMediaBroker,
    path: str,
) -> RecordingCommandResponse:
    body = await request.body()
    tenant_id, meeting_id = _resource_headers(request)
    session_id = _uuid_header(request, "X-DWP-Recording-Session-ID")
    expected_type = "START" if path == RECORDING_START_PATH else "STOP"
    _verify_resource(
        request,
        MeetingMediaPurpose.RECORDING,
        body,
        tenant_id,
        meeting_id,
        session_id,
    )
    parsed = _parse(body, RecordingCommandRequest, "INVALID_RECORDING_COMMAND")
    _require_equal(
        (parsed.tenant_id, parsed.meeting_id, parsed.recording_session_id, parsed.command_type),
        (tenant_id, meeting_id, session_id, expected_type),
    )
    idempotency_key = _idempotency_key(request, f"{expected_type}:{session_id}")
    try:
        result = broker.recording_command(
            path,
            parsed,
            correlation_id=_correlation_id(request),
            idempotency_key=idempotency_key,
        )
    except MeetingMediaUnavailable as error:
        raise _unavailable("RECORDING_COMMAND_UNAVAILABLE") from error
    response.headers["Cache-Control"] = "no-store"
    return result


def _verify_service(
    request: Request, purpose: MeetingMediaPurpose, body: bytes
) -> None:
    try:
        MeetingMediaAssertionVerifier(
            MeetingMediaIdentityConfiguration.from_environment(purpose),
            meeting_media_replay_store(),
        ).verify_service(
            service_token=_required_header(request, _token_header(purpose)),
            assertion=_required_header(request, ASSERTION_HEADER),
            method=request.method,
            path=request.url.path,
            body=body,
        )
    except MeetingMediaIdentityError as error:
        raise _identity_error(error) from error


def _verify_resource(
    request: Request,
    purpose: MeetingMediaPurpose,
    body: bytes,
    tenant_id: int,
    meeting_id: UUID,
    resource_id: UUID,
) -> None:
    try:
        MeetingMediaAssertionVerifier(
            MeetingMediaIdentityConfiguration.from_environment(purpose),
            meeting_media_replay_store(),
        ).verify_resource(
            service_token=_required_header(request, _token_header(purpose)),
            assertion=_required_header(request, ASSERTION_HEADER),
            method=request.method,
            path=request.url.path,
            tenant_id=tenant_id,
            meeting_id=meeting_id,
            resource_id=resource_id,
            body=body,
        )
    except MeetingMediaIdentityError as error:
        raise _identity_error(error) from error


def _identity_error(error: MeetingMediaIdentityError) -> HTTPException:
    unavailable = "configured" in str(error) or "unavailable" in str(error)
    return HTTPException(
        status_code=(
            status.HTTP_503_SERVICE_UNAVAILABLE
            if unavailable
            else status.HTTP_401_UNAUTHORIZED
        ),
        detail=(
            "Meeting media workload identity is unavailable."
            if unavailable
            else "Invalid meeting media workload identity."
        ),
        headers={"Cache-Control": "no-store"},
    )


def _resource_headers(request: Request) -> tuple[int, UUID]:
    return (
        _positive_integer_header(request, "X-DWP-Tenant-ID"),
        _uuid_header(request, "X-DWP-Meeting-ID"),
    )


def _positive_integer_header(request: Request, name: str) -> int:
    try:
        value = int(_required_header(request, name))
    except ValueError:
        value = 0
    if value <= 0:
        raise _invalid_identity()
    return value


def _uuid_header(request: Request, name: str) -> UUID:
    try:
        return UUID(_required_header(request, name))
    except (ValueError, AttributeError):
        raise _invalid_identity() from None


def _correlation_id(request: Request) -> str:
    value = _required_header(request, "X-Correlation-ID")
    if (
        not value
        or len(value) > 160
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _invalid_identity()
    return value


def _idempotency_key(request: Request, expected: str) -> str:
    value = _required_header(request, "Idempotency-Key")
    if value != expected:
        raise _invalid_identity()
    return value


def _parse(body: bytes, model: type[_Model], code: str) -> _Model:
    try:
        return model.model_validate_json(body)
    except (ValidationError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=code,
            headers={"Cache-Control": "no-store"},
        ) from None


def _require_equal(actual: tuple[object, ...], expected: tuple[object, ...]) -> None:
    if actual != expected:
        raise _invalid_identity()


def _token_header(purpose: MeetingMediaPurpose) -> str:
    return (
        RECORDING_TOKEN_HEADER
        if purpose is MeetingMediaPurpose.RECORDING
        else TRANSCRIPT_TOKEN_HEADER
    )


def _required_header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1 or not values[0]:
        raise _invalid_identity()
    return values[0]


def _invalid_identity() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid meeting media workload identity.",
        headers={"Cache-Control": "no-store"},
    )


def _unavailable(code: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=code,
        headers={"Cache-Control": "no-store"},
    )
