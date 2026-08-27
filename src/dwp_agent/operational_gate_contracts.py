from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, model_validator

from .contracts import ContractModel


class GateEnvironment(StrEnum):
    DEVELOPMENT = "DEVELOPMENT"
    STAGING = "STAGING"
    PRODUCTION = "PRODUCTION"


class GateCategory(StrEnum):
    AI_RUNTIME = "AI_RUNTIME"
    CONNECTIVITY = "CONNECTIVITY"
    ACCESS_CONTROL = "ACCESS_CONTROL"
    ASSURANCE = "ASSURANCE"
    DATA_PROTECTION = "DATA_PROTECTION"
    OPERATIONS = "OPERATIONS"


class OperationalGateKey(StrEnum):
    MODEL_CREDENTIALS = "MODEL_CREDENTIALS"
    MODEL_LIFECYCLE_CAPACITY = "MODEL_LIFECYCLE_CAPACITY"
    NETWORK_ISOLATION = "NETWORK_ISOLATION"
    DATA_PROCESSING_LOCATION = "DATA_PROCESSING_LOCATION"
    SOURCE_CONNECTORS = "SOURCE_CONNECTORS"
    SOURCE_ACL = "SOURCE_ACL"
    DATA_CLASSIFICATION_DLP = "DATA_CLASSIFICATION_DLP"
    EVALUATION_DATASET = "EVALUATION_DATASET"
    RELEASE_APPROVAL = "RELEASE_APPROVAL"
    ACTION_APPROVAL = "ACTION_APPROVAL"
    TENANT_KMS = "TENANT_KMS"
    RETENTION_LEGAL_HOLD = "RETENTION_LEGAL_HOLD"
    AUDIT_RESILIENCE = "AUDIT_RESILIENCE"


class GateStatus(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONFIGURING = "CONFIGURING"
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"


class GateEvidenceType(StrEnum):
    CONFIGURATION_REFERENCE = "CONFIGURATION_REFERENCE"
    TEST_RESULT = "TEST_RESULT"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    LEGAL_APPROVAL = "LEGAL_APPROVAL"
    BUSINESS_APPROVAL = "BUSINESS_APPROVAL"
    RUNBOOK = "RUNBOOK"
    OTHER = "OTHER"


class GateValidationOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class GateDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class GateApprovalEligibilityReason(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    NOT_READY_FOR_APPROVAL = "NOT_READY_FOR_APPROVAL"
    SEPARATION_OF_DUTY = "SEPARATION_OF_DUTY"


class GateActorRole(StrEnum):
    OWNER = "OWNER"
    CONFIGURATOR = "CONFIGURATOR"
    VALIDATOR = "VALIDATOR"


class GateAuditOutcome(StrEnum):
    SUCCESS = "SUCCESS"


class OperationalGateProblemCode(StrEnum):
    PERMISSION_DENIED = "GATE_PERMISSION_DENIED"
    NOT_FOUND = "GATE_NOT_FOUND"
    VERSION_CONFLICT = "GATE_VERSION_CONFLICT"
    INVALID_TRANSITION = "GATE_INVALID_TRANSITION"
    REQUIRED_EVIDENCE_MISSING = "GATE_REQUIRED_EVIDENCE_MISSING"
    SEPARATION_OF_DUTY = "GATE_SEPARATION_OF_DUTY"
    STORE_UNAVAILABLE = "GATE_STORE_UNAVAILABLE"


class OperationalGateOption(ContractModel):
    code: str
    recommended: bool = False


class OperationalGateSummary(ContractModel):
    gate_key: OperationalGateKey
    category: GateCategory
    external_owner: str
    delivery_critical: bool
    selected_option: str | None = None
    options: list[OperationalGateOption]
    required_evidence_types: list[GateEvidenceType]
    status: GateStatus
    owner_user_id: str | None = None
    configuration_ref: str | None = None
    notes: str | None = None
    validation_summary: str | None = None
    last_configured_by: str | None = None
    last_validated_by: str | None = None
    approved_by: str | None = None
    evidence_count: int = Field(ge=0)
    configuration_revision: int = Field(ge=0)
    policy_version: int = Field(ge=1)
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    updated_at: datetime


class OperationalGateEvidence(ContractModel):
    evidence_id: UUID
    evidence_type: GateEvidenceType
    title: str
    reference: str
    checksum_sha256: str | None = None
    notes: str | None = None
    created_by: str
    created_at: datetime


class OperationalGateApprovalEligibility(ContractModel):
    eligible: bool
    reason: GateApprovalEligibilityReason
    conflicting_role: GateActorRole | None = None


class OperationalGateAuditEvent(ContractModel):
    event_id: UUID
    event_type: str
    outcome: GateAuditOutcome = GateAuditOutcome.SUCCESS
    actor_user_id: str
    correlation_id: str
    change_reason: str | None = None
    previous_status: GateStatus | None = None
    current_status: GateStatus | None = None
    created_at: datetime


class OperationalGateDetail(ContractModel):
    gate: OperationalGateSummary
    evidence: list[OperationalGateEvidence]
    missing_evidence_types: list[GateEvidenceType]
    approval_eligibility: OperationalGateApprovalEligibility
    events: list[OperationalGateAuditEvent]


class OperationalGatePortfolio(ContractModel):
    environment: GateEnvironment
    total_count: int = Field(ge=0)
    required_count: int = Field(ge=0)
    approved_count: int = Field(ge=0)
    ready_for_approval_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    expired_count: int = Field(ge=0)
    completion_percent: int = Field(ge=0, le=100)
    delivery_ready: bool
    gates: list[OperationalGateSummary]


class BootstrapOperationalGatesRequest(ContractModel):
    idempotency_key: UUID
    expected_existing_count: int = Field(ge=0, le=100)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "BootstrapOperationalGatesRequest":
        self.change_reason = self.change_reason.strip()
        _reject_secret_material(self.change_reason)
        return self


class ConfigureOperationalGateRequest(ContractModel):
    selected_option: str = Field(min_length=2, max_length=80, pattern=r"^[A-Z][A-Z0-9_]+$")
    owner_user_id: str = Field(min_length=1, max_length=160)
    configuration_ref: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "ConfigureOperationalGateRequest":
        self.selected_option = self.selected_option.strip().upper()
        self.owner_user_id = self.owner_user_id.strip()
        self.configuration_ref = _optional_text(self.configuration_ref)
        self.notes = _optional_text(self.notes)
        self.change_reason = self.change_reason.strip()
        _reject_secret_material(self.configuration_ref, self.notes, self.change_reason)
        return self


class CreateOperationalGateEvidenceRequest(ContractModel):
    evidence_type: GateEvidenceType
    title: str = Field(min_length=2, max_length=200)
    reference: str = Field(min_length=3, max_length=500)
    checksum_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    notes: str | None = Field(default=None, max_length=1000)
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "CreateOperationalGateEvidenceRequest":
        self.title = self.title.strip()
        self.reference = self.reference.strip()
        self.checksum_sha256 = self.checksum_sha256.lower() if self.checksum_sha256 else None
        self.notes = _optional_text(self.notes)
        self.change_reason = self.change_reason.strip()
        _reject_secret_material(self.reference, self.notes, self.change_reason)
        return self


class ValidateOperationalGateRequest(ContractModel):
    outcome: GateValidationOutcome
    validation_summary: str = Field(min_length=10, max_length=1000)
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "ValidateOperationalGateRequest":
        self.validation_summary = self.validation_summary.strip()
        self.change_reason = self.change_reason.strip()
        _reject_secret_material(self.validation_summary, self.change_reason)
        return self


class DecideOperationalGateRequest(ContractModel):
    decision: GateDecision
    valid_days: int = Field(default=365, ge=1, le=730)
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def normalize(self) -> "DecideOperationalGateRequest":
        self.change_reason = self.change_reason.strip()
        _reject_secret_material(self.change_reason)
        return self


class OperationalGatePortfolioEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON operational gates loaded."
    success: bool = True
    data: OperationalGatePortfolio


class OperationalGateDetailEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "DWAI-ON operational gate loaded."
    success: bool = True
    data: OperationalGateDetail


class OperationalGateProblem(ContractModel):
    type: str
    title: str
    status: int = Field(ge=400, le=599)
    detail: str
    code: OperationalGateProblemCode
    instance: str
    correlation_id: str
    context: dict[str, str | list[str]] = Field(default_factory=dict)


_SECRET_PATTERN = re.compile(
    r"(?i)(?:api[_ -]?key|password|client[_ -]?secret|access[_ -]?token|bearer)\s*[:=]\s*\S+"
)


def _optional_text(value: str | None) -> str | None:
    normalized = value.strip() if value else ""
    return normalized or None


def _reject_secret_material(*values: str | None) -> None:
    for value in values:
        if not value:
            continue
        if _SECRET_PATTERN.search(value) or "-----BEGIN PRIVATE KEY-----" in value.upper():
            raise ValueError("Operational gate records accept references, not secret material.")
