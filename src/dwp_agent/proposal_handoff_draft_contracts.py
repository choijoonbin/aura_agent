from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, JsonValue

from .contract_model import ContractModel


class SaveProposalHandoffDraftRequest(ContractModel):
    command_id: UUID
    expected_version: int = Field(ge=1)
    reviewed_inputs: dict[str, JsonValue] = Field(default_factory=dict, max_length=20)


class ProposalHandoffDraft(ContractModel):
    draft_id: UUID
    handoff_id: UUID
    proposal_id: UUID
    handoff_version: int = Field(ge=1)
    revision: int = Field(ge=1)
    reviewed_inputs: dict[str, JsonValue]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    saved_at: datetime


class ProposalHandoffDraftEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Proposal handoff draft loaded."
    success: bool = True
    data: ProposalHandoffDraft | None
