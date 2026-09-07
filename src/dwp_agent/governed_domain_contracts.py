from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator

from .contracts import ContractModel


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
    source_system_data_affected: bool = False
    audit_metadata_may_be_retained: bool = True
    proposal_clear_managed_separately: bool = True
    proposal_clear_route: str = "/v1/proposals/clear"
    analysis_receipt_clear_available: bool = False


class PersonalDataGovernanceCapabilitiesEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: PersonalDataGovernanceCapabilities


class UpsertRetentionPolicyRequest(HighRiskMutationCommand):
    retention_days: int = Field(ge=1, le=3_650)
    deletion_grace_days: int = Field(ge=0, le=90)
    legal_hold: bool = False


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


class RequestDeletionRequest(HighRiskMutationCommand):
    domains: list[DomainKey] = Field(min_length=1, max_length=4)

    @field_validator("domains")
    @classmethod
    def unique_domains(cls, value: list[DomainKey]) -> list[DomainKey]:
        if len(set(value)) != len(value):
            raise ValueError("Deletion domains must be unique.")
        return value


class DeletionJob(ContractModel):
    deletion_job_id: UUID
    state: DeletionJobState
    domains: list[DomainKey]
    requested_at: datetime
    completed_at: datetime | None = None
    deletion_performed: bool = False
    deletion_execution_available: bool = False
    blocked_domains: list[DomainKey] = Field(default_factory=list)


class DeletionJobEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: DeletionJob
