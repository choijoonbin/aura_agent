from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from .contract_model import ContractModel


class RoutingRule(ContractModel):
    rule_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=240)
    task_type: str = Field(min_length=1, max_length=160)
    conditions: list[str]
    allowed_data_classifications: list[str]
    primary_model_id: str
    fallback_model_ids: list[str]
    fail_closed: bool
    version: int = Field(ge=1)


class LatestRoutingSimulation(ContractModel):
    simulation_id: str
    decision: Literal["ROUTED", "BLOCKED", "REVIEW_REQUIRED"]
    matched_rule_id: str
    target_model_id: str | None
    estimated_cost: float | None = Field(default=None, ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    estimated_latency_ms: float | None = Field(default=None, ge=0)
    fallback_model_ids: list[str]
    generated_at: datetime


class ModelRouteSimulationInput(ContractModel):
    requester_role: str = Field(min_length=1, max_length=160)
    agent_id: str = Field(min_length=1, max_length=160)
    data_classification: str = Field(min_length=1, max_length=160)
    estimated_tokens: int = Field(ge=1)
    modality: str = Field(min_length=1, max_length=80)
    constraints: list[str] = Field(max_length=100)

    @field_validator("constraints", mode="before")
    @classmethod
    def normalize_constraints(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value
