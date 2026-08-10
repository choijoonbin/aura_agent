from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


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


class RegistryRiskTier(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RegistryResolutionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REFERENCE_FALLBACK = "REFERENCE_FALLBACK"


class AgentRegistryResolution(ContractModel):
    entry_key: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{0,99}$")
    revision: int = Field(ge=0)
    artifact_version: str = Field(min_length=1, max_length=64)
    risk_tier: RegistryRiskTier
    resolution: RegistryResolutionStatus


class AdminChangeIntent(ContractModel):
    command_key: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{2,127}$")
    target_type: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{1,63}$")
    target_id: str = Field(min_length=1, max_length=256)
    expected_version: int = Field(ge=0)
    parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=50)
    justification: str = Field(min_length=3, max_length=1_000)

    @model_validator(mode="after")
    def validate_registered_command(self) -> "AdminChangeIntent":
        from admin_commands import resolve_admin_command

        resolve_admin_command(self.command_key, self.target_type, self.parameters)
        return self


class AdminCommandResolution(ContractModel):
    command_key: str
    catalog_revision: int = Field(ge=1)
    target_service: str
    http_method: str
    endpoint_template: str
    required_permission: str


class PlanPreviewRequest(ContractModel):
    request_id: str = Field(min_length=1, max_length=128)
    intent: str = Field(min_length=1, max_length=2_000)
    action: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=256)
    source_references: list[str] = Field(default_factory=list, max_length=20)
    agent_key: str = Field(
        default="REFERENCE_PLANNER",
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$",
    )
    admin_change: AdminChangeIntent | None = None


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
    agent_registry: AgentRegistryResolution
    admin_command: AdminCommandResolution | None = None


class PlanPreviewEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Plan preview prepared."
    success: bool = True
    data: PlanPreviewResponse
