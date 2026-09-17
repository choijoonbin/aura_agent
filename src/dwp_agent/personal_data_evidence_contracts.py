from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, JsonValue, field_validator, model_validator

from .contract_model import ContractModel
from .governed_domain_contracts import HighRiskMutationCommand


class PersonalDataEvidenceAction(StrEnum):
    BACKUP_LEDGER = "BACKUP_LEDGER"
    SRE_ESCALATION = "SRE_ESCALATION"
    LEGAL_HOLD_APPEAL = "LEGAL_HOLD_APPEAL"
    SIGNED_CERTIFICATE = "SIGNED_CERTIFICATE"
    SIEM_SYNC = "SIEM_SYNC"


class PersonalDataEvidenceCommandState(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CreatePersonalDataEvidenceCommandRequest(HighRiskMutationCommand):
    parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=20)

    @field_validator("parameters")
    @classmethod
    def bounded_parameters(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for key in value:
            if not key or len(key) > 64 or not key.replace("_", "").isalnum():
                raise ValueError("Evidence command parameter names are invalid.")
        return value


class PersonalDataEvidenceCommand(ContractModel):
    command_id: UUID
    deletion_job_id: UUID
    action: PersonalDataEvidenceAction
    state: PersonalDataEvidenceCommandState
    expected_revision: int = Field(ge=0)
    receipt_id: UUID | None = None
    provider_receipt_id: str | None = Field(default=None, max_length=240)
    result_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result: dict[str, JsonValue] | None = None
    safe_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    recovery_hint: str | None = Field(default=None, max_length=1_000)
    download_available: bool = False
    created_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def coherent_terminal_evidence(self) -> "PersonalDataEvidenceCommand":
        if self.state == PersonalDataEvidenceCommandState.PENDING:
            if any(
                value is not None
                for value in (
                    self.receipt_id,
                    self.provider_receipt_id,
                    self.result_fingerprint,
                    self.result,
                    self.safe_error_code,
                    self.recovery_hint,
                    self.completed_at,
                )
            ) or self.download_available:
                raise ValueError("Pending evidence commands cannot expose terminal evidence.")
        elif self.state == PersonalDataEvidenceCommandState.COMPLETED:
            if not all(
                value is not None
                for value in (
                    self.receipt_id,
                    self.provider_receipt_id,
                    self.result_fingerprint,
                    self.result,
                    self.completed_at,
                )
            ) or self.safe_error_code is not None or self.recovery_hint is not None:
                raise ValueError("Completed evidence commands require bound provider evidence.")
        elif not all(
            value is not None
            for value in (
                self.receipt_id,
                self.result_fingerprint,
                self.result,
                self.safe_error_code,
                self.recovery_hint,
                self.completed_at,
            )
        ):
            raise ValueError("Failed evidence commands require a safe failure receipt.")
        if self.download_available != (
            self.state == PersonalDataEvidenceCommandState.COMPLETED
            and self.action == PersonalDataEvidenceAction.SIGNED_CERTIFICATE
        ):
            raise ValueError("Only completed signed certificates are downloadable.")
        return self


class PersonalDataEvidenceCommandEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: PersonalDataEvidenceCommand

