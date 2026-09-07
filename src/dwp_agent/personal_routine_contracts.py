from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel
from .governed_domain_contracts import HighRiskMutationCommand, MutationCommand


class RoutineConsentState(StrEnum):
    UNSET = "UNSET"
    DISABLED = "DISABLED"
    ENABLED = "ENABLED"
    RECONSENT_REQUIRED = "RECONSENT_REQUIRED"


class RoutineLifecycle(StrEnum):
    DRAFT = "DRAFT"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class RoutineCadence(StrEnum):
    DAILY = "DAILY"
    WEEKDAYS = "WEEKDAYS"
    WEEKLY = "WEEKLY"


class RoutineSource(StrEnum):
    WORK_ITEM = "WORK_ITEM"
    MAIL = "MAIL"
    CALENDAR = "CALENDAR"


class RoutineConsentScope(StrEnum):
    SOURCE_ACCESS = "SOURCE_ACCESS"
    ANALYSIS = "ANALYSIS"
    PROPOSAL_DELIVERY = "PROPOSAL_DELIVERY"


class RoutineLifecycleAction(StrEnum):
    PAUSE = "PAUSE"
    RESUME = "RESUME"


class RoutineDefinition(ContractModel):
    name: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=500)
    cadence: RoutineCadence
    local_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    time_zone: str = Field(min_length=1, max_length=64)
    locale: str = Field(pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    sources: list[RoutineSource] = Field(min_length=1, max_length=3)
    week_days: list[int] = Field(default_factory=list, max_length=7)
    active_from: date | None = None
    active_until: date | None = None
    quiet_hours_start: str | None = Field(
        default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    quiet_hours_end: str | None = Field(
        default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )

    @field_validator("name", "objective")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Routine text cannot be blank.")
        return normalized

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, value: list[RoutineSource]) -> list[RoutineSource]:
        if len(set(value)) != len(value):
            raise ValueError("Routine sources must be unique.")
        return value

    @field_validator("week_days")
    @classmethod
    def valid_week_days(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value) or any(day < 1 or day > 7 for day in value):
            raise ValueError("weekDays must contain unique ISO weekdays from 1 through 7.")
        return sorted(value)

    @model_validator(mode="after")
    def coherent_schedule(self) -> RoutineDefinition:
        if self.cadence == RoutineCadence.WEEKLY and not self.week_days:
            raise ValueError("A weekly routine requires at least one weekDay.")
        if self.cadence != RoutineCadence.WEEKLY and self.week_days:
            raise ValueError("weekDays can only be set for a weekly routine.")
        if self.active_from and self.active_until and self.active_until < self.active_from:
            raise ValueError("activeUntil cannot precede activeFrom.")
        if (self.quiet_hours_start is None) != (self.quiet_hours_end is None):
            raise ValueError("Both quiet-hours boundaries must be supplied together.")
        if self.quiet_hours_start == self.quiet_hours_end and self.quiet_hours_start:
            raise ValueError("Quiet hours cannot cover the full day.")
        return self


class CreateRoutineRequest(MutationCommand):
    definition: RoutineDefinition


class UpdateRoutineRequest(MutationCommand):
    definition: RoutineDefinition


class ChangeRoutineConsentRequest(HighRiskMutationCommand):
    scope: RoutineConsentScope
    consent_state: RoutineConsentState

    @field_validator("consent_state")
    @classmethod
    def explicit_consent(cls, value: RoutineConsentState) -> RoutineConsentState:
        if value not in {RoutineConsentState.ENABLED, RoutineConsentState.DISABLED}:
            raise ValueError("Consent can only be explicitly enabled or disabled.")
        return value


class DryRunRoutineRequest(MutationCommand):
    reference_time: datetime | None = None


class ChangeRoutineLifecycleRequest(HighRiskMutationCommand):
    action: RoutineLifecycleAction


class ArchiveRoutineRequest(HighRiskMutationCommand):
    pass


class RoutineConsentSet(ContractModel):
    source_access: RoutineConsentState
    analysis: RoutineConsentState
    proposal_delivery: RoutineConsentState


class RoutineCapabilities(ContractModel):
    lifecycle_mode: str = "DRAFT_PREVIEW_ONLY"
    activation_available: bool = False
    scheduling_available: bool = False
    background_execution_available: bool = False
    dry_run_available: bool = True
    pause_resume_available: bool = True
    one_time_schedule_available: bool = False
    active_window_preview_available: bool = True
    quiet_hours_preview_available: bool = True
    quiet_hours_delivery_enforcement_available: bool = False
    holiday_policy_available: bool = False
    cost_budget_available: bool = False
    runtime_budget_available: bool = False
    notification_delivery_available: bool = False
    proposal_delivery_available: bool = False
    external_write_available: bool = False
    supported_cadences: list[RoutineCadence] = Field(
        default_factory=lambda: list(RoutineCadence)
    )
    consent_scopes: list[RoutineConsentScope] = Field(
        default_factory=lambda: list(RoutineConsentScope)
    )


class PersonalRoutine(ContractModel):
    routine_id: UUID
    lifecycle_state: RoutineLifecycle
    consent_state: RoutineConsentState
    consents: RoutineConsentSet
    execution_mode: str = "DRY_RUN_ONLY"
    revision: int = Field(ge=1)
    definition: RoutineDefinition
    scheduling_available: bool = False
    next_run_at: datetime | None = None
    capabilities: RoutineCapabilities = Field(default_factory=RoutineCapabilities)
    created_at: datetime
    updated_at: datetime


class RoutineDryRunReceipt(ContractModel):
    routine_run_id: UUID
    routine_id: UUID
    routine_revision: int
    state: str = "VALIDATED"
    outcome: str = "VALIDATED"
    trigger: str = "DRY_RUN"
    proposal_only: bool = True
    external_writes_performed: int = 0
    proposals_created: int = 0
    evaluated_at: datetime
    evidence_count: int = Field(ge=0)
    evidence_scope: str = "AUTHORIZED_SOURCE_BINDING"
    business_evidence_count: int = 0
    validated_sources: list[RoutineSource]
    preview_next_run_at: datetime
    scheduling_available: bool = False


class RoutineEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: PersonalRoutine


class RoutineListEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: list[PersonalRoutine]


class RoutineDryRunEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: RoutineDryRunReceipt
