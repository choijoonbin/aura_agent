from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, model_validator

from .contracts import ContractModel
from .governed_domain_contracts import HighRiskMutationCommand


class TeamArtifactRemediationKind(StrEnum):
    AUTOMATIC_MASKING = "AUTOMATIC_MASKING"
    SYNTHETIC_REPLACEMENT = "SYNTHETIC_REPLACEMENT"
    REVIEW_NOTIFICATION = "REVIEW_NOTIFICATION"


class ExecuteTeamArtifactRemediationRequest(HighRiskMutationCommand):
    action: TeamArtifactRemediationKind
    stage_id: UUID | None = None

    @model_validator(mode="after")
    def bind_review_stage(self) -> "ExecuteTeamArtifactRemediationRequest":
        requires_stage = self.action == TeamArtifactRemediationKind.REVIEW_NOTIFICATION
        if requires_stage != (self.stage_id is not None):
            raise ValueError("Only review notification requires a review stage.")
        return self


class TeamArtifactRemediationReceipt(ContractModel):
    receipt_id: UUID
    command_id: UUID
    artifact_id: UUID
    action: TeamArtifactRemediationKind
    state: str = Field(pattern=r"^SUCCEEDED$")
    artifact_revision: int = Field(ge=1)
    workspace_revision: int | None = Field(default=None, ge=1)
    affected_count: int = Field(ge=0, le=100_000)
    provider_receipt_id: str | None = Field(default=None, min_length=1, max_length=240)
    source_content_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    finding_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    result_content_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    remediated_codes: list[str] = Field(default_factory=list, max_length=32)
    residual_finding_count: int | None = Field(default=None, ge=0, le=100_000)
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime

    @model_validator(mode="after")
    def provider_evidence_matches_action(self) -> "TeamArtifactRemediationReceipt":
        provider_action = self.action == TeamArtifactRemediationKind.REVIEW_NOTIFICATION
        content_evidence = (
            self.source_content_fingerprint,
            self.finding_manifest_sha256,
            self.result_content_sha256,
            self.residual_finding_count,
        )
        if provider_action:
            if self.provider_receipt_id is None or self.affected_count != 1:
                raise ValueError("Review notification requires one provider effect.")
            if any(value is not None for value in content_evidence) or self.remediated_codes:
                raise ValueError("Review notification cannot carry content remediation evidence.")
            return self
        if self.provider_receipt_id is not None:
            raise ValueError("Content remediation cannot carry a provider receipt.")
        if any(value is None for value in content_evidence):
            raise ValueError("Content remediation requires complete DLP evidence.")
        if self.affected_count < 1 or not self.remediated_codes:
            raise ValueError("Content remediation must change at least one current finding.")
        if len(set(self.remediated_codes)) != len(self.remediated_codes):
            raise ValueError("Remediated DLP codes must be unique.")
        if any(
            not code or code != code.strip() or len(code) > 64
            for code in self.remediated_codes
        ):
            raise ValueError("Remediated DLP codes must be canonical.")
        if self.residual_finding_count != 0:
            raise ValueError("Content remediation cannot succeed with residual findings.")
        return self


class TeamArtifactRemediationEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: TeamArtifactRemediationReceipt
