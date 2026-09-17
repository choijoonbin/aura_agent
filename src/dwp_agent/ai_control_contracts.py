from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .contract_model import ContractModel


class BudgetEnforcementMode(StrEnum):
    ALERT_ONLY = "ALERT_ONLY"
    ENFORCED = "ENFORCED"


class EvaluationGateStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    STALE = "STALE"


class EvaluationEvidenceState(StrEnum):
    VERIFIED = "VERIFIED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class ExternalDataState(StrEnum):
    VERIFIED = "VERIFIED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    UNVERIFIED = "UNVERIFIED"


class MeasurementFreshness(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class RuntimeControlState(StrEnum):
    ENABLED = "ENABLED"
    EMERGENCY_DISABLED = "EMERGENCY_DISABLED"
    POLICY_NOT_CONFIGURED = "POLICY_NOT_CONFIGURED"
    CONTROL_UNAVAILABLE = "CONTROL_UNAVAILABLE"


class EnforcementActivationState(StrEnum):
    DISABLED = "DISABLED"
    ENABLED = "ENABLED"


class ToolEnforcementState(StrEnum):
    NOT_CONNECTED = "NOT_CONNECTED"


class ModelRoutePolicy(ContractModel):
    provider: str = Field(min_length=2, max_length=40, pattern=r"^[A-Z][A-Z0-9_]*$")
    model: str = Field(min_length=1, max_length=160)
    region: str | None = Field(default=None, max_length=80)
    availability_state: ExternalDataState = ExternalDataState.UNVERIFIED
    availability_observed_at: datetime | None = None

    @model_validator(mode="after")
    def normalize(self) -> "ModelRoutePolicy":
        self.provider = self.provider.strip().upper()
        self.model = self.model.strip()
        self.region = _optional_text(self.region)
        if not self.model:
            raise ValueError("Model identifiers cannot be blank.")
        for value in (self.model, self.region or ""):
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError("Model route identifiers cannot contain control characters.")
            _reject_secret_material(value)
        if self.availability_state == ExternalDataState.VERIFIED:
            if self.availability_observed_at is None:
                raise ValueError("Verified model availability requires an observation time.")
        return self


class TenantAIExecutionPolicy(ContractModel):
    emergency_disabled: bool
    allowed_model_routes: list[ModelRoutePolicy] = Field(min_length=1, max_length=50)
    allowed_tool_keys: list[str] = Field(default_factory=list, max_length=100)
    allowed_knowledge_sources: list[str] = Field(default_factory=list, max_length=50)
    max_output_tokens_per_request: int = Field(ge=128, le=4096)
    budget_enforcement_mode: BudgetEnforcementMode
    period_token_limit: int | None = Field(default=None, ge=1, le=10_000_000_000)
    alert_threshold_percent: int = Field(ge=1, le=100)
    require_evaluation_pass: bool
    evaluation_gate_status: EvaluationGateStatus
    evaluation_evidence_state: EvaluationEvidenceState = EvaluationEvidenceState.UNAVAILABLE
    evaluation_observed_at: datetime | None = None
    evaluation_policy_version: int | None = Field(default=None, ge=1)
    policy_version: int = Field(ge=1)
    updated_at: datetime

    @model_validator(mode="after")
    def validate_policy(self) -> "TenantAIExecutionPolicy":
        self.allowed_tool_keys = _normalized_keys(self.allowed_tool_keys, "tool")
        self.allowed_knowledge_sources = _normalized_keys(
            self.allowed_knowledge_sources, "knowledge source"
        )
        routes = {(route.provider, route.model, route.region) for route in self.allowed_model_routes}
        if len(routes) != len(self.allowed_model_routes):
            raise ValueError("Allowed model routes must be unique.")
        if self.budget_enforcement_mode == BudgetEnforcementMode.ENFORCED:
            if self.period_token_limit is None:
                raise ValueError("Enforced budgets require a period token limit.")
        if self.evaluation_gate_status == EvaluationGateStatus.PASSED:
            if self.evaluation_observed_at is None or self.evaluation_policy_version is None:
                raise ValueError("A passed evaluation gate requires versioned observation evidence.")
        return self


class AIUsageObservation(ContractModel):
    period_start: datetime
    period_end: datetime
    measured_input_tokens: int = Field(ge=0)
    measured_output_tokens: int = Field(ge=0)
    measured_total_tokens: int = Field(ge=0)
    reserved_tokens: int = Field(ge=0)
    unmeasured_reserved_tokens: int = Field(default=0, ge=0)
    measurement_freshness: MeasurementFreshness
    measurement_observed_at: datetime | None = None
    estimated_cost_minor: int | None = Field(default=None, ge=0)
    billed_cost_minor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    provider_usage_state: ExternalDataState = ExternalDataState.UNAVAILABLE
    provider_pricing_state: ExternalDataState = ExternalDataState.UNAVAILABLE
    provider_billing_state: ExternalDataState = ExternalDataState.UNAVAILABLE


class AIControlOverview(ContractModel):
    control_scope: Literal["ASK_RUNTIME"] = "ASK_RUNTIME"
    enforcement_activation_state: EnforcementActivationState
    runtime_control_state: RuntimeControlState
    tool_enforcement_state: ToolEnforcementState = ToolEnforcementState.NOT_CONNECTED
    policy: TenantAIExecutionPolicy | None = None
    usage: AIUsageObservation
    warnings: list[str] = Field(default_factory=list, max_length=20)


class AIControlOverviewEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON AI runtime control loaded."
    success: bool = True
    data: AIControlOverview


class BootstrapAIExecutionPolicyRequest(ContractModel):
    idempotency_key: UUID
    expected_existing_count: int = Field(ge=0, le=1)
    allowed_model_routes: list[ModelRoutePolicy] = Field(min_length=1, max_length=50)
    allowed_tool_keys: list[str] = Field(default_factory=list, max_length=100)
    allowed_knowledge_sources: list[str] = Field(default_factory=list, max_length=50)
    max_output_tokens_per_request: int = Field(default=900, ge=128, le=4096)
    budget_enforcement_mode: BudgetEnforcementMode = BudgetEnforcementMode.ALERT_ONLY
    period_token_limit: int | None = Field(default=None, ge=1, le=10_000_000_000)
    alert_threshold_percent: int = Field(default=80, ge=1, le=100)
    require_evaluation_pass: bool = False
    evaluation_gate_status: EvaluationGateStatus = EvaluationGateStatus.NOT_REQUIRED
    evaluation_observed_at: datetime | None = None
    evaluation_policy_version: int | None = Field(default=None, ge=1)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "BootstrapAIExecutionPolicyRequest":
        if self.expected_existing_count != 0:
            raise ValueError("AI runtime policy bootstrap requires expectedExistingCount=0.")
        _normalize_policy_request(self)
        return self


class UpdateAIExecutionPolicyRequest(ContractModel):
    allowed_model_routes: list[ModelRoutePolicy] = Field(min_length=1, max_length=50)
    allowed_tool_keys: list[str] = Field(default_factory=list, max_length=100)
    allowed_knowledge_sources: list[str] = Field(default_factory=list, max_length=50)
    max_output_tokens_per_request: int = Field(ge=128, le=4096)
    budget_enforcement_mode: BudgetEnforcementMode
    period_token_limit: int | None = Field(default=None, ge=1, le=10_000_000_000)
    alert_threshold_percent: int = Field(ge=1, le=100)
    require_evaluation_pass: bool
    evaluation_gate_status: EvaluationGateStatus
    evaluation_observed_at: datetime | None = None
    evaluation_policy_version: int | None = Field(default=None, ge=1)
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "UpdateAIExecutionPolicyRequest":
        _normalize_policy_request(self)
        return self


class SetAIEmergencyDisableRequest(ContractModel):
    disabled: bool
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "SetAIEmergencyDisableRequest":
        self.change_reason = self.change_reason.strip()
        _reject_secret_material(self.change_reason)
        return self


def _normalize_policy_request(request: object) -> None:
    routes = getattr(request, "allowed_model_routes")
    if any(
        route.availability_state != ExternalDataState.UNVERIFIED
        or route.availability_observed_at is not None
        for route in routes
    ):
        raise ValueError(
            "External model availability must come from verified provider observations."
        )
    required = bool(getattr(request, "require_evaluation_pass"))
    expected_status = (
        EvaluationGateStatus.PENDING if required else EvaluationGateStatus.NOT_REQUIRED
    )
    if (
        getattr(request, "evaluation_gate_status") != expected_status
        or getattr(request, "evaluation_observed_at") is not None
        or getattr(request, "evaluation_policy_version") is not None
    ):
        raise ValueError(
            "Administrative policy changes may only select NOT_REQUIRED or PENDING without "
            "evaluation evidence; PASSED, FAILED and STALE require a trusted evaluator receipt."
        )
    tools = _normalized_keys(getattr(request, "allowed_tool_keys"), "tool")
    sources = _normalized_keys(getattr(request, "allowed_knowledge_sources"), "source")
    setattr(request, "allowed_tool_keys", tools)
    setattr(request, "allowed_knowledge_sources", sources)
    reason = getattr(request, "change_reason").strip()
    setattr(request, "change_reason", reason)
    _reject_secret_material(reason)
    TenantAIExecutionPolicy(
        emergency_disabled=False,
        allowed_model_routes=routes,
        allowed_tool_keys=tools,
        allowed_knowledge_sources=sources,
        max_output_tokens_per_request=getattr(request, "max_output_tokens_per_request"),
        budget_enforcement_mode=getattr(request, "budget_enforcement_mode"),
        period_token_limit=getattr(request, "period_token_limit"),
        alert_threshold_percent=getattr(request, "alert_threshold_percent"),
        require_evaluation_pass=getattr(request, "require_evaluation_pass"),
        evaluation_gate_status=getattr(request, "evaluation_gate_status"),
        evaluation_observed_at=getattr(request, "evaluation_observed_at"),
        evaluation_policy_version=getattr(request, "evaluation_policy_version"),
        policy_version=1,
        updated_at=datetime.now().astimezone(),
    )


def _normalized_keys(values: list[str], label: str) -> list[str]:
    normalized = list(dict.fromkeys(value.strip().upper() for value in values))
    if any(re.fullmatch(r"[A-Z][A-Z0-9_.:-]{0,127}", value) is None for value in normalized):
        raise ValueError(f"Each {label} key must use the supported identifier format.")
    return normalized


def _optional_text(value: str | None) -> str | None:
    normalized = value.strip() if value else ""
    return normalized or None


_SECRET_PATTERN = re.compile(
    r"(?i)(?:api[_ -]?key|password|client[_ -]?secret|access[_ -]?token|bearer)\s*[:=]\s*\S+"
)


def _reject_secret_material(value: str) -> None:
    if _SECRET_PATTERN.search(value) or "-----BEGIN PRIVATE KEY-----" in value.upper():
        raise ValueError("AI policy records accept references, not secret material.")
