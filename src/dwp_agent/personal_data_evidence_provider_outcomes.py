from __future__ import annotations

import hashlib
from typing import Literal, Mapping

from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator

from .contract_model import ContractModel
from .canonical_json import canonical_json_bytes
from .personal_data_evidence_contracts import PersonalDataEvidenceAction


class BackupLedgerOutcome(ContractModel):
    schema_version: Literal[1]
    ledger_scope: str = Field(min_length=1, max_length=120)
    destroyed_partition_ids: list[str] = Field(min_length=0, max_length=500)
    retained_partition_ids: list[str] = Field(min_length=0, max_length=500)
    ledger_entry_ids: list[str] = Field(min_length=1, max_length=1_000)
    observed_at: AwareDatetime

    @field_validator(
        "ledger_scope",
        "destroyed_partition_ids",
        "retained_partition_ids",
        "ledger_entry_ids",
    )
    @classmethod
    def normalized_text(cls, value: str | list[str]) -> str | list[str]:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("Backup ledger scope cannot be blank.")
            return normalized
        normalized_values = [item.strip() for item in value]
        if any(not item or len(item) > 240 for item in normalized_values):
            raise ValueError("Backup ledger identifiers must be non-empty and bounded.")
        if len(set(normalized_values)) != len(normalized_values):
            raise ValueError("Backup ledger identifiers must be unique.")
        return normalized_values

    @model_validator(mode="after")
    def coherent_partitions(self) -> "BackupLedgerOutcome":
        destroyed = set(self.destroyed_partition_ids)
        retained = set(self.retained_partition_ids)
        if not destroyed and not retained:
            raise ValueError("Backup ledger evidence must describe at least one partition.")
        if destroyed & retained:
            raise ValueError("A backup partition cannot be both destroyed and retained.")
        return self


class SreEscalationOutcome(ContractModel):
    schema_version: Literal[1]
    case_id: str = Field(min_length=1, max_length=240)
    queue: str = Field(min_length=1, max_length=240)
    severity: Literal["P0", "P1", "P2", "P3", "P4"]
    state: Literal["ACCEPTED"]
    accepted_at: AwareDatetime

    @field_validator("case_id", "queue")
    @classmethod
    def normalized_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("SRE escalation evidence cannot be blank.")
        return normalized


class LegalHoldAppealOutcome(ContractModel):
    schema_version: Literal[1]
    hold_id: str = Field(min_length=1, max_length=240)
    appeal_id: str = Field(min_length=1, max_length=240)
    request_parameters_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["SUBMITTED"]
    submitted_at: AwareDatetime

    @field_validator("hold_id", "appeal_id")
    @classmethod
    def normalized_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Legal-hold appeal evidence cannot be blank.")
        return normalized


class SiemSyncOutcome(ContractModel):
    schema_version: Literal[1]
    sync_id: str = Field(min_length=1, max_length=240)
    destination: str = Field(min_length=1, max_length=240)
    accepted_event_count: int = Field(ge=1, le=1_000_000)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_at: AwareDatetime

    @field_validator("sync_id", "destination")
    @classmethod
    def normalized_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("SIEM synchronization evidence cannot be blank.")
        return normalized


_OUTCOME_MODELS = {
    PersonalDataEvidenceAction.BACKUP_LEDGER: BackupLedgerOutcome,
    PersonalDataEvidenceAction.SRE_ESCALATION: SreEscalationOutcome,
    PersonalDataEvidenceAction.LEGAL_HOLD_APPEAL: LegalHoldAppealOutcome,
    PersonalDataEvidenceAction.SIEM_SYNC: SiemSyncOutcome,
}


def validate_personal_data_provider_outcome(
    action: PersonalDataEvidenceAction,
    result: Mapping[str, JsonValue],
    *,
    evidence_digest: str,
    parameters: Mapping[str, object] | None = None,
    evidence: Mapping[str, object] | None = None,
) -> dict[str, JsonValue]:
    if action == PersonalDataEvidenceAction.SIGNED_CERTIFICATE:
        return dict(result)
    model_type = _OUTCOME_MODELS[action]
    outcome = model_type.model_validate(result)
    normalized = outcome.model_dump(mode="json", by_alias=True)
    requested = parameters or {}
    if isinstance(outcome, BackupLedgerOutcome):
        ledger_scope = requested.get("ledgerScope")
        if ledger_scope is not None and outcome.ledger_scope != ledger_scope:
            raise ValueError("Backup ledger scope is not bound to the governed request.")
    elif isinstance(outcome, SreEscalationOutcome):
        priority = requested.get("priority")
        if priority is not None and outcome.severity != priority:
            raise ValueError("SRE escalation severity is not bound to the governed request.")
    elif isinstance(outcome, LegalHoldAppealOutcome):
        if parameters is not None:
            expected_parameters_sha256 = hashlib.sha256(
                canonical_json_bytes(dict(parameters))
            ).hexdigest()
            if outcome.request_parameters_sha256 != expected_parameters_sha256:
                raise ValueError(
                    "Legal-hold appeal parameters are not bound to the governed request."
                )
        if evidence is not None and outcome.hold_id not in _active_hold_ids(evidence):
            raise ValueError(
                "Legal-hold appeal is not bound to an active hold in the deletion evidence."
            )
    elif isinstance(outcome, SiemSyncOutcome):
        destination = requested.get("destination")
        if destination is not None and outcome.destination != destination:
            raise ValueError("SIEM destination is not bound to the governed request.")
        if outcome.source_digest != evidence_digest:
            raise ValueError("SIEM synchronization is not bound to the source evidence digest.")
    return normalized


def _active_hold_ids(evidence: Mapping[str, object]) -> set[str]:
    deletion_job = evidence.get("deletionJob")
    if not isinstance(deletion_job, Mapping):
        return set()
    holds = deletion_job.get("legalHolds")
    if not isinstance(holds, list):
        return set()
    return {
        hold_id
        for hold in holds
        if isinstance(hold, Mapping)
        and hold.get("available") is True
        and hold.get("state") == "ACTIVE"
        and isinstance((hold_id := hold.get("holdId")), str)
        and hold_id
    }
