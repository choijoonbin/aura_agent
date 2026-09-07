from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .meeting_media_attestation import VerifiedMeetingMediaAttestation
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
from .meeting_media_security import (
    MeetingMediaIdentityConfiguration,
    MeetingMediaPurpose,
    meeting_media_replay_store,
)
from .meeting_media_provider_validation import (
    MeetingMediaConfigurationError,
    MeetingMediaUnavailable,
    recording_capability_matches,
    recording_unavailable,
    transcript_capability_matches,
    transcript_unavailable,
    validate_access_url,
    validate_deletion,
    validate_provider_configuration,
)


RECORDING_CAPABILITY_PATH = "/internal/v1/meeting-recording/capability"
RECORDING_START_PATH = "/internal/v1/meeting-recording/start"
RECORDING_STOP_PATH = "/internal/v1/meeting-recording/stop"
RECORDING_ACCESS_PATH = "/internal/v1/meeting-recording/access-ticket"
RECORDING_DELETE_PATH = "/internal/v1/meeting-recording/delete"
TRANSCRIPT_READ_PATH = "/internal/v1/meeting-transcripts/read"
TRANSCRIPT_RETENTION_PATH = "/internal/v1/meeting-transcripts/retention-capability"
TRANSCRIPT_DELETE_PATH = "/internal/v1/meeting-transcripts/delete"

_Model = TypeVar("_Model", bound=BaseModel)


@dataclass(frozen=True)
class MeetingMediaProviderConfiguration:
    purpose: MeetingMediaPurpose
    enabled: bool
    base_url: str
    allowed_hosts: frozenset[str]
    api_token: str = field(repr=False)
    provider_code: str
    storage_provider_code: str
    processing_region: str
    policy_attestation: str = field(repr=False)
    attestation_public_key_base64: str = field(repr=False)
    attestation_key_id: str
    approved_policy_sha256: str
    access_allowed_hosts: frozenset[str] = frozenset()
    access_path_prefix: str = "/playback/"
    access_ticket_ttl_seconds: int = 120
    timeout_seconds: float = 10.0
    maximum_response_bytes: int = 5_000_000

    @classmethod
    def from_environment(
        cls, purpose: MeetingMediaPurpose
    ) -> "MeetingMediaProviderConfiguration":
        prefix = (
            "DWP_MEETING_RECORDING_BROKER"
            if purpose is MeetingMediaPurpose.RECORDING
            else "DWP_MEETING_TRANSCRIPT_BROKER"
        )
        maximum_default = "1000000" if purpose is MeetingMediaPurpose.RECORDING else "5000000"
        return cls(
            purpose=purpose,
            enabled=os.getenv(f"{prefix}_ENABLED", "false").strip().lower() == "true",
            base_url=os.getenv(f"{prefix}_BASE_URL", "").strip(),
            allowed_hosts=_csv(os.getenv(f"{prefix}_ALLOWED_HOSTS", "")),
            api_token=os.getenv(f"{prefix}_API_TOKEN", "").strip(),
            provider_code=os.getenv(f"{prefix}_PROVIDER_CODE", "").strip(),
            storage_provider_code=os.getenv(
                f"{prefix}_STORAGE_PROVIDER_CODE", ""
            ).strip(),
            processing_region=os.getenv(f"{prefix}_PROCESSING_REGION", "").strip(),
            policy_attestation=os.getenv(f"{prefix}_POLICY_ATTESTATION", "").strip(),
            attestation_public_key_base64=os.getenv(
                f"{prefix}_ATTESTATION_PUBLIC_KEY_BASE64", ""
            ).strip(),
            attestation_key_id=os.getenv(f"{prefix}_ATTESTATION_KEY_ID", "").strip(),
            approved_policy_sha256=os.getenv(
                f"{prefix}_APPROVED_POLICY_SHA256", ""
            ).strip(),
            access_allowed_hosts=_csv(
                os.getenv(f"{prefix}_ACCESS_ALLOWED_HOSTS", "")
            ),
            access_path_prefix=os.getenv(
                f"{prefix}_ACCESS_PATH_PREFIX", "/playback/"
            ).strip(),
            access_ticket_ttl_seconds=_integer_env(
                f"{prefix}_ACCESS_TICKET_TTL_SECONDS", 120
            ),
            timeout_seconds=_float_env(f"{prefix}_TIMEOUT_SECONDS", 10.0),
            maximum_response_bytes=_integer_env(
                f"{prefix}_MAXIMUM_RESPONSE_BYTES", int(maximum_default)
            ),
        )

    def validate(self) -> VerifiedMeetingMediaAttestation:
        return validate_provider_configuration(self)


class MeetingMediaBroker:
    def __init__(
        self,
        recording: MeetingMediaProviderConfiguration | None = None,
        transcript: MeetingMediaProviderConfiguration | None = None,
        *,
        recording_transport: httpx.BaseTransport | None = None,
        transcript_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.recording = recording or MeetingMediaProviderConfiguration.from_environment(
            MeetingMediaPurpose.RECORDING
        )
        self.transcript = transcript or MeetingMediaProviderConfiguration.from_environment(
            MeetingMediaPurpose.TRANSCRIPT
        )
        self.recording_transport = recording_transport
        self.transcript_transport = transcript_transport

    def recording_capability(self) -> RecordingCapability:
        try:
            attestation = self.recording.validate()
            capability = self._request(
                self.recording,
                self.recording_transport,
                "GET",
                RECORDING_CAPABILITY_PATH,
                RecordingCapability,
            )
            if not recording_capability_matches(capability, attestation):
                raise MeetingMediaUnavailable("Recording capability is not ready.")
            return capability
        except (MeetingMediaConfigurationError, MeetingMediaUnavailable):
            return recording_unavailable()

    def recording_command(
        self,
        path: str,
        request: RecordingCommandRequest,
        *,
        correlation_id: str,
        idempotency_key: str,
    ) -> RecordingCommandResponse:
        if path not in {RECORDING_START_PATH, RECORDING_STOP_PATH}:
            raise MeetingMediaUnavailable("Recording command is unavailable.")
        self._require_recording_ready()
        expected_type = "START" if path == RECORDING_START_PATH else "STOP"
        if request.command_type != expected_type:
            raise MeetingMediaUnavailable("Recording command is unavailable.")
        response = self._request(
            self.recording,
            self.recording_transport,
            "POST",
            path,
            RecordingCommandResponse,
            body=_body(request),
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        expected_state = "STARTED" if expected_type == "START" else "STOPPED"
        if (
            response.recording_session_id != request.recording_session_id
            or response.command_state != expected_state
        ):
            raise MeetingMediaUnavailable("Recording command is unavailable.")
        return response

    def recording_access_ticket(
        self,
        request: RecordingAccessTicketRequest,
        *,
        correlation_id: str,
    ) -> RecordingAccessTicketResponse:
        self._require_recording_ready()
        now = datetime.now(UTC)
        if (
            request.expires_no_later_than <= now
            or request.expires_no_later_than
            > now + timedelta(seconds=self.recording.access_ticket_ttl_seconds)
        ):
            raise MeetingMediaUnavailable("Recording access is unavailable.")
        response = self._request(
            self.recording,
            self.recording_transport,
            "POST",
            RECORDING_ACCESS_PATH,
            RecordingAccessTicketResponse,
            body=_body(request),
            correlation_id=correlation_id,
        )
        if (
            response.artifact_id != request.artifact_id
            or response.requester_user_id != request.requester_user_id
            or response.artifact_version != request.artifact_version
            or not hmac.compare_digest(response.source_sha256, request.source_sha256)
            or response.expires_at <= now
            or response.expires_at > request.expires_no_later_than
            or response.expires_at
            > now + timedelta(seconds=self.recording.access_ticket_ttl_seconds)
        ):
            raise MeetingMediaUnavailable("Recording access is unavailable.")
        validate_access_url(response.access_url, request, self.recording)
        return response

    def recording_delete(
        self,
        request: RecordingDeleteRequest,
        *,
        correlation_id: str,
        idempotency_key: str,
    ) -> RecordingDeleteResponse:
        self._require_recording_ready()
        response = self._request(
            self.recording,
            self.recording_transport,
            "POST",
            RECORDING_DELETE_PATH,
            RecordingDeleteResponse,
            body=_body(request),
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        validate_deletion(response, request)
        return response

    def transcript_retention_capability(self) -> TranscriptRetentionCapability:
        try:
            attestation = self.transcript.validate()
            capability = self._request(
                self.transcript,
                self.transcript_transport,
                "GET",
                TRANSCRIPT_RETENTION_PATH,
                TranscriptRetentionCapability,
            )
            if not transcript_capability_matches(capability, attestation):
                raise MeetingMediaUnavailable("Transcript retention is not ready.")
            return capability
        except (MeetingMediaConfigurationError, MeetingMediaUnavailable):
            return transcript_unavailable()

    def transcript_read(
        self,
        request: TranscriptReadRequest,
        *,
        correlation_id: str,
    ) -> TranscriptReadResponse:
        self._require_transcript_ready()
        response = self._request(
            self.transcript,
            self.transcript_transport,
            "POST",
            TRANSCRIPT_READ_PATH,
            TranscriptReadResponse,
            body=_body(request),
            correlation_id=correlation_id,
        )
        if not hmac.compare_digest(response.source_sha256, request.source_sha256):
            raise MeetingMediaUnavailable("Transcript read is unavailable.")
        return response

    def transcript_delete(
        self,
        request: TranscriptDeleteRequest,
        *,
        correlation_id: str,
        idempotency_key: str,
    ) -> TranscriptDeleteResponse:
        self._require_transcript_ready()
        response = self._request(
            self.transcript,
            self.transcript_transport,
            "POST",
            TRANSCRIPT_DELETE_PATH,
            TranscriptDeleteResponse,
            body=_body(request),
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        validate_deletion(response, request)
        return response

    def _require_recording_ready(self) -> None:
        if not self.recording_capability().available:
            raise MeetingMediaUnavailable("Recording broker is unavailable.")

    def _require_transcript_ready(self) -> None:
        if not self.transcript_retention_capability().available:
            raise MeetingMediaUnavailable("Transcript broker is unavailable.")

    def _request(
        self,
        configuration: MeetingMediaProviderConfiguration,
        transport: httpx.BaseTransport | None,
        method: str,
        path: str,
        response_type: type[_Model],
        *,
        body: bytes | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> _Model:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {configuration.api_token}",
            "X-DWP-Managed-Media-Purpose": configuration.purpose.value,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if correlation_id:
            headers["X-Correlation-ID"] = correlation_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            with httpx.Client(
                transport=transport,
                timeout=configuration.timeout_seconds,
                follow_redirects=False,
            ) as client:
                with client.stream(
                    method,
                    configuration.base_url.rstrip("/") + path,
                    headers=headers,
                    content=body,
                ) as response:
                    if response.status_code != 200:
                        raise MeetingMediaUnavailable("Managed media broker is unavailable.")
                    content_type = response.headers.get("content-type", "").lower()
                    if not content_type.startswith("application/json"):
                        raise MeetingMediaUnavailable("Managed media broker is unavailable.")
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        try:
                            declared_length = int(declared)
                        except ValueError as error:
                            raise MeetingMediaUnavailable(
                                "Managed media broker is unavailable."
                            ) from error
                        if not 0 <= declared_length <= configuration.maximum_response_bytes:
                            raise MeetingMediaUnavailable(
                                "Managed media broker is unavailable."
                            )
                    chunks: list[bytes] = []
                    received = 0
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > configuration.maximum_response_bytes:
                            raise MeetingMediaUnavailable(
                                "Managed media broker is unavailable."
                            )
                        chunks.append(chunk)
            return response_type.model_validate_json(b"".join(chunks))
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            raise MeetingMediaUnavailable("Managed media broker is unavailable.") from error


def validate_meeting_media_runtime_configuration() -> None:
    for purpose in MeetingMediaPurpose:
        configuration = MeetingMediaProviderConfiguration.from_environment(purpose)
        if configuration.enabled:
            configuration.validate()
            MeetingMediaIdentityConfiguration.from_environment(purpose)
            meeting_media_replay_store()


def _body(model: BaseModel) -> bytes:
    return model.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")


def _csv(value: str) -> frozenset[str]:
    return frozenset(item.strip().lower() for item in value.split(",") if item.strip())


def _integer_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError as error:
        raise MeetingMediaConfigurationError(f"{name} is invalid.") from error


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except ValueError as error:
        raise MeetingMediaConfigurationError(f"{name} is invalid.") from error
