from __future__ import annotations
import hashlib
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, JsonValue, field_validator, model_validator

from .contract_model import ContractModel
from .proposal_handoff_receipt_contracts import ProposalHandoffCompletionReceipt
from .secure_attachment_contracts import (
    AttachmentCapabilities,
    AttachmentCapabilitiesEnvelope,
    AttachmentCitation,
    AttachmentEnvelope,
    AttachmentListEnvelope,
    AttachmentStage,
    AttachmentStageKey,
    AttachmentStageState,
    AttachmentState,
    AttachmentUploadTicket,
    CompleteAttachmentUploadRequest,
    CreateAttachmentRequest,
    DeleteAttachmentRequest,
    SecureAttachment,
)
from .workflow_capability_contracts import ResearchCapabilities, ResearchCapabilitiesEnvelope
from .workflow_capability_contracts import ResearchDeliveryCapabilities, WorkflowCapability


class ProposalHandoffState(StrEnum):
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    HANDED_OFF = "HANDED_OFF"
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"

class CreateProposalHandoffRequest(ContractModel):
    expected_version: int = Field(ge=1)
    command_id: UUID
    idempotency_key: UUID
    reviewed_inputs: dict[str, JsonValue] = Field(default_factory=dict, max_length=20)

class ProposalHandoff(ContractModel):
    handoff_id: UUID
    proposal_id: UUID
    action_key: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{0,127}$")
    state: ProposalHandoffState
    version: int = Field(ge=1)
    target_route: str = Field(min_length=1, max_length=1_000)
    approval_required: bool
    receipt_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

class ProposalHandoffEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Proposal handoff prepared."
    success: bool = True
    data: ProposalHandoff

class ProposalHandoffObservation(ContractModel):
    command_id: UUID
    expected_version: int = Field(ge=1)
    state: ProposalHandoffState
    receipt: ProposalHandoffCompletionReceipt | None = None

    @model_validator(mode="after")
    def completed_has_receipt(self) -> "ProposalHandoffObservation":
        if self.state == ProposalHandoffState.COMPLETED and not self.receipt:
            raise ValueError("A completed handoff requires a domain completion receipt.")
        if self.state != ProposalHandoffState.COMPLETED and self.receipt is not None:
            raise ValueError("A domain completion receipt is only accepted for COMPLETED.")
        return self

class ResearchPlanState(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    ARCHIVED = "ARCHIVED"

class ResearchRunState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    PARTIAL = "PARTIAL"
    CONFLICT = "CONFLICT"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"

class ResearchSourcePolicy(ContractModel):
    source_key: str = Field(pattern=r"^[A-Z][A-Z0-9_.:-]{0,127}$")
    allowed: bool
    scope: str = Field(min_length=1, max_length=500)

class ResearchBudget(ContractModel):
    maximum_minutes: int = Field(ge=1, le=240)
    maximum_sources: int = Field(ge=1, le=500)
    maximum_tokens: int = Field(ge=128, le=2_000_000)

class ResearchPlanDefinition(ContractModel):
    goal: str = Field(min_length=10, max_length=4_000)
    question: str = Field(min_length=10, max_length=4_000)
    success_criteria: list[str] = Field(min_length=1, max_length=20)
    deliverable_types: list[str] = Field(min_length=1, max_length=10)
    source_policies: list[ResearchSourcePolicy] = Field(min_length=1, max_length=100)
    require_all_allowed_sources: bool = False
    budget: ResearchBudget

    @field_validator("success_criteria")
    @classmethod
    def normalized_criteria(cls, value: list[str]) -> list[str]:
        normalized = [" ".join(item.split()) for item in value]
        if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("Research success criteria must be unique and non-empty.")
        return normalized

    @field_validator("deliverable_types")
    @classmethod
    def normalized_deliverables(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().upper() for item in value]
        allowed = {"REPORT", "EXECUTIVE_SUMMARY", "COMPARISON", "SOURCE_MAP"}
        if len(set(normalized)) != len(normalized) or not set(normalized) <= allowed:
            raise ValueError("Research deliverableTypes contain an unsupported value.")
        return normalized

class CreateResearchPlanRequest(ContractModel):
    command_id: UUID
    expected_revision: int = Field(default=0, ge=0, le=0)
    definition: ResearchPlanDefinition

class UpdateResearchPlanRequest(ContractModel):
    command_id: UUID
    expected_revision: int = Field(ge=1)
    definition: ResearchPlanDefinition

class ResearchPlan(ContractModel):
    plan_id: UUID
    state: ResearchPlanState
    revision: int = Field(ge=1)
    definition: ResearchPlanDefinition
    created_at: datetime
    updated_at: datetime

class ResearchPlanEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Research plan loaded."
    success: bool = True
    data: ResearchPlan

class StartResearchRunRequest(ContractModel):
    command_id: UUID
    expected_plan_revision: int = Field(ge=1)
    idempotency_key: UUID

class ExecuteResearchRunRequest(ContractModel):
    command_id: UUID
    expected_version: int = Field(ge=1)

class ResearchRunCommandAction(StrEnum):
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    EXCLUDE_SOURCE_AND_CONTINUE = "EXCLUDE_SOURCE_AND_CONTINUE"
    REPROBE_SOURCE = "REPROBE_SOURCE"
    SAFE_CANCEL = "SAFE_CANCEL"
    EXTEND = "EXTEND"

class ResearchRunCommandRequest(ContractModel):
    command_id: UUID
    expected_version: int = Field(ge=1)
    action: ResearchRunCommandAction
    reason: str = Field(min_length=5, max_length=500)
    source_key: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.:-]{0,127}$"
    )
    extension_minutes: int | None = Field(default=None, ge=1, le=120)

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 5:
            raise ValueError("Research command reason must contain at least 5 characters.")
        return normalized

    @model_validator(mode="after")
    def coherent_action(self) -> "ResearchRunCommandRequest":
        if self.action in {
            ResearchRunCommandAction.EXCLUDE_SOURCE_AND_CONTINUE,
            ResearchRunCommandAction.REPROBE_SOURCE,
        } and not self.source_key:
            raise ValueError("sourceKey is required for the selected research command.")
        if self.action == ResearchRunCommandAction.EXTEND and self.extension_minutes is None:
            raise ValueError("extensionMinutes is required for EXTEND.")
        return self

class ResearchProgress(ContractModel):
    completed_steps: int = Field(ge=0)
    total_steps: int = Field(ge=0)
    discovered_sources: int = Field(ge=0)
    verified_citations: int = Field(ge=0)
    failed_sources: list[str] = Field(default_factory=list, max_length=100)
    recovery_hint: str | None = Field(default=None, max_length=500)

class ResearchResult(ContractModel):
    report_markdown: str = Field(min_length=1, max_length=1_000_000)
    citations: list[AttachmentCitation] = Field(min_length=1, max_length=5_000)
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def verified_result_hash(self) -> "ResearchResult":
        if hashlib.sha256(self.report_markdown.encode("utf-8")).hexdigest() != self.result_sha256:
            raise ValueError("Research resultSha256 does not match reportMarkdown.")
        return self

class ResearchRun(ContractModel):
    run_id: UUID
    plan_id: UUID
    plan_revision: int = Field(ge=1)
    state: ResearchRunState
    version: int = Field(ge=1)
    progress: ResearchProgress
    result: ResearchResult | None = None
    receipt_id: UUID | None = None
    safe_error_code: str | None = None
    started_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

class ResearchRunEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Research run loaded."
    success: bool = True
    data: ResearchRun

class ResearchWorkerObservation(ContractModel):
    command_id: UUID
    expected_version: int = Field(ge=1)
    state: ResearchRunState
    progress: ResearchProgress
    result: ResearchResult | None = None
    safe_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )

    @model_validator(mode="after")
    def completed_has_evidence(self) -> "ResearchWorkerObservation":
        if self.state == ResearchRunState.COMPLETED and self.result is None:
            raise ValueError("COMPLETED research requires a citation-backed result.")
        if self.state != ResearchRunState.COMPLETED and self.result is not None:
            raise ValueError("Research results are sealed only when the run completes.")
        return self

class ResearchDeliveryType(StrEnum):
    ARTIFACT = "ARTIFACT"
    PROPOSAL = "PROPOSAL"
    EXPORT = "EXPORT"
    HANDOFF = "HANDOFF"
    SHARE = "SHARE"
    ROUTINE = "ROUTINE"

class ResearchDeliveryState(StrEnum):
    QUEUED = "QUEUED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class CreateResearchDeliveryRequest(ContractModel):
    command_id: UUID
    expected_version: int = Field(ge=1)
    idempotency_key: UUID
    parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=30)

class ResearchDelivery(ContractModel):
    delivery_id: UUID
    run_id: UUID
    delivery_type: ResearchDeliveryType
    state: ResearchDeliveryState
    receipt_id: UUID | None = None
    receipt: dict[str, JsonValue] | None = None
    safe_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    recovery_hint: str | None = Field(default=None, min_length=1, max_length=500)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

class ResearchDeliveryObservation(ContractModel):
    command_id: UUID
    state: ResearchDeliveryState
    receipt_id: UUID | None = None
    receipt: dict[str, JsonValue] | None = None
    safe_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    recovery_hint: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def completed_has_receipt(self) -> "ResearchDeliveryObservation":
        completed = self.state == ResearchDeliveryState.COMPLETED
        incomplete = self.state in {
            ResearchDeliveryState.PARTIAL,
            ResearchDeliveryState.FAILED,
        }
        has_receipt = self.receipt_id is not None or self.receipt is not None
        has_recovery = self.safe_error_code is not None or self.recovery_hint is not None
        if completed and (self.receipt_id is None or not self.receipt):
            raise ValueError("A completed delivery requires a target-system receipt.")
        if not completed and has_receipt:
            raise ValueError("A receipt is only accepted for a completed delivery.")
        if completed and has_recovery:
            raise ValueError("A completed delivery cannot include recovery evidence.")
        if incomplete and (self.safe_error_code is None or self.recovery_hint is None):
            raise ValueError("An incomplete delivery requires safe recovery evidence.")
        if not incomplete and has_recovery:
            raise ValueError("Recovery evidence is only accepted for an incomplete delivery.")
        return self

class ResearchDeliveryEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Research delivery requested."
    success: bool = True
    data: ResearchDelivery

class ResearchDeliveryListEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Research deliveries loaded."
    success: bool = True
    data: list[ResearchDelivery]
