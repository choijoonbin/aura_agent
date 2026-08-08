from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class RiskTier(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class PlanState(StrEnum):
    REVIEW = "REVIEW"


class PlanPreviewRequest(ContractModel):
    request_id: str = Field(min_length=1, max_length=128)
    intent: str = Field(min_length=1, max_length=2_000)
    action: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=256)
    source_references: list[str] = Field(default_factory=list, max_length=20)


class PlanStep(ContractModel):
    id: str
    title: str
    tool: str
    description: str


class PlanPreviewResponse(ContractModel):
    run_id: str
    audit_id: str
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    correlation_id: str = Field(min_length=1, max_length=128)
    state: PlanState
    risk_tier: RiskTier
    approval_required: bool
    mutation_allowed: bool
    summary: str
    steps: list[PlanStep]
    source_references: list[str]
    reference_mode: bool


class PlanPreviewEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Plan preview prepared."
    success: bool = True
    data: PlanPreviewResponse
