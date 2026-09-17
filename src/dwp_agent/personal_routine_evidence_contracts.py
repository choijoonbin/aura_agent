from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, JsonValue, field_validator, model_validator

from .contracts import ContractModel
from .governed_domain_contracts import HighRiskMutationCommand
from .personal_routine_contracts import PersonalRoutine, RoutineRunState


class TriggerRoutineWebhookRequest(HighRiskMutationCommand):
    event_id: UUID
    event_type: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{1,63}$")
    occurred_at: datetime
    payload: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)

    @field_validator("occurred_at")
    @classmethod
    def aware_occurrence(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurredAt must include a time-zone offset.")
        return value


class RollbackRoutineVersionRequest(HighRiskMutationCommand):
    pass


class RoutineRollbackReceipt(ContractModel):
    command_id: UUID
    routine_id: UUID
    target_revision: int = Field(ge=1)
    target_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_revision: int = Field(ge=2)
    routine: PersonalRoutine
    rolled_back_at: datetime
    integrity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def coherent_revision(self) -> "RoutineRollbackReceipt":
        if self.routine.routine_id != self.routine_id:
            raise ValueError("Rollback receipt routineId does not match its snapshot.")
        if self.routine.revision != self.created_revision:
            raise ValueError("Rollback receipt createdRevision does not match its snapshot.")
        return self


class RoutineRollbackEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: RoutineRollbackReceipt


class RoutineVersionSnapshot(ContractModel):
    command_id: UUID
    command_type: str = Field(min_length=1, max_length=24)
    revision: int = Field(ge=1)
    snapshot: PersonalRoutine
    created_at: datetime
    integrity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_target_revision: int | None = Field(default=None, ge=1)
    rollback_target_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class RoutineVersionListEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: list[RoutineVersionSnapshot]


class RoutineHealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"


class RoutineHealth(ContractModel):
    routine_id: UUID
    routine_revision: int = Field(ge=1)
    state: RoutineHealthState
    worker_available: bool
    schedule_current: bool
    all_consents_enabled: bool
    latest_run_id: UUID | None = None
    latest_run_state: RoutineRunState | None = None
    latest_run_at: datetime | None = None
    recovery_hints: list[str] = Field(default_factory=list)
    checked_at: datetime


class RoutineHealthEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: RoutineHealth


class RoutineTelemetryEvent(ContractModel):
    source: str = Field(pattern=r"^(ROUTINE|EXECUTION)$")
    event_id: UUID
    routine_run_id: UUID | None = None
    event_type: str = Field(min_length=1, max_length=40)
    previous_state: str | None = Field(default=None, max_length=32)
    current_state: str = Field(min_length=1, max_length=32)
    version: int = Field(ge=1)
    occurred_at: datetime
    integrity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
