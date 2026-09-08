from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .grounded_response_status import grounded_status_for_provider


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
    APPROVAL_TASK = "APPROVAL_TASK"
    APPROVAL_REQUEST = "APPROVAL_REQUEST"
    APPROVAL_FORM = "APPROVAL_FORM"
    APPROVAL_OPERATION = "APPROVAL_OPERATION"


class ConversationRole(StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


class AnswerFeedbackRating(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class WorkplaceActionMode(StrEnum):
    REDIRECT = "REDIRECT"
    APPROVAL_HANDOFF = "APPROVAL_HANDOFF"


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
    required_permission: str | None = None
    authority_kind: Literal["TENANT_PERMISSION", "PROVIDER_ROLE", "APP_GOVERNANCE_CAPABILITY"]
    identity_plane: Literal["TENANT", "PROVIDER"]
    required_roles: list[str] = Field(default_factory=list)
    required_authorities: list[str] = Field(default_factory=list)
    body_parameters: list[str] = Field(default_factory=list)
    query_parameters: list[str] = Field(default_factory=list)
    header_parameters: dict[str, str] = Field(default_factory=dict)
    context_parameters: list[str] = Field(default_factory=list)
    final_authority_service: str

class ActionHandoffOrigin(ContractModel):
    app_key: Literal["APP.ASK"]
    route: str = Field(max_length=500, pattern=r"^/dwaion/(?:new|conversations/[0-9a-f-]{36})$")
    surface: Literal["action-shelf"]
    source_run_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    source_request_id: str = Field(min_length=1, max_length=128)
    source_correlation_id: str = Field(min_length=1, max_length=128)
    conversation_id: UUID | None = None


class PlanPreviewRequest(ContractModel):
    request_id: str = Field(min_length=1, max_length=128)
    intent: str = Field(min_length=1, max_length=2_000)
    action: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=256)
    source_references: list[str] = Field(default_factory=list, max_length=20)
    inputs: dict[str, JsonValue] = Field(default_factory=dict, max_length=20)
    agent_key: str = Field(
        default="REFERENCE_PLANNER",
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$",
    )
    admin_change: AdminChangeIntent | None = None
    handoff_origin: ActionHandoffOrigin | None = None


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
    handoff_origin: ActionHandoffOrigin | None = None


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
    conversation_id: UUID | None = None
    source_scopes: list[CitationSourceType] = Field(
        default_factory=lambda: list(CitationSourceType),
        min_length=1,
        max_length=7,
    )
    page_context: "AskPageContext | None" = None

    @model_validator(mode="after")
    def normalize_source_scopes(self) -> "AskRequest":
        self.source_scopes = list(dict.fromkeys(self.source_scopes))
        selected = self.page_context.selected_work if self.page_context else None
        if selected is not None:
            approval = selected.source_system.startswith("APPROVAL_")
            scope = CitationSourceType(selected.source_system) if approval else CitationSourceType.WORK_ITEM
            agent = "DWP_APPROVAL_EXPERT" if approval else "DWP_ASSISTANT"
            if self.source_scopes != [scope] or self.agent_key != agent:
                raise ValueError("Selected work must use its exact source scope and read-only agent.")
        return self


class AskSelectedWork(ContractModel):
    source_system: Literal[
        "PERSONAL_TASK", "SERVICE_REQUEST", "APPROVAL_TASK", "APPROVAL_REQUEST", "WORKSPACE"
    ]
    source_reference: UUID
    expected_version: int = Field(ge=0, strict=True)
    obligation_key: str | None = Field(default=None, max_length=128)


class AskPageContext(ContractModel):
    route: str = Field(min_length=1, max_length=500, pattern=r"^/")
    app_key: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{1,99}$")
    surface: str | None = Field(default=None, max_length=100)
    entity_type: str | None = Field(default=None, max_length=80)
    entity_ref: str | None = Field(default=None, max_length=256)
    selected_work: AskSelectedWork | None = None

    @model_validator(mode="after")
    def validate_selected_work(self) -> "AskPageContext":
        if (self.surface == "selected-work-assist") != (self.selected_work is not None):
            raise ValueError("Selected work requires the selected-work-assist surface and binding.")
        if self.selected_work is not None and (
            self.entity_type != self.selected_work.source_system
            or self.entity_ref != str(self.selected_work.source_reference)
        ):
            raise ValueError("Selected work must match the page entity reference.")
        return self


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
    excerpt: str | None = Field(default=None, max_length=500)


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
    conversation_id: UUID | None = None
    user_message_id: UUID | None = None
    assistant_message_id: UUID | None = None
    selected_work: AskSelectedWork | None = None

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
            expected_status = grounded_status_for_provider(self.model_route.provider)
            if self.status_code != expected_status:
                raise ValueError("A completed Ask response must be grounded.")
        elif self.answer is not None or self.confidence is not None or self.citations:
            raise ValueError("A non-completed Ask response cannot expose an answer or citations.")
        return self


class AskEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Ask request evaluated."
    success: bool = True
    data: AskResponse


class ConversationSummary(ContractModel):
    conversation_id: UUID
    title: str = Field(min_length=1, max_length=160)
    locale: str = Field(pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
    message_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime


class ConversationMessage(ContractModel):
    message_id: UUID
    role: ConversationRole
    content: str = Field(min_length=1, max_length=8_000)
    run_id: UUID | None = None
    status_code: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_.-]{2,127}$",
    )
    citations: list[AskCitation] = Field(default_factory=list, max_length=20)
    agent_key: str = Field(default="DWP_ASSISTANT", max_length=80)
    selected_work: AskSelectedWork | None = None
    created_at: datetime


class ConversationDetail(ContractModel):
    summary: ConversationSummary
    messages: list[ConversationMessage] = Field(default_factory=list, max_length=200)


class ConversationListEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Conversations loaded."
    success: bool = True
    data: list[ConversationSummary]


class ConversationEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Conversation loaded."
    success: bool = True
    data: ConversationDetail


class RenameConversationRequest(ContractModel):
    title: str = Field(min_length=1, max_length=160)


class AnswerFeedbackRequest(ContractModel):
    rating: AnswerFeedbackRating
    reason_codes: list[str] = Field(default_factory=list, max_length=8)
    comment: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_reason_codes(self) -> "AnswerFeedbackRequest":
        normalized: list[str] = []
        for code in self.reason_codes:
            candidate = code.strip().upper()
            if not candidate or len(candidate) > 64 or not all(
                character.isalnum() or character in "_.-" for character in candidate
            ):
                raise ValueError("Feedback reason codes must use safe catalog identifiers.")
            if candidate not in normalized:
                normalized.append(candidate)
        self.reason_codes = normalized
        self.comment = self.comment.strip() if self.comment else None
        return self


class FeedbackReceipt(ContractModel):
    run_id: UUID
    rating: AnswerFeedbackRating
    recorded_at: datetime


class FeedbackEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Feedback recorded."
    success: bool = True
    data: FeedbackReceipt


class WorkplaceAction(ContractModel):
    action_key: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{2,127}$")
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=300)
    mode: WorkplaceActionMode
    risk_tier: RiskTier
    required_permission: str = Field(
        pattern=r"^[A-Z][A-Z0-9_.-]{1,99}:[A-Z][A-Z0-9_.-]{1,63}$"
    )
    target_route: str = Field(min_length=1, max_length=500, pattern=r"^/")
    confirmation_required: bool = True
    input_fields: list[str] = Field(default_factory=list, max_length=20)


class WorkplaceActionListEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Available actions loaded."
    success: bool = True
    data: list[WorkplaceAction]


class WorkplaceActionPreviewRequest(ContractModel):
    request_id: str = Field(min_length=1, max_length=128)
    inputs: dict[str, JsonValue] = Field(default_factory=dict, max_length=20)
    source_references: list[str] = Field(default_factory=list, max_length=20)
    origin: ActionHandoffOrigin


class WorkplaceActionPreview(ContractModel):
    action: WorkplaceAction
    reviewed_inputs: dict[str, JsonValue] = Field(default_factory=dict, max_length=20)
    plan: PlanPreviewResponse


class WorkplaceActionPreviewEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Action handoff preview prepared."
    success: bool = True
    data: WorkplaceActionPreview
