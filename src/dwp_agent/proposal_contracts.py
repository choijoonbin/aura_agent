from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from .contracts import ContractModel


class ProposalKind(StrEnum):
    WORK_SIGNAL = "WORK_SIGNAL"
    RISK = "RISK"
    SCHEDULE = "SCHEDULE"
    APPROVAL = "APPROVAL"
    INSIGHT = "INSIGHT"


class ProposalPriority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class ProposalState(StrEnum):
    PENDING = "PENDING"
    SNOOZED = "SNOOZED"
    ACCEPTED = "ACCEPTED"
    DISMISSED = "DISMISSED"
    EXPIRED = "EXPIRED"


class ProposalDecision(StrEnum):
    ACCEPT = "ACCEPT"
    SNOOZE = "SNOOZE"
    DISMISS = "DISMISS"


class ProposalInboxView(StrEnum):
    ACTIVE = "ACTIVE"
    SNOOZED = "SNOOZED"
    HANDLED = "HANDLED"
    ALL = "ALL"


class ProposalEvidence(ContractModel):
    source_type: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{0,63}$")
    reference_id: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=240)
    occurred_at: datetime | None = None


class ProposalContent(ContractModel):
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=1_000)
    rationale: str = Field(min_length=1, max_length=2_000)
    action_inputs: dict[str, JsonValue] = Field(default_factory=dict, max_length=20)
    evidence: list[ProposalEvidence] = Field(default_factory=list, max_length=20)


class AgentProposal(ContractModel):
    proposal_id: UUID
    kind: ProposalKind
    priority: ProposalPriority
    state: ProposalState
    revision: int = Field(ge=1)
    agent_key: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{0,99}$")
    action_key: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{0,127}$"
    )
    content: ProposalContent
    proposed_at: datetime
    available_at: datetime
    expires_at: datetime
    snoozed_until: datetime | None = None
    decided_at: datetime | None = None


class ProposalInboxSummary(ContractModel):
    active: int = Field(ge=0)
    high_priority: int = Field(ge=0)
    snoozed: int = Field(ge=0)
    handled: int = Field(ge=0)


class ProposalInboxPage(ContractModel):
    items: list[AgentProposal]
    summary: ProposalInboxSummary
    next_cursor: str | None = None


class ProposalInboxEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Agent proposals loaded."
    success: bool = True
    data: ProposalInboxPage


class CreateAgentProposalRequest(ContractModel):
    command_id: UUID
    target_user_id: str = Field(min_length=1, max_length=160)
    source_event_id: str = Field(min_length=1, max_length=160)
    kind: ProposalKind
    priority: ProposalPriority
    agent_key: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{0,99}$")
    action_key: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{0,127}$"
    )
    content: ProposalContent
    available_at: datetime | None = None
    expires_at: datetime
    change_reason: str = Field(min_length=10, max_length=500)


class AgentProposalEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Agent proposal loaded."
    success: bool = True
    data: AgentProposal


class DecideAgentProposalRequest(ContractModel):
    command_id: UUID
    expected_revision: int = Field(ge=1)
    decision: ProposalDecision
    snooze_until: datetime | None = None
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_decision(self) -> "DecideAgentProposalRequest":
        if self.decision == ProposalDecision.SNOOZE and self.snooze_until is None:
            raise ValueError("snoozeUntil is required for SNOOZE.")
        if self.decision != ProposalDecision.SNOOZE and self.snooze_until is not None:
            raise ValueError("snoozeUntil is only allowed for SNOOZE.")
        return self


class ProposalDecisionReceipt(ContractModel):
    proposal: AgentProposal
    action_review_required: bool


class ProposalDecisionEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Agent proposal decision recorded."
    success: bool = True
    data: ProposalDecisionReceipt
