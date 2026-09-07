from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator


def _camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class _Contract(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


SafeCode = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ObjectKey = Annotated[str, StringConstraints(min_length=1, max_length=1_000)]


class RecordingCapability(_Contract):
    schema_version: Literal["meeting-recording-capability-v1"] = (
        "meeting-recording-capability-v1"
    )
    available: bool
    egress_available: bool
    storage_available: bool
    speech_to_text_available: bool
    deletion_available: bool
    crypto_shred_available: bool
    orphan_cleanup_available: bool
    maximum_orphan_ttl_seconds: int = Field(ge=0, le=3_600)
    legacy_locator_deletion_available: bool
    customer_managed_storage: bool
    provider_retention_disabled: bool
    processing_region: str = Field(min_length=1, max_length=32)
    provider_code: SafeCode = Field(max_length=48)


class RecordingCommandRequest(_Contract):
    schema_version: Literal["meeting-recording-command-v1"]
    command_type: Literal["START", "STOP"]
    tenant_id: int = Field(gt=0)
    meeting_id: UUID
    recording_session_id: UUID
    plan_version: int = Field(ge=0)
    notice_id: UUID
    provider_room_name: str = Field(min_length=1, max_length=180)


class RecordingCommandResponse(_Contract):
    schema_version: Literal["meeting-recording-command-v1"]
    recording_session_id: UUID
    command_state: Literal["STARTED", "STOPPED"]
    provider_command_id: SafeCode = Field(min_length=3, max_length=160)


class RecordingAccessTicketRequest(_Contract):
    schema_version: Literal["meeting-recording-access-ticket-v1"]
    tenant_id: int = Field(gt=0)
    meeting_id: UUID
    artifact_id: UUID
    requester_user_id: int = Field(gt=0)
    storage_provider: SafeCode = Field(max_length=32)
    object_key: ObjectKey
    content_type: str = Field(min_length=1, max_length=120)
    source_sha256: Sha256
    artifact_version: int = Field(ge=0)
    expires_no_later_than: AwareDatetime

    @model_validator(mode="after")
    def validate_locator(self) -> "RecordingAccessTicketRequest":
        _require_opaque_locator(self.object_key)
        return self


class RecordingAccessTicketResponse(_Contract):
    schema_version: Literal["meeting-recording-access-ticket-v1"]
    artifact_id: UUID
    requester_user_id: int = Field(gt=0)
    artifact_version: int = Field(ge=0)
    source_sha256: Sha256
    access_url: str = Field(min_length=1, max_length=8_192)
    expires_at: AwareDatetime


class _DeleteRequest(_Contract):
    tenant_id: int = Field(gt=0)
    meeting_id: UUID
    artifact_id: UUID
    storage_provider: SafeCode = Field(max_length=32)
    object_key: ObjectKey
    deletion_binding_sha256: Sha256
    artifact_version: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_locator(self) -> "_DeleteRequest":
        _require_opaque_locator(self.object_key)
        return self


class RecordingDeleteRequest(_DeleteRequest):
    schema_version: Literal["meeting-recording-delete-v1"]


class TranscriptDeleteRequest(_DeleteRequest):
    schema_version: Literal["meeting-transcript-delete-v1"]


class _DeleteResponse(_Contract):
    artifact_id: UUID
    artifact_version: int = Field(ge=0)
    deletion_binding_sha256: Sha256
    deletion_state: Literal["DELETED"]
    crypto_shredded: Literal[True]
    provider_deletion_id: SafeCode = Field(max_length=160)
    deleted_at: AwareDatetime


class RecordingDeleteResponse(_DeleteResponse):
    schema_version: Literal["meeting-recording-delete-v1"]


class TranscriptDeleteResponse(_DeleteResponse):
    schema_version: Literal["meeting-transcript-delete-v1"]


class TranscriptReadRequest(_Contract):
    schema_version: Literal["meeting-transcript-read-v1"]
    tenant_id: int = Field(gt=0)
    meeting_id: UUID
    run_id: UUID
    artifact_id: UUID
    source_sha256: Sha256


class TranscriptSegment(_Contract):
    segment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    start_millis: int = Field(ge=0)
    end_millis: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def validate_range(self) -> "TranscriptSegment":
        if self.end_millis <= self.start_millis:
            raise ValueError("Transcript segment range is invalid.")
        return self


class TranscriptReadResponse(_Contract):
    schema_version: Literal["meeting-transcript-v1"]
    source_sha256: Sha256
    segments: list[TranscriptSegment] = Field(max_length=500)

    @model_validator(mode="after")
    def validate_segments(self) -> "TranscriptReadResponse":
        identifiers = [segment.segment_id for segment in self.segments]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Transcript segment identifiers must be unique.")
        if sum(len(segment.text) for segment in self.segments) > 300_000:
            raise ValueError("Transcript exceeds the governed response limit.")
        if any(
            current.start_millis < previous.start_millis
            for previous, current in zip(self.segments, self.segments[1:])
        ):
            raise ValueError("Transcript segments must be time ordered.")
        return self


class TranscriptRetentionCapability(_Contract):
    schema_version: Literal["meeting-transcript-retention-capability-v1"] = (
        "meeting-transcript-retention-capability-v1"
    )
    available: bool
    deletion_available: bool
    crypto_shred_available: bool
    customer_managed_storage: bool
    provider_retention_disabled: bool
    orphan_cleanup_available: bool
    maximum_orphan_ttl_seconds: int = Field(ge=0, le=3_600)
    legacy_locator_deletion_available: bool
    provider_code: SafeCode = Field(max_length=48)
    storage_provider_code: SafeCode = Field(max_length=32)
    processing_region: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$"
    )


def _require_opaque_locator(value: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Storage locator is invalid.")
    if "://" in value or "?" in value or "#" in value:
        raise ValueError("Storage locator is invalid.")
