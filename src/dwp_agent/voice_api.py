from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response, status

from .delivery_gate import (
    DeliveryCapability,
    OperationalDeliveryConfigurationError,
    OperationalDeliveryNotReady,
    require_delivery_capability,
)
from .policy import AskIdentity
from .security import header_values, require_gateway_service, verified_ask_identity
from .voice_contracts import (
    VoiceSpeechRequest,
    VoiceTranscription,
    VoiceTranscriptionEnvelope,
)
from .voice_provider import VoiceProvider, VoiceProviderUnavailable


MAX_VOICE_BYTES = 4 * 1024 * 1024
SUPPORTED_AUDIO_TYPES = frozenset(
    {"audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "audio/x-wav"}
)
VOICE_LOCALE_PATTERN = r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"

router = APIRouter(
    prefix="/v1/voice",
    tags=["voice"],
    dependencies=[Depends(require_gateway_service)],
)


def get_voice_provider() -> VoiceProvider:
    return VoiceProvider()


def require_voice_access(
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> None:
    if "APP.ASK:VIEW" not in header_values(permissions):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="DWAI-ON access is required.",
        )


def require_voice_delivery(identity: AskIdentity) -> None:
    try:
        require_delivery_capability(
            tenant_id=identity.tenant_id,
            capability=DeliveryCapability.ASK,
        )
    except (OperationalDeliveryConfigurationError, OperationalDeliveryNotReady) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DWAI-ON voice is not approved for this environment.",
        ) from error


@router.post(
    "/transcriptions",
    response_model=VoiceTranscriptionEnvelope,
    response_model_by_alias=True,
    dependencies=[Depends(require_voice_access)],
)
def transcribe_voice(
    audio: Annotated[
        bytes,
        Body(
            media_type="application/octet-stream",
            min_length=1,
            max_length=MAX_VOICE_BYTES,
            description="Ephemeral voice recording. The Agent runtime does not persist this body.",
        ),
    ],
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    content_type: Annotated[str, Header(alias="Content-Type")],
    locale: Annotated[
        str,
        Header(
            alias="X-DWP-Voice-Locale",
            min_length=2,
            max_length=40,
            pattern=VOICE_LOCALE_PATTERN,
        ),
    ],
    response: Response,
    provider: VoiceProvider = Depends(get_voice_provider),
) -> VoiceTranscriptionEnvelope:
    require_voice_delivery(identity)
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in SUPPORTED_AUDIO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="The voice recording format is not supported.",
        )
    try:
        text = provider.transcribe(audio, media_type, locale)
    except VoiceProviderUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return VoiceTranscriptionEnvelope(
        data=VoiceTranscription(text=text, language=locale)
    )


@router.post(
    "/speech",
    response_class=Response,
    responses={200: {"content": {"audio/mpeg": {}}, "description": "Synthesized speech."}},
    dependencies=[Depends(require_voice_access)],
)
def synthesize_voice(
    request: VoiceSpeechRequest,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    provider: VoiceProvider = Depends(get_voice_provider),
) -> Response:
    require_voice_delivery(identity)
    try:
        audio = provider.synthesize(request.text)
    except VoiceProviderUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store", "Content-Disposition": "inline"},
    )
