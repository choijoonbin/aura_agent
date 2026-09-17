from __future__ import annotations
import hashlib

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, JsonValue, field_validator, model_validator

from .contract_model import ContractModel

class WorkflowCapability(ContractModel):
    available: bool
    configured: bool
    reason_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    recovery_hint: str | None = Field(default=None, max_length=500)

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
    receipt: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def completed_has_receipt(self) -> "ProposalHandoffObservation":
        if self.state == ProposalHandoffState.COMPLETED and not self.receipt:
            raise ValueError("A completed handoff requires a domain completion receipt.")
        if self.state != ProposalHandoffState.COMPLETED and self.receipt is not None:
            raise ValueError("A domain completion receipt is only accepted for COMPLETED.")
        return self

class AttachmentState(StrEnum):
    UPLOADING = "UPLOADING"
    SCANNING = "SCANNING"
    READY = "READY"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DELETION_PENDING = "DELETION_PENDING"
    DELETED = "DELETED"

class AttachmentStageKey(StrEnum):
    UPLOAD = "UPLOAD"
    AV = "AV"
    DLP = "DLP"
    PARSER = "PARSER"
    OCR = "OCR"
    INDEX = "INDEX"

class AttachmentStageState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    NOT_REQUIRED = "NOT_REQUIRED"
    NOT_CONFIGURED = "NOT_CONFIGURED"

class AttachmentStage(ContractModel):
    key: AttachmentStageKey
    state: AttachmentStageState
    provider_code: str | None = Field(default=None, max_length=80)
    observed_at: datetime | None = None
    safe_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    recovery_hint: str | None = Field(default=None, max_length=500)

class AttachmentCitation(ContractModel):
    citation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
    locator: str = Field(min_length=1, max_length=500)
    label: str = Field(min_length=1, max_length=240)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def verified_content_hash(self) -> "AttachmentCitation":
        if hashlib.sha256(self.evidence.encode("utf-8")).hexdigest() != self.content_sha256:
            raise ValueError("Attachment citation contentSha256 does not match its evidence.")
        return self

class AttachmentCapabilities(ContractModel):
    upload: WorkflowCapability
    antivirus: WorkflowCapability
    dlp: WorkflowCapability
    parser: WorkflowCapability
    ocr: WorkflowCapability
    index: WorkflowCapability
    deletion: WorkflowCapability
    detach_all: WorkflowCapability
    inspection_log: WorkflowCapability
    masking_history: WorkflowCapability
    ocr_viewer: WorkflowCapability
    signed_audit_report: WorkflowCapability
    maximum_file_bytes: int = Field(ge=1)
    allowed_media_types: list[str]

class AttachmentCapabilitiesEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Secure attachment capabilities loaded."
    success: bool = True
    data: AttachmentCapabilities

class AttachmentUploadTicket(ContractModel):
    method: str = "PUT"
    upload_url: str = Field(min_length=8, max_length=8_192)
    upload_reference: str = Field(min_length=8, max_length=1_000)
    expires_at: datetime

class CreateAttachmentRequest(ContractModel):
    command_id: UUID
    expected_revision: int = Field(default=0, ge=0, le=0)
    conversation_id: UUID | None = None
    file_name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=3, max_length=120)
    size_bytes: int = Field(ge=1, le=104_857_600)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retention_hours: int = Field(default=24, ge=1, le=720)

    @field_validator("file_name")
    @classmethod
    def safe_file_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(part in normalized for part in ("/", "\\", "\x00")):
            raise ValueError("Attachment fileName must be a leaf name.")
        return normalized

class CompleteAttachmentUploadRequest(ContractModel):
    command_id: UUID
    expected_revision: int = Field(ge=1)
    upload_reference: str = Field(min_length=8, max_length=1_000)
    observed_size_bytes: int = Field(ge=1, le=104_857_600)
    observed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("upload_reference")
    @classmethod
    def opaque_reference(cls, value: str) -> str:
        normalized = value.strip()
        if "://" in normalized or "?" in normalized or "#" in normalized:
            raise ValueError("Upload references must be opaque provider identifiers.")
        return normalized

class DeleteAttachmentRequest(ContractModel):
    command_id: UUID
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=5, max_length=500)

class AttachmentWorkerObservation(ContractModel):
    command_id: UUID
    expected_revision: int = Field(ge=1)
    stages: list[AttachmentStage] = Field(min_length=1, max_length=6)
    citations: list[AttachmentCitation] = Field(default_factory=list, max_length=2_000)
    provider_deleted: bool = False

    @model_validator(mode="after")
    def unique_stages(self) -> "AttachmentWorkerObservation":
        if len({stage.key for stage in self.stages}) != len(self.stages):
            raise ValueError("Attachment stage keys must be unique.")
        return self

class SecureAttachment(ContractModel):
    attachment_id: UUID
    conversation_id: UUID | None = None
    file_name: str
    media_type: str
    size_bytes: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1)
    state: AttachmentState
    stages: list[AttachmentStage]
    citations: list[AttachmentCitation] = Field(default_factory=list)
    retention_expires_at: datetime
    capabilities: AttachmentCapabilities
    upload_ticket: AttachmentUploadTicket | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

class AttachmentEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Secure attachment loaded."
    success: bool = True
    data: SecureAttachment

class AttachmentListEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Secure attachments loaded."
    success: bool = True
    data: list[SecureAttachment]

class ResearchCapabilities(ContractModel):
    raw_export: WorkflowCapability
    pdf_export: WorkflowCapability
    receipt_download: WorkflowCapability
    audit_download: WorkflowCapability
    fork: WorkflowCapability
    merge: WorkflowCapability
    keep_local: WorkflowCapability
    sensitivity_recalculation: WorkflowCapability
    cache_fallback: WorkflowCapability

class ResearchCapabilitiesEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Research capabilities loaded."
    success: bool = True
    data: ResearchCapabilities

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
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

class ResearchDeliveryObservation(ContractModel):
    command_id: UUID
    state: ResearchDeliveryState
    receipt_id: UUID | None = None
    receipt: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def completed_has_receipt(self) -> "ResearchDeliveryObservation":
        if self.state == ResearchDeliveryState.COMPLETED and (
            self.receipt_id is None or not self.receipt
        ):
            raise ValueError("A completed delivery requires a target-system receipt.")
        if self.state != ResearchDeliveryState.COMPLETED and (
            self.receipt_id is not None or self.receipt is not None
        ):
            raise ValueError("A receipt is only accepted for a completed delivery.")
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
