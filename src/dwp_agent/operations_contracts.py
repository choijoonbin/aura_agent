from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, model_validator

from .contracts import ContractModel


class BootstrapRetentionPolicyRequest(ContractModel):
    idempotency_key: UUID
    expected_existing_count: int = Field(ge=0, le=0)
    retention_days: int = Field(default=90, ge=30, le=3650)
    legal_hold: bool = False
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "BootstrapRetentionPolicyRequest":
        self.change_reason = self.change_reason.strip()
        return self


class RetentionPolicy(ContractModel):
    retention_days: int = Field(ge=30, le=3650)
    legal_hold: bool
    policy_version: int = Field(ge=1)
    updated_at: datetime


class UpdateRetentionPolicyRequest(ContractModel):
    retention_days: int | None = Field(default=None, ge=30, le=3650)
    legal_hold: bool | None = None
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def require_change(self) -> "UpdateRetentionPolicyRequest":
        if self.retention_days is None and self.legal_hold is None:
            raise ValueError("At least one retention policy field must be supplied.")
        self.change_reason = self.change_reason.strip()
        return self


class DwaionOperationsOverview(ContractModel):
    period_days: int = Field(ge=1, le=90)
    run_count: int = Field(ge=0)
    completed_run_count: int = Field(ge=0)
    failed_run_count: int = Field(ge=0)
    allowed_run_count: int = Field(ge=0)
    handed_off_run_count: int = Field(ge=0)
    denied_run_count: int = Field(ge=0)
    grounded_answer_count: int = Field(ge=0)
    abstained_answer_count: int = Field(ge=0)
    configuration_required_count: int = Field(ge=0)
    average_latency_ms: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    active_user_count: int = Field(ge=0)
    conversation_count: int = Field(ge=0)
    feedback_up_count: int = Field(ge=0)
    feedback_down_count: int = Field(ge=0)
    retention: RetentionPolicy
    generated_at: datetime


class DwaionOperationsOverviewEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON operations overview loaded."
    success: bool = True
    data: DwaionOperationsOverview


class RetentionPolicyEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON retention policy loaded."
    success: bool = True
    data: RetentionPolicy
