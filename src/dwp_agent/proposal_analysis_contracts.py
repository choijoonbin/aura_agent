from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from .contracts import ContractModel
from .proposal_contracts import AgentProposal


class AnalyzeProposalsRequest(ContractModel):
    command_id: UUID


class ProposalAnalysisReceipt(ContractModel):
    analyzed_at: datetime
    sources_analyzed: int = Field(ge=0, le=12)
    actionable_proposals: int = Field(ge=0, le=6)
    attempted_sources: list[str] = Field(max_length=3)
    unavailable_sources: list[str] = Field(max_length=3)
    proposals: list[AgentProposal] = Field(max_length=6)


class ProposalAnalysisEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Workspace signals analyzed."
    success: bool = True
    data: ProposalAnalysisReceipt


class ProposalAnalysisPreference(ContractModel):
    proactive_analysis_enabled: bool
    revision: int = Field(ge=0)
    updated_at: datetime | None = None


class UpdateProposalAnalysisPreferenceRequest(ContractModel):
    command_id: UUID
    expected_revision: int = Field(ge=0)
    proactive_analysis_enabled: bool


class ProposalAnalysisPreferenceEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Proposal analysis preference loaded."
    success: bool = True
    data: ProposalAnalysisPreference


class ClearProposalInboxRequest(ContractModel):
    command_id: UUID


class ClearProposalInboxReceipt(ContractModel):
    hidden_count: int = Field(ge=0)
    cleared_at: datetime


class ClearProposalInboxEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Proposal inbox cleared."
    success: bool = True
    data: ClearProposalInboxReceipt
