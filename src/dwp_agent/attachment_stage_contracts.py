from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .contract_model import ContractModel
from .secure_attachment_contracts import (
    AttachmentCitation,
    AttachmentStageKey,
)


class AttachmentStageVerdict(StrEnum):
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class AttachmentStageReceiptPayload(ContractModel):
    """Canonical provider assertion for one attachment processing stage."""

    schema_version: int = Field(default=1, ge=1, le=1)
    attachment_id: UUID
    upload_reference: str = Field(min_length=8, max_length=1_000)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: AttachmentStageKey
    provider_receipt_id: str = Field(min_length=1, max_length=240)
    observed_at: datetime
    verdict: AttachmentStageVerdict
    provider_code: str = Field(min_length=1, max_length=80)
    citations: list[AttachmentCitation] = Field(default_factory=list, max_length=2_000)
    safe_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    recovery_hint: str | None = Field(default=None, max_length=500)

    @field_validator(
        "upload_reference", "provider_receipt_id", "provider_code", "recovery_hint"
    )
    @classmethod
    def non_blank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Attachment stage receipt text must not be blank.")
        return normalized

    @field_validator("observed_at")
    @classmethod
    def aware_observation(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Attachment stage observedAt must include a timezone.")
        return value

    @model_validator(mode="after")
    def validate_stage_result(self) -> "AttachmentStageReceiptPayload":
        if self.stage == AttachmentStageKey.UPLOAD:
            raise ValueError("Upload verification is not an attachment stage receipt.")
        if len({item.citation_id for item in self.citations}) != len(self.citations):
            raise ValueError("Attachment stage citation IDs must be unique.")
        if self.stage in {AttachmentStageKey.AV, AttachmentStageKey.DLP} and self.citations:
            raise ValueError("AV and DLP receipts cannot publish content citations.")
        if self.verdict == AttachmentStageVerdict.PASSED:
            if self.safe_error_code is not None or self.recovery_hint is not None:
                raise ValueError("A passed attachment stage cannot carry failure metadata.")
        elif self.safe_error_code is None or self.recovery_hint is None:
            raise ValueError("A blocked or failed attachment stage requires recovery metadata.")
        return self

    def canonical_digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class AttachmentStageProviderReceipt(AttachmentStageReceiptPayload):
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def verify_result_digest(self) -> "AttachmentStageProviderReceipt":
        payload = AttachmentStageReceiptPayload.model_validate(
            self.model_dump(mode="json", by_alias=True, exclude={"result_digest"})
        )
        if self.result_digest != payload.canonical_digest():
            raise ValueError("Attachment stage resultDigest does not match the receipt payload.")
        return self


class AttachmentWorkerObservation(ContractModel):
    command_id: UUID
    expected_revision: int = Field(ge=1)
    receipts: list[AttachmentStageProviderReceipt] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def unique_stages(self) -> "AttachmentWorkerObservation":
        if len({receipt.stage for receipt in self.receipts}) != len(self.receipts):
            raise ValueError("Attachment stage receipt keys must be unique.")
        if len({receipt.provider_receipt_id for receipt in self.receipts}) != len(
            self.receipts
        ):
            raise ValueError("Attachment provider receipt IDs must be unique.")
        if len({receipt.result_digest for receipt in self.receipts}) != len(self.receipts):
            raise ValueError("Attachment stage result digests must be unique.")
        return self


def build_attachment_stage_receipt(
    payload: AttachmentStageReceiptPayload,
) -> AttachmentStageProviderReceipt:
    return AttachmentStageProviderReceipt.model_validate(
        {
            **payload.model_dump(mode="json", by_alias=True),
            "resultDigest": payload.canonical_digest(),
        }
    )
