from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .contract_model import ContractModel
from .dwaion_workflow_contracts import ResearchPlanDefinition


class ResearchRecoveryAction(StrEnum):
    SAVE_AS_FORK = "SAVE_AS_FORK"
    PULL_AND_MERGE = "PULL_AND_MERGE"
    KEEP_LOCAL = "KEEP_LOCAL"
    RECALCULATE_SENSITIVITY = "RECALCULATE_SENSITIVITY"
    USE_CACHE_FALLBACK = "USE_CACHE_FALLBACK"


class ResearchRecoveryCommandRequest(ContractModel):
    command_id: UUID
    idempotency_key: UUID
    expected_version: int = Field(ge=1)
    action: ResearchRecoveryAction
    local_definition: ResearchPlanDefinition | None = None
    reason: str = Field(min_length=5, max_length=500)

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 5:
            raise ValueError("Research recovery reason must contain at least 5 characters.")
        return normalized

    @model_validator(mode="after")
    def coherent_definition(self) -> "ResearchRecoveryCommandRequest":
        needs_local = self.action in {
            ResearchRecoveryAction.PULL_AND_MERGE,
            ResearchRecoveryAction.KEEP_LOCAL,
        }
        if needs_local and self.local_definition is None:
            raise ValueError("localDefinition is required for this recovery action.")
        if not needs_local and self.local_definition is not None:
            raise ValueError("localDefinition is not accepted for this recovery action.")
        return self


class ResearchSensitivityAssessment(ContractModel):
    classification: str = Field(
        pattern=r"^(PUBLIC|INTERNAL|CONFIDENTIAL|RESTRICTED)$"
    )
    score: int = Field(ge=0, le=100)
    matched_indicators: list[str] = Field(default_factory=list, max_length=20)


class ResearchRecoveryReceipt(ContractModel):
    receipt_id: UUID
    command_id: UUID
    action: ResearchRecoveryAction
    state: str = Field(default="COMPLETED", pattern=r"^COMPLETED$")
    run_id: UUID
    source_plan_id: UUID
    source_plan_revision: int = Field(ge=1)
    target_plan_id: UUID | None = None
    target_plan_revision: int | None = Field(default=None, ge=1)
    cached_run_id: UUID | None = None
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sensitivity: ResearchSensitivityAssessment | None = None
    integrity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime

    @model_validator(mode="after")
    def coherent_result(self) -> "ResearchRecoveryReceipt":
        has_target = self.target_plan_id is not None or self.target_plan_revision is not None
        if has_target and (self.target_plan_id is None or self.target_plan_revision is None):
            raise ValueError("A recovery target requires both plan ID and revision.")
        if self.action in {
            ResearchRecoveryAction.SAVE_AS_FORK,
            ResearchRecoveryAction.PULL_AND_MERGE,
            ResearchRecoveryAction.KEEP_LOCAL,
        } and self.target_plan_id is None:
            raise ValueError("The plan recovery action requires a target plan receipt.")
        if self.action == ResearchRecoveryAction.USE_CACHE_FALLBACK and (
            self.cached_run_id is None or self.result_sha256 is None
        ):
            raise ValueError("Cache fallback requires cached-run evidence.")
        if self.action == ResearchRecoveryAction.RECALCULATE_SENSITIVITY and (
            self.sensitivity is None or self.result_sha256 is None
        ):
            raise ValueError("Sensitivity recalculation requires assessment evidence.")
        return self


class ResearchRecoveryReceiptEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Research recovery action completed."
    success: bool = True
    data: ResearchRecoveryReceipt
