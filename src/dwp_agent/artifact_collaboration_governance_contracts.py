from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, model_validator

from .contracts import ContractModel
from .dwaion_workflow_contracts import WorkflowCapability


class TeamArtifactReviewStageKey(StrEnum):
    AUTHOR = "AUTHOR"
    PRIMARY_REVIEW = "PRIMARY_REVIEW"
    FINAL_APPROVAL = "FINAL_APPROVAL"


class TeamArtifactReviewStageState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    UNAVAILABLE = "UNAVAILABLE"


class TeamArtifactReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class TeamArtifactGovernanceGateKey(StrEnum):
    DLP = "DLP"
    CITATION = "CITATION"
    RECIPIENT_ACL = "RECIPIENT_ACL"
    IMMUTABLE_VERSION = "IMMUTABLE_VERSION"


class TeamArtifactGovernanceGateState(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"


class TeamArtifactReviewStage(ContractModel):
    stage_id: UUID
    stage_order: int = Field(ge=1, le=3)
    stage_key: TeamArtifactReviewStageKey
    assignee_subject_id: str | None = Field(default=None, max_length=160)
    state: TeamArtifactReviewStageState
    revision: int = Field(ge=1)
    evidence_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    decided_by_subject_id: str | None = Field(default=None, max_length=160)
    decided_at: datetime | None = None

    @model_validator(mode="after")
    def coherent_stage(self) -> "TeamArtifactReviewStage":
        decided = self.state in {
            TeamArtifactReviewStageState.APPROVED,
            TeamArtifactReviewStageState.REJECTED,
        }
        if decided != all(
            value is not None
            for value in (
                self.assignee_subject_id,
                self.evidence_fingerprint,
                self.decided_by_subject_id,
                self.decided_at,
            )
        ):
            raise ValueError("Decided review stages require complete decision evidence.")
        if self.state == TeamArtifactReviewStageState.PENDING and self.assignee_subject_id is None:
            raise ValueError("Pending review stages require an assignee.")
        if self.state == TeamArtifactReviewStageState.UNAVAILABLE and self.assignee_subject_id is not None:
            raise ValueError("Unavailable review stages cannot claim an assignee.")
        return self


class TeamArtifactGovernanceGate(ContractModel):
    key: TeamArtifactGovernanceGateKey
    state: TeamArtifactGovernanceGateState
    detail_code: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$")
    evidence_reference: str | None = Field(default=None, max_length=240)
    evidence_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    evaluated_at: datetime | None = None


class TeamArtifactSignatureEvidence(ContractModel):
    capability: WorkflowCapability
    provider: str | None = Field(default=None, max_length=80)
    key_reference_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    signature: str | None = Field(default=None, max_length=2_000)
    signed_at: datetime | None = None

    @model_validator(mode="after")
    def coherent_signature(self) -> "TeamArtifactSignatureEvidence":
        evidence = (
            self.provider,
            self.key_reference_fingerprint,
            self.signature,
            self.signed_at,
        )
        if self.capability.available != all(value is not None for value in evidence):
            raise ValueError("Signature capability and evidence must agree.")
        return self


def unavailable_artifact_signature_evidence() -> TeamArtifactSignatureEvidence:
    return TeamArtifactSignatureEvidence(
        capability=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ARTIFACT_SIGNED_WORM_RECEIPT_NOT_CONFIGURED",
            recovery_hint="Configure an attested immutable-ledger provider and tenant signing key.",
        )
    )


class TeamArtifactCapabilities(ContractModel):
    team_workspace_available: bool
    acl_preflight_available: bool
    access_request_available: bool
    collaboration_available: bool
    conflict_resolution_available: bool
    internal_sharing_available: bool
    external_sharing_available: bool = False
    share_expiry_available: bool
    share_revocation_available: bool
    inline_comments: WorkflowCapability
    staged_review: WorkflowCapability = Field(
        default_factory=lambda: WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ARTIFACT_STAGED_REVIEW_NOT_AVAILABLE",
            recovery_hint="Configure governed artifact collaboration before using staged review.",
        )
    )
    signed_worm_receipt: WorkflowCapability = Field(
        default_factory=lambda: WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ARTIFACT_SIGNED_WORM_RECEIPT_NOT_CONFIGURED",
            recovery_hint="Configure an attested immutable-ledger provider and tenant signing key.",
        )
    )
    automatic_masking: WorkflowCapability
    synthetic_replacement: WorkflowCapability
    review_notification: WorkflowCapability
    review_rejection: WorkflowCapability
    provider_state: str
    recovery_hint: str | None = None


