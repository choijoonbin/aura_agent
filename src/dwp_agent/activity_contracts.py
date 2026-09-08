from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import Field

from .contracts import ContractModel
from .run_observability import RunDataProvenance, RunStageKey


@dataclass(frozen=True)
class ActivityRunSnapshot:
    """Privacy-minimized current execution state, never a historical event journal."""

    run_id: UUID
    tenant_id: str
    user_id: str
    agent_key: str
    agent_revision: int
    run_state: str
    risk_tier: str
    policy_outcome: str
    created_at: datetime
    generation: int = 1
    answer_state: str | None = None
    status_code: str | None = None
    source_count: int = 0
    latency_ms: int = 0
    completed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    current_stage: RunStageKey | None = None
    progress_percent: int | None = None
    audit_id: str | None = None
    audit_record_id: UUID | None = None
    audit_link_state: str | None = None
    data_provenance: RunDataProvenance = RunDataProvenance.LIVE

    def activity_state(self, now: datetime) -> str:
        if self.run_state == "RUNNING":
            return (
                "UNKNOWN"
                if self.lease_expires_at is None or self.lease_expires_at <= now
                else "RUNNING"
            )
        if self.run_state == "FAILED":
            return "FAILED"
        if self.policy_outcome == "DENY":
            return "POLICY_BLOCKED"
        if self.policy_outcome == "HANDOFF":
            return "NEEDS_INPUT"
        return "COMPLETED"

    def execution_version(self, now: datetime) -> int:
        # Each fenced attempt can only progress from running to one terminal state.
        # Lease expiry is an observation, not a source-ledger transition/version.
        return max(1, self.generation) * 2 + (self.run_state != "RUNNING")


class ActivityCoverage(ContractModel):
    supported_object_types: list[str] = Field(default_factory=lambda: ["AGENT_RUN"])
    includes_legacy: bool = False
    includes_usage: bool = False
    excluded_provenance: list[str] = Field(default_factory=lambda: ["SAMPLE", "QUARANTINED"])
    source_scope: str = "DWAI_ON"
    semantics: str = "CURRENT_EXECUTION_SNAPSHOTS"


class ActivityEvent(ContractModel):
    id: UUID
    occurred_at: datetime
    actor: str = "AGENT"
    actor_name: str = "DWAI·ON"
    state: str
    title: str
    summary: str
    object_type: str = "AGENT_RUN"
    object_label: str
    source: str = "DWAI_ON"
    tool: str | None = None
    audit_id: str | None = None
    progress: int | None = None
    source_route: str
    event_kind: str = "EXECUTION_SNAPSHOT"
    source_event_id: str
    object_id: str
    execution_id: str
    execution_version: int
    attempt: int
    work_status: str | None = None
    correlation_id: str | None = None
    audit_record_id: UUID | None = None
    data_provenance: RunDataProvenance = RunDataProvenance.LIVE
    source_access: str = "AVAILABLE"
    audit_access: str = "RESTRICTED"
    audit_status: str = "NOT_LINKED"
    resume_cursor: str | None = None
    source_observed_at: datetime
    updated_at: datetime | None = None


class ActivityPage(ContractModel):
    events: list[ActivityEvent]
    generated_at: datetime
    snapshot_at: datetime
    start_cursor: str
    next_cursor: str | None = None
    has_more: bool
    coverage: ActivityCoverage = Field(default_factory=ActivityCoverage)


class ExecutionSummary(ContractModel):
    total: int = 0
    running: int = 0
    needs_input: int = 0
    policy_blocked: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    unknown: int = 0
    generated_at: datetime
    coverage: ActivityCoverage = Field(default_factory=ActivityCoverage)


class ActivityPageEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Current Agent execution snapshots loaded."
    success: bool = True
    data: ActivityPage


class ActivityEventEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Current Agent execution snapshot loaded."
    success: bool = True
    data: ActivityEvent


class ExecutionSummaryEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Current Agent execution summary loaded."
    success: bool = True
    data: ExecutionSummary
