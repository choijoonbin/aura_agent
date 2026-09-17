from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel
from .dwaion_workflow_contracts import WorkflowCapability


class DomainKey(StrEnum):
    ROUTINE = "ROUTINE"
    MEMORY = "MEMORY"
    ARTIFACT = "ARTIFACT"
    ARTIFACT_EXPORT = "ARTIFACT_EXPORT"


class MutationCommand(ContractModel):
    command_id: UUID
    expected_revision: int = Field(ge=0)
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{1,63}$")


class HighRiskMutationCommand(MutationCommand):
    change_reason: str = Field(min_length=5, max_length=1_000)

    @field_validator("change_reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 5:
            raise ValueError("changeReason must explain the governed change.")
        return normalized


class RetentionPolicy(ContractModel):
    domain: DomainKey
    retention_days: int = Field(ge=1, le=3_650)
    deletion_grace_days: int = Field(ge=0, le=90)
    legal_hold: bool
    revision: int = Field(ge=1)
    updated_at: datetime


class RetentionPoliciesEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: list[RetentionPolicy]


class PersonalDataGovernanceCapabilities(ContractModel):
    supported_deletion_domains: list[DomainKey] = Field(
        default_factory=lambda: list(DomainKey)
    )
    deletion_request_available: bool = True
    deletion_execution_available: bool = False
    deletion_completion_claim_available: bool = False
    active_store_physical_purge_available: bool = False
    active_store_crypto_shred_available: bool = False
    backup_disposition_available: bool = False
    deletion_execution_scope: str = "AGENT_ACTIVE_POSTGRES_DOMAINS_ONLY"
    backup_disposition_state: str = "EXTERNAL_RETENTION_BOUNDARY"
    source_system_data_affected: bool = False
    audit_metadata_may_be_retained: bool = True
    proposal_clear_managed_separately: bool = True
    proposal_clear_route: str = "/v1/proposals/clear"
    analysis_receipt_clear_available: bool = False
    backup_destruction_log: WorkflowCapability
    sre_support: WorkflowCapability
    legal_hold_evidence: WorkflowCapability
    legal_hold_appeal: WorkflowCapability
    signed_certificate: WorkflowCapability
    siem_sync: WorkflowCapability


class PersonalDataGovernanceCapabilitiesEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: PersonalDataGovernanceCapabilities


class UpsertRetentionPolicyRequest(HighRiskMutationCommand):
    retention_days: int = Field(ge=1, le=3_650)
    deletion_grace_days: int = Field(ge=0, le=90)
    legal_hold: bool = False
    legal_hold_directive: "LegalHoldDirective | None" = None

    @model_validator(mode="after")
    def coherent_legal_hold(self) -> "UpsertRetentionPolicyRequest":
        if not self.legal_hold and self.legal_hold_directive is not None:
            raise ValueError("A released legal hold cannot include an active directive.")
        return self


class LegalHoldDirective(ContractModel):
    authority_reference: str = Field(min_length=1, max_length=160)
    dpo_subject_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9@._:-]{0,159}$",
    )
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{1,63}$")
    effective_at: datetime
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def coherent_lifecycle(self) -> "LegalHoldDirective":
        if self.effective_at.utcoffset() is None:
            raise ValueError("effectiveAt must include a time-zone offset.")
        if self.expires_at is not None:
            if self.expires_at.utcoffset() is None:
                raise ValueError("expiresAt must include a time-zone offset.")
            if self.expires_at <= self.effective_at:
                raise ValueError("expiresAt must be later than effectiveAt.")
        return self


class RetentionPolicyEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: RetentionPolicy


class DeletionJobState(StrEnum):
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    BLOCKED_LEGAL_HOLD = "BLOCKED_LEGAL_HOLD"
    FAILED = "FAILED"


class DeletionTargetState(StrEnum):
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    BLOCKED_LEGAL_HOLD = "BLOCKED_LEGAL_HOLD"
    FAILED = "FAILED"


class DeletionStageKey(StrEnum):
    REQUEST_ACCEPTED = "REQUEST_ACCEPTED"
    TARGETS_SCHEDULED = "TARGETS_SCHEDULED"
    ACTIVE_STORE_DISPOSITION = "ACTIVE_STORE_DISPOSITION"
    BACKUP_BOUNDARY = "BACKUP_BOUNDARY"
    RECEIPT_FINALIZATION = "RECEIPT_FINALIZATION"


class DeletionStageState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class RequestDeletionRequest(HighRiskMutationCommand):
    domains: list[DomainKey] = Field(min_length=1, max_length=4)

    @field_validator("domains")
    @classmethod
    def unique_domains(cls, value: list[DomainKey]) -> list[DomainKey]:
        if len(set(value)) != len(value):
            raise ValueError("Deletion domains must be unique.")
        return value


class RetryDeletionRequest(HighRiskMutationCommand):
    pass


class DataDispositionReceipt(ContractModel):
    disposition_id: UUID
    domain: DomainKey
    generation: int = Field(ge=1)
    purged_row_count: int = Field(ge=0)
    purged_table_counts: dict[str, int] = Field(default_factory=dict)
    disposition_scope: str = "AGENT_ACTIVE_POSTGRES_DOMAIN_ONLY"
    disposition_method: str = "PHYSICAL_ROW_PURGE_OF_ENCRYPTED_RECORDS"
    active_store_envelopes_destroyed: bool
    source_system_data_affected: bool = False
    backup_disposition_state: str = "EXTERNAL_RETENTION_BOUNDARY"
    receipt_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime

    @field_validator("purged_table_counts")
    @classmethod
    def valid_table_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not name.startswith("ai_") or count < 0 for name, count in value.items()):
            raise ValueError("Disposition table counts are invalid.")
        return value

    @model_validator(mode="after")
    def coherent_scope(self) -> "DataDispositionReceipt":
        if sum(self.purged_table_counts.values()) != self.purged_row_count:
            raise ValueError("Disposition table counts do not match the purged row count.")
        if (
            self.disposition_scope != "AGENT_ACTIVE_POSTGRES_DOMAIN_ONLY"
            or self.disposition_method != "PHYSICAL_ROW_PURGE_OF_ENCRYPTED_RECORDS"
            or not self.active_store_envelopes_destroyed
            or self.source_system_data_affected
            or self.backup_disposition_state != "EXTERNAL_RETENTION_BOUNDARY"
        ):
            raise ValueError("Disposition receipt exceeds the active-store boundary.")
        return self


class LegalHoldEvidence(ContractModel):
    available: bool
    domain: DomainKey
    hold_id: UUID | None = None
    state: str | None = Field(default=None, pattern=r"^(ACTIVE|RELEASED)$")
    authority_reference: str | None = Field(default=None, max_length=160)
    dpo_subject_id: str | None = Field(default=None, max_length=160)
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$")
    effective_at: datetime | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def coherent_evidence(self) -> "LegalHoldEvidence":
        required = (
            self.hold_id,
            self.state,
            self.authority_reference,
            self.dpo_subject_id,
            self.effective_at,
        )
        if self.available != all(value is not None for value in required):
            raise ValueError("Legal-hold availability and evidence must agree.")
        return self


class DeletionStage(ContractModel):
    key: DeletionStageKey
    state: DeletionStageState
    detail_code: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$")
    observed_at: datetime | None = None
    evidence_reference: str | None = Field(default=None, max_length=240)
    evidence_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class DeletionTargetReceipt(ContractModel):
    domain: DomainKey
    state: DeletionTargetState
    affected_count: int | None = Field(default=None, ge=0)
    safe_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    disposition: DataDispositionReceipt | None = None
    legal_hold_evidence: LegalHoldEvidence | None = None


class DeletionJob(ContractModel):
    deletion_job_id: UUID
    state: DeletionJobState
    domains: list[DomainKey]
    requested_at: datetime
    completed_at: datetime | None = None
    deletion_performed: bool = False
    deletion_execution_available: bool = False
    blocked_domains: list[DomainKey] = Field(default_factory=list)
    attempt_count: int = Field(default=0, ge=0)
    targets: list[DeletionTargetReceipt] = Field(default_factory=list, max_length=4)
    stages: list[DeletionStage] = Field(default_factory=list, max_length=5)
    legal_holds: list[LegalHoldEvidence] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def coherent_evidence(self) -> "DeletionJob":
        if self.stages and [stage.key for stage in self.stages] != list(DeletionStageKey):
            raise ValueError("Deletion evidence must contain the ordered five-stage boundary.")
        if len({hold.domain for hold in self.legal_holds}) != len(self.legal_holds):
            raise ValueError("Deletion legal-hold evidence must be unique by domain.")
        return self


class DeletionJobEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: DeletionJob


class DeletionJobsEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: list[DeletionJob]
