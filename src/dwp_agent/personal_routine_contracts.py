from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel
from .dwaion_workflow_contracts import WorkflowCapability
from .governed_domain_contracts import HighRiskMutationCommand, MutationCommand


class RoutineConsentState(StrEnum):
    UNSET = "UNSET"
    DISABLED = "DISABLED"
    ENABLED = "ENABLED"
    RECONSENT_REQUIRED = "RECONSENT_REQUIRED"


class RoutineLifecycle(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
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


class RoutineActivationAction(StrEnum):
    ACTIVATE = "ACTIVATE"
    DEACTIVATE = "DEACTIVATE"


class RoutineExecutionMode(StrEnum):
    DRY_RUN_ONLY = "DRY_RUN_ONLY"
    SCHEDULED = "SCHEDULED"


class RoutineCompensationStrategy(StrEnum):
    REVOKE_PENDING_HANDOFFS = "REVOKE_PENDING_HANDOFFS"
    PROVIDER_MANAGED = "PROVIDER_MANAGED"


class RoutineRunTrigger(StrEnum):
    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"


class RoutineRunState(StrEnum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    COMPENSATING = "COMPENSATING"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    COMPENSATED = "COMPENSATED"


class RoutineRunCommand(StrEnum):
    RETRY = "RETRY"
    CANCEL = "CANCEL"
    COMPENSATE = "COMPENSATE"


class RoutineNotificationState(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    DELIVERED = "DELIVERED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    FAILED = "FAILED"


class RoutineBudget(ContractModel):
    maximum_runs_per_month: int = Field(default=31, ge=1, le=744)
    maximum_tokens_per_run: int = Field(default=32_000, ge=128, le=2_000_000)
    maximum_minutes_per_run: int = Field(default=15, ge=1, le=240)


class RoutineRetryPolicy(ContractModel):
    maximum_attempts: int = Field(default=3, ge=1, le=10)
    initial_backoff_seconds: int = Field(default=30, ge=5, le=3_600)
    backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)


class RoutineNotificationPolicy(ContractModel):
    notify_on_partial: bool = True
    notify_on_failure: bool = True
    notify_on_recovery: bool = True


class RoutineCompensationPolicy(ContractModel):
    enabled: bool = True
    strategy: RoutineCompensationStrategy = (
        RoutineCompensationStrategy.REVOKE_PENDING_HANDOFFS
    )


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
    budget: RoutineBudget = Field(default_factory=RoutineBudget)
    retry_policy: RoutineRetryPolicy = Field(default_factory=RoutineRetryPolicy)
    notification_policy: RoutineNotificationPolicy = Field(
        default_factory=RoutineNotificationPolicy
    )
    compensation_policy: RoutineCompensationPolicy = Field(
        default_factory=RoutineCompensationPolicy
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


class ChangeRoutineActivationRequest(HighRiskMutationCommand):
    action: RoutineActivationAction
    start_at: datetime | None = None

    @field_validator("start_at")
    @classmethod
    def aware_start_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("startAt must include a time-zone offset.")
        return value


class TriggerRoutineRunRequest(HighRiskMutationCommand):
    pass


class CommandRoutineRunRequest(HighRiskMutationCommand):
    action: RoutineRunCommand


class ArchiveRoutineRequest(HighRiskMutationCommand):
    pass


class RoutineConsentSet(ContractModel):
    source_access: RoutineConsentState
    analysis: RoutineConsentState
    proposal_delivery: RoutineConsentState


class RoutineCapabilities(ContractModel):
    lifecycle_mode: str = "GOVERNED_SCHEDULED_EXECUTION"
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
    oauth_reauthorization: WorkflowCapability
    temporary_budget_increase: WorkflowCapability
    operator_escalation: WorkflowCapability
    provider_rollback: WorkflowCapability
    execution_provider_state: str = "NOT_CONFIGURED"
    recovery_hint: str | None = (
        "Configure and start the governed routine execution broker."
    )
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
    execution_mode: RoutineExecutionMode = RoutineExecutionMode.DRY_RUN_ONLY
    revision: int = Field(ge=1)
    definition: RoutineDefinition
    scheduling_available: bool = False
    next_run_at: datetime | None = None
    capabilities: RoutineCapabilities = Field(default_factory=RoutineCapabilities)
    created_at: datetime
    updated_at: datetime


class RoutineExecutionReceipt(ContractModel):
    receipt_id: UUID
    routine_run_id: UUID
    routine_id: UUID
    routine_revision: int = Field(ge=1)
    terminal_state: RoutineRunState
    provider_receipt_id: str = Field(min_length=1, max_length=240)
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_count: int = Field(ge=0)
    proposals_created: int = Field(ge=0)
    approval_gated_actions_created: int = Field(ge=0)
    external_writes_performed: int = Field(default=0, ge=0)
    notification_state: RoutineNotificationState
    authorization_decision_revision: int = Field(ge=1)
    authorized_sources: list[RoutineSource] = Field(min_length=1, max_length=3)
    completed_at: datetime

    @model_validator(mode="after")
    def truthful_terminal_receipt(self) -> "RoutineExecutionReceipt":
        if self.terminal_state not in {
            RoutineRunState.COMPLETED,
            RoutineRunState.COMPENSATED,
        }:
            raise ValueError("A routine receipt requires a completed terminal state.")
        if self.external_writes_performed != 0:
            raise ValueError("Routine execution can only create approval-gated actions.")
        return self


class RoutineExecutionRun(ContractModel):
    routine_run_id: UUID
    routine_id: UUID
    routine_revision: int = Field(ge=1)
    trigger: RoutineRunTrigger
    state: RoutineRunState
    version: int = Field(ge=1)
    attempt_count: int = Field(ge=0)
    maximum_attempts: int = Field(ge=1, le=10)
    scheduled_for: datetime
    next_attempt_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    evidence_count: int = Field(default=0, ge=0)
    proposals_created: int = Field(default=0, ge=0)
    approval_gated_actions_created: int = Field(default=0, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)
    notification_state: RoutineNotificationState = RoutineNotificationState.NOT_REQUIRED
    safe_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    recovery_hint: str | None = Field(default=None, max_length=1_000)
    compensation_required: bool = False
    receipt: RoutineExecutionReceipt | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def coherent_run_state(self) -> "RoutineExecutionRun":
        terminal = {
            RoutineRunState.PARTIAL,
            RoutineRunState.COMPLETED,
            RoutineRunState.FAILED,
            RoutineRunState.CANCELLED,
            RoutineRunState.COMPENSATED,
        }
        if self.state in terminal and self.completed_at is None:
            raise ValueError("A terminal routine run requires completedAt.")
        if self.state not in terminal and self.completed_at is not None:
            raise ValueError("A non-terminal routine run cannot be completed.")
        if self.receipt is not None and self.state not in {
            RoutineRunState.COMPLETED,
            RoutineRunState.COMPENSATED,
        }:
            raise ValueError("A success receipt cannot be attached before completion.")
        return self


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


class RoutineCapabilitiesEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: RoutineCapabilities


class RoutineExecutionEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: RoutineExecutionRun


class RoutineExecutionListEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: list[RoutineExecutionRun]
