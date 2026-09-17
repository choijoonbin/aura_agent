from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, model_validator

from .contract_model import ContractModel
from .dwaion_workflow_contracts import (
    AttachmentCitation,
    AttachmentStage,
    AttachmentStageKey,
    AttachmentStageState,
    AttachmentState,
)


class AttachmentEvidenceEvent(ContractModel):
    event_id: UUID
    event_type: str = Field(min_length=1, max_length=80)
    previous_state: AttachmentState | None = None
    current_state: AttachmentState
    revision: int = Field(ge=1)
    safe_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    occurred_at: datetime


class AttachmentEvidence(ContractModel):
    attachment_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    deletion_attempt_count: int = Field(default=0, ge=0)
    deletion_last_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    deletion_receipt_id: str | None = Field(default=None, min_length=1, max_length=240)
    stages: list[AttachmentStage]
    citations: list[AttachmentCitation]
    inspection_log: list[AttachmentEvidenceEvent]
    masking_history: list[AttachmentEvidenceEvent]
    ocr_evidence: list[AttachmentCitation]

    @model_validator(mode="after")
    def verify_evidence_closure(self) -> "AttachmentEvidence":
        if len({stage.key for stage in self.stages}) != len(self.stages):
            raise ValueError("Attachment evidence stages must be unique.")
        citation_ids = [citation.citation_id for citation in self.citations]
        event_ids = [event.event_id for event in self.inspection_log]
        if len(citation_ids) != len(set(citation_ids)) or len(event_ids) != len(set(event_ids)):
            raise ValueError("Attachment evidence IDs must be unique.")
        if self.inspection_log != sorted(
            self.inspection_log, key=lambda event: (event.occurred_at, str(event.event_id))
        ):
            raise ValueError("Attachment inspection events must be chronological.")
        known_events = set(event_ids)
        if not {event.event_id for event in self.masking_history}.issubset(known_events):
            raise ValueError("Masking history must be bound to the inspection log.")
        ocr_passed = any(
            stage.key == AttachmentStageKey.OCR
            and stage.state == AttachmentStageState.PASSED
            for stage in self.stages
        )
        if self.ocr_evidence and not ocr_passed:
            raise ValueError("OCR evidence requires a passed OCR stage.")
        if not {item.citation_id for item in self.ocr_evidence}.issubset(set(citation_ids)):
            raise ValueError("OCR evidence must be bound to the citation manifest.")
        return self


class AttachmentEvidenceEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Secure attachment evidence loaded."
    success: bool = True
    data: AttachmentEvidence
