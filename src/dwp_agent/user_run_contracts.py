from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from .contracts import AskState, ContractModel, PolicyOutcome, RiskTier


class AgentRunState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class UserAgentRunSummary(ContractModel):
    run_id: UUID
    agent_key: str = Field(min_length=1, max_length=100)
    agent_revision: int = Field(ge=0)
    run_state: AgentRunState
    answer_state: AskState | None = None
    risk_tier: RiskTier
    policy_outcome: PolicyOutcome
    status_code: str | None = Field(default=None, max_length=128)
    source_count: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    conversation_id: UUID | None = None
    created_at: datetime
    completed_at: datetime | None = None


class UserAgentRunListEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Agent activity loaded."
    success: bool = True
    data: list[UserAgentRunSummary]


class UserAgentRunEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Agent run loaded."
    success: bool = True
    data: UserAgentRunSummary
