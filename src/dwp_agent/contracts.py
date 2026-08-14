from __future__ import annotations

from datetime import datetime
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


class AskState(StrEnum):
    COMPLETED = "COMPLETED"
    ABSTAINED = "ABSTAINED"
    CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"


class PolicyOutcome(StrEnum):
    ALLOW = "ALLOW"
    HANDOFF = "HANDOFF"
    DENY = "DENY"


class ModelRouteState(StrEnum):
    COMPLETED = "COMPLETED"
    NOT_INVOKED = "NOT_INVOKED"
    CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"
    REFUSED = "REFUSED"


class AnswerConfidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class CitationSourceType(StrEnum):
    WORK_ITEM = "WORK_ITEM"
    MAIL = "MAIL"
    CALENDAR = "CALENDAR"


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
        from .admin_commands import resolve_admin_command

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


class AskRequest(ContractModel):
    request_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=2, max_length=4_000)
    locale: str = Field(default="en", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
    agent_key: str = Field(
        default="DWP_ASSISTANT",
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$",
    )


class AskPolicyDecision(ContractModel):
    outcome: PolicyOutcome
    risk_tier: RiskTier
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{2,127}$")
    explanation: str = Field(min_length=1, max_length=500)
    model_allowed: bool
    mutation_allowed: bool = False

    @model_validator(mode="after")
    def validate_policy_boundary(self) -> "AskPolicyDecision":
        if self.mutation_allowed:
            raise ValueError("Ask is a read-only runtime and cannot allow mutations.")
        if self.outcome != PolicyOutcome.ALLOW and self.model_allowed:
            raise ValueError("A denied or handed-off request cannot invoke a model.")
        return self


class AskCitation(ContractModel):
    source_id: str = Field(pattern=r"^src-[0-9]{2}$")
    source_type: CitationSourceType
    title: str = Field(min_length=1, max_length=300)
    source_system: str = Field(min_length=1, max_length=100)
    route: str | None = Field(default=None, max_length=1_000)
    occurred_at: datetime | None = None


class AskModelRoute(ContractModel):
    state: ModelRouteState
    provider: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=160)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_route_state(self) -> "AskModelRoute":
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("Total tokens cannot be smaller than input plus output tokens.")
        if self.state == ModelRouteState.COMPLETED and not (self.provider and self.model):
            raise ValueError("A completed model route requires provider and model identifiers.")
        if self.state == ModelRouteState.NOT_INVOKED and any(
            (
                self.provider,
                self.model,
                self.input_tokens,
                self.output_tokens,
                self.total_tokens,
                self.latency_ms,
            )
        ):
            raise ValueError("A non-invoked model route cannot contain execution evidence.")
        return self


class AskResponse(ContractModel):
    run_id: str
    audit_id: str
    request_id: str
    correlation_id: str = Field(min_length=1, max_length=128)
    state: AskState
    answer: str | None = Field(default=None, max_length=8_000)
    confidence: AnswerConfidence | None = None
    citations: list[AskCitation] = Field(default_factory=list, max_length=20)
    source_count: int = Field(ge=0)
    policy: AskPolicyDecision
    model_route: AskModelRoute
    agent_registry: AgentRegistryResolution
    status_code: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{2,127}$")
    completed_at: datetime

    @model_validator(mode="after")
    def validate_answer_state(self) -> "AskResponse":
        if len(self.citations) > self.source_count:
            raise ValueError("Citations cannot exceed the scoped source count.")
        if self.state == AskState.COMPLETED:
            if not self.answer or not self.answer.strip():
                raise ValueError("A completed Ask response requires an answer.")
            if self.confidence is None or not self.citations:
                raise ValueError("A completed Ask response requires confidence and citations.")
            if self.policy.outcome != PolicyOutcome.ALLOW or not self.policy.model_allowed:
                raise ValueError("A completed Ask response requires an allowing policy decision.")
            if self.model_route.state != ModelRouteState.COMPLETED:
                raise ValueError("A completed Ask response requires a completed model route.")
            if self.status_code != "ANSWER_GROUNDED":
                raise ValueError("A completed Ask response must be grounded.")
        elif self.answer is not None or self.confidence is not None or self.citations:
            raise ValueError("A non-completed Ask response cannot expose an answer or citations.")
        return self


class AskEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Ask request evaluated."
    success: bool = True
    data: AskResponse
