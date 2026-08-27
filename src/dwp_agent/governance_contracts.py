from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, model_validator

from .contracts import CitationSourceType, ContractModel, PolicyOutcome, RiskTier


class DataClassification(StrEnum):
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class SourceAccessMode(StrEnum):
    SOURCE_PERMISSIONS = "SOURCE_PERMISSIONS"
    TENANT_ALLOWLIST = "TENANT_ALLOWLIST"
    BLOCKED = "BLOCKED"


class ConnectionState(StrEnum):
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    BLOCKED = "BLOCKED"


class ActionExecutionPolicy(StrEnum):
    USER_HANDOFF = "USER_HANDOFF"
    APPROVAL_HANDOFF = "APPROVAL_HANDOFF"
    BLOCKED = "BLOCKED"


class EvaluationLifecycle(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class EvaluationRunState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"
    FAILED = "FAILED"


class EvaluationOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"


class BootstrapGovernancePoliciesRequest(ContractModel):
    idempotency_key: UUID
    expected_existing_count: int = Field(ge=0, le=100)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "BootstrapGovernancePoliciesRequest":
        self.change_reason = self.change_reason.strip()
        return self


class DataSourcePolicy(ContractModel):
    source_key: CitationSourceType
    display_name: str
    description: str
    provider_type: str
    classification: DataClassification
    access_mode: SourceAccessMode
    enabled: bool
    connection_state: ConnectionState
    connector_ref: str | None = None
    policy_version: int = Field(ge=1)
    updated_at: datetime


class UpdateDataSourcePolicyRequest(ContractModel):
    enabled: bool
    access_mode: SourceAccessMode
    classification: DataClassification
    connector_ref: str | None = Field(default=None, max_length=160)
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "UpdateDataSourcePolicyRequest":
        self.connector_ref = self.connector_ref.strip() if self.connector_ref else None
        self.change_reason = self.change_reason.strip()
        if self.enabled and self.access_mode == SourceAccessMode.BLOCKED:
            raise ValueError("An enabled source cannot use BLOCKED access mode.")
        return self


class ActionPolicy(ContractModel):
    action_key: str
    title: str
    description: str
    risk_tier: RiskTier
    required_permission: str
    enabled: bool
    confirmation_required: bool
    execution_policy: ActionExecutionPolicy
    policy_version: int = Field(ge=1)
    updated_at: datetime


class UpdateActionPolicyRequest(ContractModel):
    enabled: bool
    confirmation_required: bool = True
    execution_policy: ActionExecutionPolicy
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "UpdateActionPolicyRequest":
        self.change_reason = self.change_reason.strip()
        if self.enabled and self.execution_policy == ActionExecutionPolicy.BLOCKED:
            raise ValueError("An enabled action cannot use BLOCKED execution policy.")
        if self.enabled and not self.confirmation_required:
            raise ValueError("DWAI-ON actions require explicit user confirmation.")
        return self


class SafetyPolicy(ContractModel):
    prompt_injection_outcome: PolicyOutcome
    privileged_data_outcome: PolicyOutcome
    mutation_outcome: PolicyOutcome
    require_citations: bool
    public_web_enabled: bool
    max_source_scopes: int = Field(ge=1, le=7)
    max_tool_calls: int = Field(ge=0, le=10)
    policy_version: int = Field(ge=1)
    updated_at: datetime


class UpdateSafetyPolicyRequest(ContractModel):
    privileged_data_outcome: PolicyOutcome
    mutation_outcome: PolicyOutcome
    require_citations: bool = True
    max_source_scopes: int = Field(ge=1, le=7)
    max_tool_calls: int = Field(ge=0, le=10)
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def validate_outcomes(self) -> "UpdateSafetyPolicyRequest":
        if self.privileged_data_outcome not in {PolicyOutcome.DENY, PolicyOutcome.HANDOFF}:
            raise ValueError("Privileged data must be denied or handed off.")
        if self.mutation_outcome not in {PolicyOutcome.DENY, PolicyOutcome.HANDOFF}:
            raise ValueError("Mutations must be denied or handed off.")
        if not self.require_citations:
            raise ValueError("Grounded DWAI-ON answers require citations.")
        self.change_reason = self.change_reason.strip()
        return self


class EvaluationSetSummary(ContractModel):
    evaluation_set_id: UUID
    name: str
    description: str | None = None
    locale: str
    lifecycle_state: EvaluationLifecycle
    case_count: int = Field(ge=0)
    latest_run_state: EvaluationRunState | None = None
    latest_pass_rate: int | None = Field(default=None, ge=0, le=100)
    version: int = Field(ge=1)
    updated_at: datetime


class EvaluationCase(ContractModel):
    evaluation_case_id: UUID
    evaluation_set_id: UUID
    name: str
    prompt: str
    expected_terms: list[str]
    source_scopes: list[CitationSourceType]
    version: int = Field(ge=1)
    created_at: datetime


class EvaluationSetDetail(ContractModel):
    summary: EvaluationSetSummary
    cases: list[EvaluationCase]


class CreateEvaluationSetRequest(ContractModel):
    name: str = Field(min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    locale: str = Field(default="ko-KR", pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")


class CreateEvaluationCaseRequest(ContractModel):
    name: str = Field(min_length=2, max_length=160)
    prompt: str = Field(min_length=2, max_length=4000)
    expected_terms: list[str] = Field(default_factory=list, max_length=20)
    source_scopes: list[CitationSourceType] = Field(
        default_factory=list, min_length=1, max_length=7
    )

    @model_validator(mode="after")
    def normalize(self) -> "CreateEvaluationCaseRequest":
        self.name = self.name.strip()
        self.prompt = self.prompt.strip()
        self.expected_terms = list(dict.fromkeys(
            term.strip() for term in self.expected_terms if term.strip()
        ))
        if any(len(term) > 160 for term in self.expected_terms):
            raise ValueError("Expected terms must contain at most 160 characters.")
        self.source_scopes = list(dict.fromkeys(self.source_scopes))
        return self


class UpdateEvaluationLifecycleRequest(ContractModel):
    lifecycle_state: EvaluationLifecycle
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "UpdateEvaluationLifecycleRequest":
        if self.lifecycle_state == EvaluationLifecycle.DRAFT:
            raise ValueError("A published evaluation set cannot transition back to draft.")
        self.change_reason = self.change_reason.strip()
        return self


class EvaluationResult(ContractModel):
    evaluation_case_id: UUID
    case_name: str
    outcome: EvaluationOutcome
    status_code: str
    grounded: bool
    expected_terms_matched: int = Field(ge=0)
    expected_terms_total: int = Field(ge=0)
    latency_ms: int = Field(ge=0)


class EvaluationRun(ContractModel):
    evaluation_run_id: UUID
    evaluation_set_id: UUID
    run_state: EvaluationRunState
    case_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    configuration_required_count: int = Field(ge=0)
    model_ref: str | None = None
    results: list[EvaluationResult] = Field(default_factory=list)
    created_at: datetime
    completed_at: datetime | None = None


class EvaluationRunSummary(ContractModel):
    evaluation_run_id: UUID
    evaluation_set_id: UUID
    run_state: EvaluationRunState
    case_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    configuration_required_count: int = Field(ge=0)
    pass_rate: int | None = Field(default=None, ge=0, le=100)
    model_ref: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class GovernanceAuditEvent(ContractModel):
    event_id: UUID
    category: str
    event_type: str
    target_type: str
    target_key: str
    actor_user_id: str
    correlation_id: str
    change_reason: str | None = None
    created_at: datetime


class GovernanceAuditPage(ContractModel):
    content: list[GovernanceAuditEvent]
    page: int = Field(ge=0)
    size: int = Field(ge=1, le=100)
    total_elements: int = Field(ge=0)
    total_pages: int = Field(ge=0)


class DataSourcePolicyListEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON source policies loaded."
    success: bool = True
    data: list[DataSourcePolicy]


class DataSourcePolicyEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON source policy loaded."
    success: bool = True
    data: DataSourcePolicy


class ActionPolicyListEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON action policies loaded."
    success: bool = True
    data: list[ActionPolicy]


class ActionPolicyEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON action policy loaded."
    success: bool = True
    data: ActionPolicy


class SafetyPolicyEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON safety policy loaded."
    success: bool = True
    data: SafetyPolicy


class EvaluationSetListEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON evaluation sets loaded."
    success: bool = True
    data: list[EvaluationSetSummary]


class EvaluationSetEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON evaluation set loaded."
    success: bool = True
    data: EvaluationSetDetail


class EvaluationRunEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON evaluation completed."
    success: bool = True
    data: EvaluationRun


class EvaluationRunListEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON evaluation runs loaded."
    success: bool = True
    data: list[EvaluationRunSummary]


class GovernanceAuditEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON audit evidence loaded."
    success: bool = True
    data: GovernanceAuditPage
