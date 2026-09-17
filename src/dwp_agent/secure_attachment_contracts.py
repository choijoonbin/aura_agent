from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .contract_model import ContractModel
from .workflow_capability_contracts import WorkflowCapability


class AttachmentState(StrEnum):
    UPLOADING = "UPLOADING"
    SCANNING = "SCANNING"
    READY = "READY"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DELETION_PENDING = "DELETION_PENDING"
    DELETED = "DELETED"

class AttachmentStageKey(StrEnum):
    UPLOAD = "UPLOAD"
    AV = "AV"
    DLP = "DLP"
    PARSER = "PARSER"
    OCR = "OCR"
    INDEX = "INDEX"

class AttachmentStageState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    NOT_REQUIRED = "NOT_REQUIRED"
    NOT_CONFIGURED = "NOT_CONFIGURED"

class AttachmentStage(ContractModel):
    key: AttachmentStageKey
    state: AttachmentStageState
    provider_code: str | None = Field(default=None, max_length=80)
    provider_receipt_id: str | None = Field(default=None, min_length=1, max_length=240)
    result_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime | None = None
    safe_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    recovery_hint: str | None = Field(default=None, max_length=500)

    @field_validator("provider_code", "provider_receipt_id", "recovery_hint")
    @classmethod
    def non_blank_stage_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Attachment stage metadata must not be blank.")
        return normalized

    @model_validator(mode="after")
    def verified_terminal_stage(self) -> "AttachmentStage":
        receipt_fields = (
            self.provider_code,
            self.provider_receipt_id,
            self.result_digest,
            self.observed_at,
        )
        if (
            self.key != AttachmentStageKey.UPLOAD
            and any(value is not None for value in receipt_fields[1:3])
            and not all(receipt_fields)
        ):
            raise ValueError(
                "Attachment provider receipt metadata must be complete."
            )
        return self

class AttachmentCitation(ContractModel):
    citation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
    locator: str = Field(min_length=1, max_length=500)
    label: str = Field(min_length=1, max_length=240)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def verified_content_hash(self) -> "AttachmentCitation":
        if hashlib.sha256(self.evidence.encode("utf-8")).hexdigest() != self.content_sha256:
            raise ValueError("Attachment citation contentSha256 does not match its evidence.")
        return self

class AttachmentCapabilities(ContractModel):
    upload: WorkflowCapability
    antivirus: WorkflowCapability
    dlp: WorkflowCapability
    parser: WorkflowCapability
    ocr: WorkflowCapability
    index: WorkflowCapability
    deletion: WorkflowCapability
    detach_all: WorkflowCapability
    inspection_log: WorkflowCapability
    masking_history: WorkflowCapability
    ocr_viewer: WorkflowCapability
    signed_audit_report: WorkflowCapability
    maximum_file_bytes: int = Field(ge=1)
    allowed_media_types: list[str]

class AttachmentCapabilitiesEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Secure attachment capabilities loaded."
    success: bool = True
    data: AttachmentCapabilities

class AttachmentUploadTicket(ContractModel):
    method: str = "PUT"
    upload_url: str = Field(min_length=8, max_length=8_192)
    upload_reference: str = Field(min_length=8, max_length=1_000)
    expires_at: datetime

class CreateAttachmentRequest(ContractModel):
    command_id: UUID
    expected_revision: int = Field(default=0, ge=0, le=0)
    conversation_id: UUID | None = None
    file_name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=3, max_length=120)
    size_bytes: int = Field(ge=1, le=104_857_600)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retention_hours: int = Field(default=24, ge=1, le=720)

    @field_validator("file_name")
    @classmethod
    def safe_file_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(part in normalized for part in ("/", "\\", "\x00")):
            raise ValueError("Attachment fileName must be a leaf name.")
        return normalized

class CompleteAttachmentUploadRequest(ContractModel):
    command_id: UUID
    expected_revision: int = Field(ge=1)
    upload_reference: str = Field(min_length=8, max_length=1_000)
    observed_size_bytes: int = Field(ge=1, le=104_857_600)
    observed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("upload_reference")
    @classmethod
    def opaque_reference(cls, value: str) -> str:
        normalized = value.strip()
        if "://" in normalized or "?" in normalized or "#" in normalized:
            raise ValueError("Upload references must be opaque provider identifiers.")
        return normalized

class DeleteAttachmentRequest(ContractModel):
    command_id: UUID
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=5, max_length=500)

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 5:
            raise ValueError("Attachment deletion reason must contain at least 5 characters.")
        return normalized

class SecureAttachment(ContractModel):
    attachment_id: UUID
    conversation_id: UUID | None = None
    file_name: str
    media_type: str
    size_bytes: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1)
    state: AttachmentState
    stages: list[AttachmentStage]
    citations: list[AttachmentCitation] = Field(default_factory=list)
    retention_expires_at: datetime
    capabilities: AttachmentCapabilities
    upload_ticket: AttachmentUploadTicket | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    deletion_attempt_count: int = Field(default=0, ge=0)
    deletion_last_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    deletion_receipt_id: str | None = Field(default=None, min_length=1, max_length=240)


class AttachmentEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Secure attachment loaded."
    success: bool = True
    data: SecureAttachment


class AttachmentListEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Secure attachments loaded."
    success: bool = True
    data: list[SecureAttachment]
