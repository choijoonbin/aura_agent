from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel
from .governed_domain_contracts import HighRiskMutationCommand
from .personal_routine_contracts import RoutineDefinition, RoutineSource


class RoutineAdvancedCommandKind(StrEnum):
    CHANGE_APPROVAL = "CHANGE_APPROVAL"
    AGENT_ENGINE_SWITCH = "AGENT_ENGINE_SWITCH"
    WORM_EVIDENCE_DELIVERY = "WORM_EVIDENCE_DELIVERY"
    OAUTH_REAUTHORIZATION = "OAUTH_REAUTHORIZATION"
    TEMPORARY_BUDGET_INCREASE = "TEMPORARY_BUDGET_INCREASE"
    OPERATOR_ESCALATION = "OPERATOR_ESCALATION"
    PROVIDER_ROLLBACK = "PROVIDER_ROLLBACK"


class RoutineAdvancedCommandState(StrEnum):
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class RoutineChangeApprovalPayload(ContractModel):
    kind: Literal[RoutineAdvancedCommandKind.CHANGE_APPROVAL]
    definition: RoutineDefinition


class RoutineAgentEngineSwitchPayload(ContractModel):
    kind: Literal[RoutineAdvancedCommandKind.AGENT_ENGINE_SWITCH]
    action: Literal["APPLY", "ROLLBACK"] = "APPLY"
    agent_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    engine_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def coherent_switch(self) -> "RoutineAgentEngineSwitchPayload":
        if self.action == "ROLLBACK":
            if any(value is not None for value in (self.agent_id, self.engine_id, self.expires_at)):
                raise ValueError("An engine rollback cannot carry a replacement engine.")
            return self
        if self.agent_id is None or self.engine_id is None or self.expires_at is None:
            raise ValueError("An engine switch requires an agent, engine, and expiry.")
        now = datetime.now(UTC)
        if self.expires_at.utcoffset() is None or not now < self.expires_at <= now + timedelta(days=30):
            raise ValueError("An engine switch must expire within 30 days.")
        return self


class RoutineWormEvidenceDeliveryPayload(ContractModel):
    kind: Literal[RoutineAdvancedCommandKind.WORM_EVIDENCE_DELIVERY]
    evidence_scope: Literal["ROUTINE_HISTORY", "LATEST_RUN", "FULL_AUDIT"]
    retention_days: int = Field(ge=1, le=3650)
    legal_hold: bool = False


class RoutineOAuthReauthorizationPayload(ContractModel):
    kind: Literal[RoutineAdvancedCommandKind.OAUTH_REAUTHORIZATION]
    source: RoutineSource
    connection_reference: str = Field(
        min_length=1, max_length=240, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$"
    )


class RoutineTemporaryBudgetIncreasePayload(ContractModel):
    kind: Literal[RoutineAdvancedCommandKind.TEMPORARY_BUDGET_INCREASE]
    additional_runs: int = Field(default=0, ge=0, le=744)
    additional_tokens_per_run: int = Field(default=0, ge=0, le=2_000_000)
    additional_minutes_per_run: int = Field(default=0, ge=0, le=240)
    expires_at: datetime

    @model_validator(mode="after")
    def bounded_exception(self) -> "RoutineTemporaryBudgetIncreasePayload":
        if not any(
            (self.additional_runs, self.additional_tokens_per_run, self.additional_minutes_per_run)
        ):
            raise ValueError("A temporary budget increase must raise at least one limit.")
        now = datetime.now(UTC)
        if self.expires_at.utcoffset() is None or not now < self.expires_at <= now + timedelta(days=30):
            raise ValueError("A temporary budget increase must expire within 30 days.")
        return self


class RoutineOperatorEscalationPayload(ContractModel):
    kind: Literal[RoutineAdvancedCommandKind.OPERATOR_ESCALATION]
    severity: Literal["P1", "P2", "P3"]
    summary: str = Field(min_length=10, max_length=1000)
    routine_run_id: UUID | None = None

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 10:
            raise ValueError("Operator escalation summary is too short.")
        return normalized


class RoutineProviderRollbackPayload(ContractModel):
    kind: Literal[RoutineAdvancedCommandKind.PROVIDER_ROLLBACK]
    routine_run_id: UUID
    provider_receipt_id: str = Field(min_length=1, max_length=240)

    @field_validator("provider_receipt_id")
    @classmethod
    def normalize_provider_receipt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Provider receipt ID must not be blank.")
        return normalized


RoutineAdvancedPayload = Annotated[
    RoutineChangeApprovalPayload
    | RoutineAgentEngineSwitchPayload
    | RoutineWormEvidenceDeliveryPayload
    | RoutineOAuthReauthorizationPayload
    | RoutineTemporaryBudgetIncreasePayload
    | RoutineOperatorEscalationPayload
    | RoutineProviderRollbackPayload,
    Field(discriminator="kind"),
]


class RoutineAdvancedProviderOutcome(ContractModel):
    routine_id: UUID
    expected_revision: int = Field(ge=1)
    kind: RoutineAdvancedCommandKind
    outcome: Literal["APPLIED", "PARTIALLY_APPLIED"]
    applied_payload: RoutineAdvancedPayload
    evidence_ref: str = Field(min_length=1, max_length=240)

    @field_validator("evidence_ref")
    @classmethod
    def normalize_evidence_ref(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Provider evidence reference must not be blank.")
        return normalized


class CreateRoutineAdvancedCommandRequest(HighRiskMutationCommand):
    payload: RoutineAdvancedPayload


class DecideRoutineAdvancedCommandRequest(HighRiskMutationCommand):
    decision: Literal["APPROVE", "REJECT"]
    evidence_refs: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(
        min_length=1, max_length=20
    )

    @field_validator("evidence_refs")
    @classmethod
    def normalize_evidence(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if not normalized or any(not value for value in normalized):
            raise ValueError("Approval evidence references must not be blank.")
        if len(normalized) != len(set(normalized)):
            raise ValueError("Approval evidence references must be unique.")
        return normalized


class RoutineAdvancedCommandProblem(ContractModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$")
    detail: str = Field(min_length=1, max_length=500)
    recovery_hint: str = Field(min_length=1, max_length=1000)


class RoutineAdvancedCommandReceipt(ContractModel):
    receipt_id: UUID
    command_id: UUID
    routine_id: UUID
    kind: RoutineAdvancedCommandKind
    state: Literal[
        RoutineAdvancedCommandState.SUCCEEDED,
        RoutineAdvancedCommandState.PARTIAL,
    ]
    provider_receipt_id: str = Field(min_length=1, max_length=240)
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_outcome: RoutineAdvancedProviderOutcome
    applied_revision: int | None = Field(default=None, ge=1)
    completed_at: datetime

    @field_validator("provider_receipt_id")
    @classmethod
    def normalize_provider_receipt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Provider receipt ID must not be blank.")
        return normalized


class RoutineAdvancedCommand(ContractModel):
    command_id: UUID
    routine_id: UUID
    owner_user_id: str
    kind: RoutineAdvancedCommandKind
    state: RoutineAdvancedCommandState
    expected_revision: int = Field(ge=1)
    version: int = Field(ge=1)
    maker_user_id: str
    checker_user_id: str | None = None
    can_approve: bool = False
    proposed_definition: RoutineDefinition | None
    problem: RoutineAdvancedCommandProblem | None = None
    receipt: RoutineAdvancedCommandReceipt | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def coherent_state(self) -> "RoutineAdvancedCommand":
        if self.state in {RoutineAdvancedCommandState.SUCCEEDED, RoutineAdvancedCommandState.PARTIAL}:
            if self.receipt is None or self.receipt.state != self.state:
                raise ValueError("A completed advanced command requires a matching receipt.")
        elif self.receipt is not None:
            raise ValueError("A non-completed advanced command cannot carry a receipt.")
        if self.state in {RoutineAdvancedCommandState.FAILED, RoutineAdvancedCommandState.PARTIAL}:
            if self.problem is None:
                raise ValueError("An incomplete advanced command requires a problem.")
        elif self.problem is not None:
            raise ValueError("Only failed or partial advanced commands can carry a problem.")
        if self.checker_user_id == self.maker_user_id:
            raise ValueError("Maker and checker must be different users.")
        if (self.kind == RoutineAdvancedCommandKind.CHANGE_APPROVAL) != (
            self.proposed_definition is not None
        ):
            raise ValueError("Only a change approval carries a proposed routine definition.")
        return self


class RoutineAdvancedCommandEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: RoutineAdvancedCommand


class RoutineAdvancedCommandListEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: list[RoutineAdvancedCommand]
