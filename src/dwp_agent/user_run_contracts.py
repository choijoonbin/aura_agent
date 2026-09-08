from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from .contracts import AskState, CitationSourceType, ContractModel, PolicyOutcome, RiskTier
from .run_observability import (
    RunDataProvenance,
    RunSourceHealthStatus,
    RunStageKey,
    RunStageState,
)


class AgentRunState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RunLeaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    RELEASED = "RELEASED"


class RunMeasurementStatus(StrEnum):
    MEASURING = "MEASURING"
    MEASURED = "MEASURED"
    PARTIAL = "PARTIAL"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class RunAuditEvidenceStatus(StrEnum):
    LINKED = "LINKED"
    PENDING = "PENDING"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class UserAgentRunLease(ContractModel):
    status: RunLeaseStatus
    expires_at: datetime | None = None


class UserAgentRunStage(ContractModel):
    key: RunStageKey
    state: RunStageState
    sequence: int = Field(ge=10, le=60)
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)


class UserAgentRunAuditEvidence(ContractModel):
    audit_id: str | None = Field(default=None, max_length=128)
    audit_record_id: UUID | None = None
    status: RunAuditEvidenceStatus = RunAuditEvidenceStatus.NOT_AVAILABLE


class UserAgentRunSourceHealth(ContractModel):
    source_type: CitationSourceType
    status: RunSourceHealthStatus
    latency_ms: int | None = Field(default=None, ge=0)
    last_attempt_at: datetime
    last_success_at: datetime | None = None


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
    activity_title: str = Field(default="DWAI·ON Agent execution", min_length=1, max_length=160)
    attempt: int = Field(default=1, ge=1)
    lease: UserAgentRunLease = Field(
        default_factory=lambda: UserAgentRunLease(status=RunLeaseStatus.RELEASED)
    )
    current_stage: RunStageKey | None = None
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    measurement_status: RunMeasurementStatus = RunMeasurementStatus.NOT_AVAILABLE
    stages: list[UserAgentRunStage] = Field(default_factory=list)
    audit_evidence: UserAgentRunAuditEvidence = Field(
        default_factory=UserAgentRunAuditEvidence
    )
    source_health: list[UserAgentRunSourceHealth] = Field(default_factory=list)
    data_provenance: RunDataProvenance = RunDataProvenance.LIVE


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
