from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, model_validator

from .contract_model import ContractModel


class EvaluationComparisonSummary(ContractModel):
    comparison_id: str
    baseline_label: str
    candidate_label: str
    state: str = Field(pattern=r"^(QUEUED|RUNNING|PARTIAL|COMPLETED|FAILED)$")
    pass_rate: float | None = Field(default=None, ge=0, le=100)
    regression_count: int | None = Field(default=None, ge=0)
    evaluator_failure_count: int | None = Field(default=None, ge=0)
    dataset_id: str | None = Field(default=None, min_length=1, max_length=160)
    dataset_version: int | None = Field(default=None, ge=1)
    result_version: int | None = Field(default=None, ge=1)
    created_at: datetime


class GovernedEvaluationRunResult(ContractModel):
    run_id: str = Field(min_length=1, max_length=240)
    command_id: UUID
    dataset_id: str = Field(min_length=1, max_length=160)
    dataset_version: int = Field(ge=1)
    result_version: int = Field(ge=1)
    state: str = Field(pattern=r"^(ACCEPTED|COMPLETED)$")
    comparison_id: str | None = Field(default=None, min_length=1, max_length=240)


class GovernedEvaluationComparisonResult(EvaluationComparisonSummary):
    command_id: UUID
    dataset_id: str = Field(min_length=1, max_length=160)
    dataset_version: int = Field(ge=1)
    result_version: int = Field(ge=1)

    @model_validator(mode="after")
    def completed_receipt(self) -> "GovernedEvaluationComparisonResult":
        if self.state != "COMPLETED":
            raise ValueError("A successful comparison receipt must be COMPLETED.")
        return self
