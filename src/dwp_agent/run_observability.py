from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5


class RunStageKey(StrEnum):
    AUTHORIZING = "AUTHORIZING"
    RETRIEVING = "RETRIEVING"
    REASONING = "REASONING"
    VERIFYING = "VERIFYING"
    PERSISTING = "PERSISTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RunStageState(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class RunSourceHealthStatus(StrEnum):
    SUCCESS = "SUCCESS"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class RunDataProvenance(StrEnum):
    LIVE = "LIVE"
    SAMPLE = "SAMPLE"


STAGE_SEQUENCE = {
    RunStageKey.AUTHORIZING: 10,
    RunStageKey.RETRIEVING: 20,
    RunStageKey.REASONING: 30,
    RunStageKey.VERIFYING: 40,
    RunStageKey.PERSISTING: 50,
    RunStageKey.COMPLETED: 60,
    RunStageKey.FAILED: 60,
}

OPERATIONAL_STAGES = tuple(stage for stage in RunStageKey if stage not in {
    RunStageKey.COMPLETED, RunStageKey.FAILED
})


@dataclass(frozen=True)
class RunStageSnapshot:
    key: RunStageKey
    state: RunStageState
    sequence: int
    started_at: datetime
    completed_at: datetime | None = None

    def duration_ms(self, now: datetime) -> int:
        end = self.completed_at or now
        return max(0, int((end - self.started_at).total_seconds() * 1_000))


@dataclass(frozen=True)
class SourceHealthObservation:
    source_type: str
    status: RunSourceHealthStatus
    observed_at: datetime
    latency_ms: int | None = None


@dataclass(frozen=True)
class RunSourceHealthSnapshot:
    source_type: str
    status: RunSourceHealthStatus
    latency_ms: int | None
    last_attempt_at: datetime
    last_success_at: datetime | None


def audit_record_id(audit_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"urn:dwp:audit:{audit_id}")


def stage_sequence(stage: RunStageKey | str) -> int:
    return STAGE_SEQUENCE[RunStageKey(stage)]


def progress_percent(stages: tuple[RunStageSnapshot, ...]) -> int | None:
    if not stages:
        return None
    transitioned = sum(
        stage.key in OPERATIONAL_STAGES
        and stage.state in {RunStageState.COMPLETED, RunStageState.SKIPPED}
        for stage in stages
    )
    return transitioned * 100 // len(OPERATIONAL_STAGES)


def safe_activity_title(agent_key: str) -> str:
    titles = {
        "DWP_ASSISTANT": "DWAI·ON Assistant execution",
        "DWP_APPROVAL_EXPERT": "DWAI·ON Approval expert execution",
    }
    return titles.get(agent_key.strip().upper(), "DWAI·ON Agent execution")
