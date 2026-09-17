from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .contract_model import ContractModel


class AttachmentRevisionBinding(ContractModel):
    attachment_id: UUID
    expected_revision: int = Field(ge=1)


class DetachAllAttachmentsRequest(ContractModel):
    command_id: UUID
    idempotency_key: UUID
    attachments: list[AttachmentRevisionBinding] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=5, max_length=500)

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 5:
            raise ValueError("Attachment detach reason must contain at least 5 characters.")
        return normalized

    @model_validator(mode="after")
    def unique_attachments(self) -> "DetachAllAttachmentsRequest":
        identifiers = [item.attachment_id for item in self.attachments]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Attachment bindings must be unique.")
        return self


class DetachedAttachment(ContractModel):
    attachment_id: UUID
    revision: int = Field(ge=2)


class AttachmentDetachReceipt(ContractModel):
    receipt_id: UUID
    command_id: UUID
    conversation_id: UUID
    detached_attachments: list[DetachedAttachment] = Field(min_length=1, max_length=20)
    integrity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    detached_at: datetime


class AttachmentDetachReceiptEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Secure attachments detached from the conversation."
    success: bool = True
    data: AttachmentDetachReceipt


class CreateAttachmentAuditReportRequest(ContractModel):
    command_id: UUID
    idempotency_key: UUID
    attachments: list[AttachmentRevisionBinding] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=5, max_length=500)

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 5:
            raise ValueError("Attachment audit reason must contain at least 5 characters.")
        return normalized

    @model_validator(mode="after")
    def unique_attachments(self) -> "CreateAttachmentAuditReportRequest":
        identifiers = [item.attachment_id for item in self.attachments]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Attachment bindings must be unique.")
        return self


class AttachmentAuditReportReceipt(ContractModel):
    report_id: UUID
    command_id: UUID
    conversation_id: UUID
    attachment_ids: list[UUID] = Field(min_length=1, max_length=20)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature_algorithm: str = "HMAC-SHA256"
    signature: str = Field(min_length=1, max_length=512)
    signing_key_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    download_path: str = Field(
        pattern=r"^/v1/attachments/audit-reports/[0-9a-f-]+/download$"
    )
    created_at: datetime


class AttachmentAuditReportReceiptEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Signed secure attachment audit report generated."
    success: bool = True
    data: AttachmentAuditReportReceipt
