from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .contract_model import ContractModel


class _ProposalHandoffReceipt(ContractModel):
    handoff_id: UUID
    proposal_id: UUID
    handoff_version: int = Field(ge=1)
    committed_at: datetime
    correlation_id: str = Field(min_length=1, max_length=160)

    @field_validator("correlation_id")
    @classmethod
    def canonical_correlation_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or normalized != value or any(
            character in normalized for character in ("\r", "\n", "\x00")
        ):
            raise ValueError("Proposal receipt correlationId must be canonical.")
        return normalized

    @model_validator(mode="after")
    def committed_at_is_timezone_aware(self) -> "_ProposalHandoffReceipt":
        if self.committed_at.tzinfo is None or self.committed_at.utcoffset() is None:
            raise ValueError("Proposal receipt committedAt must include a timezone.")
        return self


class ApprovalProposalHandoffReceipt(_ProposalHandoffReceipt):
    domain: Literal["APPROVAL"]
    operation: Literal["REQUEST_SUBMIT"]
    action_key: Literal["APPROVAL.REQUEST.CREATE"]
    request_id: UUID
    request_version: int = Field(ge=1)
    status: Literal["IN_REVIEW"]


class CalendarProposalHandoffReceipt(_ProposalHandoffReceipt):
    domain: Literal["CALENDAR"]
    operation: Literal["EVENT_CREATE"]
    action_key: Literal["CALENDAR.EVENT.CREATE"]
    event_id: UUID
    event_version: int = Field(ge=0)
    status: Literal["CONFIRMED", "TENTATIVE"]


class MailProposalHandoffReceipt(_ProposalHandoffReceipt):
    domain: Literal["MAIL"]
    operation: Literal["DRAFT_CREATE"]
    action_key: Literal["MAIL.DRAFT.CREATE"]
    thread_id: UUID
    thread_version: int = Field(ge=0)
    status: Literal["DRAFT"]


class ServiceProposalHandoffReceipt(_ProposalHandoffReceipt):
    domain: Literal["SERVICE"]
    operation: Literal["REQUEST_CREATE"]
    action_key: Literal["SERVICE.REQUEST.CREATE"]
    request_id: UUID
    request_version: int = Field(ge=0)
    status: Literal["DRAFT", "SUBMITTED"]


ProposalHandoffCompletionReceipt = Annotated[
    ApprovalProposalHandoffReceipt
    | CalendarProposalHandoffReceipt
    | MailProposalHandoffReceipt
    | ServiceProposalHandoffReceipt,
    Field(discriminator="domain"),
]
