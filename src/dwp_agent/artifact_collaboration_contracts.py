from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .artifact_contracts import ArtifactDraftContent, ArtifactSourceReference
from .artifact_collaboration_governance_contracts import (
    TeamArtifactGovernanceGate,
    TeamArtifactGovernanceGateKey,
    TeamArtifactGovernanceGateState,
    TeamArtifactCapabilities,
    TeamArtifactReviewDecision,
    TeamArtifactReviewStage,
    TeamArtifactReviewStageKey,
    TeamArtifactReviewStageState,
    TeamArtifactSignatureEvidence,
    unavailable_artifact_signature_evidence,
)
from .contracts import ContractModel
from .dwaion_workflow_contracts import WorkflowCapability
from .governed_domain_contracts import HighRiskMutationCommand, MutationCommand


class TeamArtifactRole(StrEnum):
    OWNER = "OWNER"
    EDITOR = "EDITOR"
    REVIEWER = "REVIEWER"
    VIEWER = "VIEWER"


class TeamArtifactWorkspaceState(StrEnum):
    ACTIVE = "ACTIVE"
    READ_ONLY = "READ_ONLY"
    REVOKED = "REVOKED"


class TeamArtifactPreflightState(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    EXPIRED = "EXPIRED"


class TeamArtifactAccessRequestState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


class TeamArtifactConflictState(StrEnum):
    OPEN = "OPEN"
    RESOLVED_LOCAL = "RESOLVED_LOCAL"
    RESOLVED_SERVER = "RESOLVED_SERVER"
    RESOLVED_MERGED = "RESOLVED_MERGED"
    STASHED = "STASHED"
    ROLLED_BACK = "ROLLED_BACK"


class TeamArtifactConflictResolution(StrEnum):
    USE_LOCAL = "USE_LOCAL"
    USE_SERVER = "USE_SERVER"
    MERGE = "MERGE"
    STASH = "STASH"
    ROLLBACK = "ROLLBACK"


class TeamArtifactShareState(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class TeamArtifactSharePermission(StrEnum):
    VIEW = "VIEW"
    COMMENT = "COMMENT"
    EDIT = "EDIT"


class TeamArtifactCommentState(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class TeamArtifactMemberRequest(ContractModel):
    subject_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9@._:-]{0,159}$",
    )
    role: TeamArtifactRole


class TeamArtifactMember(TeamArtifactMemberRequest):
    allowed: bool
    denied_source_count: int = Field(ge=0)
    reason_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )

    @model_validator(mode="after")
    def coherent_decision(self) -> "TeamArtifactMember":
        if self.allowed and (self.denied_source_count != 0 or self.reason_code is not None):
            raise ValueError("Allowed members cannot carry denial evidence.")
        if not self.allowed and self.reason_code is None:
            raise ValueError("Denied members require a reason code.")
        return self


class RunTeamArtifactPreflightRequest(MutationCommand):
    team_id: UUID
    artifact_revision: int = Field(ge=1)
    members: list[TeamArtifactMemberRequest] = Field(min_length=1, max_length=100)
    sources: list[ArtifactSourceReference] = Field(default_factory=list, max_length=20)
    exclude_inaccessible_sources: bool = False

    @field_validator("members")
    @classmethod
    def unique_members(
        cls, value: list[TeamArtifactMemberRequest]
    ) -> list[TeamArtifactMemberRequest]:
        if len({member.subject_id for member in value}) != len(value):
            raise ValueError("Team artifact members must be unique.")
        return value

    @field_validator("sources")
    @classmethod
    def unique_sources(
        cls, value: list[ArtifactSourceReference]
    ) -> list[ArtifactSourceReference]:
        keys = {(source.source_type, source.reference) for source in value}
        if len(keys) != len(value):
            raise ValueError("Team artifact sources must be unique.")
        return value

    @model_validator(mode="after")
    def bind_artifact_revision(self) -> "RunTeamArtifactPreflightRequest":
        if self.expected_revision != self.artifact_revision:
            raise ValueError("artifactRevision must match expectedRevision.")
        return self


class TeamArtifactPreflight(ContractModel):
    preflight_id: UUID
    artifact_id: UUID
    team_id: UUID
    artifact_revision: int = Field(ge=1)
    state: TeamArtifactPreflightState
    decision_revision: int = Field(ge=1)
    members: list[TeamArtifactMember] = Field(min_length=1, max_length=100)
    allowed_source_count: int = Field(ge=0, le=20)
    excluded_source_count: int = Field(ge=0, le=20)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime
    created_at: datetime

    @model_validator(mode="after")
    def coherent_decision(self) -> "TeamArtifactPreflight":
        denied = any(not member.allowed for member in self.members)
        if self.state == TeamArtifactPreflightState.READY and (
            denied or self.excluded_source_count != 0
        ):
            raise ValueError("A ready preflight cannot contain denied access.")
        if self.state == TeamArtifactPreflightState.PARTIAL and (
            denied or self.excluded_source_count == 0
        ):
            raise ValueError("A partial preflight requires source exclusions only.")
        if self.state == TeamArtifactPreflightState.PERMISSION_DENIED and not (
            denied or self.excluded_source_count > 0
        ):
            raise ValueError("A denied preflight requires denial evidence.")
        if self.expires_at <= self.created_at:
            raise ValueError("A preflight must expire after creation.")
        return self


class CreateTeamArtifactWorkspaceRequest(HighRiskMutationCommand):
    team_id: UUID
    preflight_id: UUID


class CreateTeamArtifactAccessRequest(HighRiskMutationCommand):
    preflight_id: UUID


class UpdateTeamArtifactMembersRequest(HighRiskMutationCommand):
    preflight_id: UUID


class SubmitTeamArtifactEditRequest(MutationCommand):
    base_revision: int = Field(ge=1)
    content: ArtifactDraftContent

    @model_validator(mode="after")
    def bind_workspace_revision(self) -> "SubmitTeamArtifactEditRequest":
        if self.expected_revision != self.base_revision:
            raise ValueError("baseRevision must match expectedRevision.")
        return self


class ResolveTeamArtifactConflictRequest(HighRiskMutationCommand):
    resolution: TeamArtifactConflictResolution
    merged_content: ArtifactDraftContent | None = None

    @model_validator(mode="after")
    def require_merged_content(self) -> "ResolveTeamArtifactConflictRequest":
        if (self.resolution == TeamArtifactConflictResolution.MERGE) != (
            self.merged_content is not None
        ):
            raise ValueError("MERGE requires mergedContent and other resolutions forbid it.")
        return self


class CreateTeamArtifactShareRequest(HighRiskMutationCommand):
    preflight_id: UUID
    permission: TeamArtifactSharePermission
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("expiresAt must include a time-zone offset.")
        return value


class RevokeTeamArtifactShareRequest(HighRiskMutationCommand):
    pass


class CreateTeamArtifactCommentRequest(MutationCommand):
    body: str = Field(min_length=1, max_length=4_000)
    anchor: str | None = Field(default=None, max_length=1_000)

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("A comment body cannot be empty.")
        return normalized

    @field_validator("anchor")
    @classmethod
    def normalize_anchor(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None


class ReplyTeamArtifactCommentRequest(MutationCommand):
    body: str = Field(min_length=1, max_length=4_000)

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("A comment reply cannot be empty.")
        return normalized


class ResolveTeamArtifactCommentRequest(HighRiskMutationCommand):
    pass


class DecideTeamArtifactReviewStageRequest(HighRiskMutationCommand):
    decision: TeamArtifactReviewDecision


class TeamArtifactCommentReply(ContractModel):
    reply_id: UUID
    comment_id: UUID
    author_subject_id: str = Field(min_length=1, max_length=160)
    author_display_name: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=4_000)
    created_at: datetime


class TeamArtifactComment(ContractModel):
    comment_id: UUID
    workspace_id: UUID
    artifact_id: UUID
    author_subject_id: str = Field(min_length=1, max_length=160)
    author_display_name: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=4_000)
    anchor: str | None = Field(default=None, max_length=1_000)
    state: TeamArtifactCommentState
    revision: int = Field(ge=1)
    replies: list[TeamArtifactCommentReply] = Field(default_factory=list, max_length=500)
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def coherent_resolution(self) -> "TeamArtifactComment":
        if (self.state == TeamArtifactCommentState.RESOLVED) != (
            self.resolved_at is not None
        ):
            raise ValueError("Resolved comments require a resolution timestamp.")
        return self


class TeamArtifactAccessRequest(ContractModel):
    access_request_id: UUID
    artifact_id: UUID
    team_id: UUID
    preflight_id: UUID
    state: TeamArtifactAccessRequestState
    denied_subject_count: int = Field(ge=0, le=100)
    denied_source_count: int = Field(ge=0, le=20)
    submission_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def denied_evidence_required(self) -> "TeamArtifactAccessRequest":
        if self.denied_subject_count + self.denied_source_count < 1:
            raise ValueError("An access request requires denied ACL evidence.")
        return self


class TeamArtifactConflict(ContractModel):
    conflict_id: UUID
    workspace_id: UUID
    base_revision: int = Field(ge=1)
    server_revision: int = Field(ge=1)
    state: TeamArtifactConflictState
    local_content: ArtifactDraftContent
    server_content: ArtifactDraftContent
    local_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    server_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    resolved_at: datetime | None = None


class TeamArtifactShare(ContractModel):
    share_id: UUID
    workspace_id: UUID
    state: TeamArtifactShareState
    permission: TeamArtifactSharePermission
    member_count: int = Field(ge=1, le=100)
    expires_at: datetime
    revoked_at: datetime | None = None
    receipt_id: UUID
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revocation_receipt_id: UUID | None = None
    revocation_receipt_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    created_at: datetime

    @model_validator(mode="after")
    def coherent_revocation_receipt(self) -> "TeamArtifactShare":
        revoked = self.state == TeamArtifactShareState.REVOKED
        if revoked != (self.revocation_receipt_id is not None):
            raise ValueError("Revoked shares require a revocation receipt.")
        if revoked != (self.revocation_receipt_sha256 is not None):
            raise ValueError("Revoked shares require a sealed revocation receipt.")
        if revoked != (self.revoked_at is not None):
            raise ValueError("Revoked shares require a revocation timestamp.")
        if self.expires_at <= self.created_at:
            raise ValueError("A share must expire after creation.")
        return self


class TeamArtifactWorkspace(ContractModel):
    workspace_id: UUID
    artifact_id: UUID
    team_id: UUID
    state: TeamArtifactWorkspaceState
    revision: int = Field(ge=1)
    content: ArtifactDraftContent
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    members: list[TeamArtifactMember] = Field(min_length=1, max_length=100)
    open_conflict: TeamArtifactConflict | None = None
    shares: list[TeamArtifactShare] = Field(default_factory=list, max_length=100)
    review_stages: list[TeamArtifactReviewStage] = Field(default_factory=list, max_length=3)
    governance_gates: list[TeamArtifactGovernanceGate] = Field(
        default_factory=list, max_length=4
    )
    signature_evidence: TeamArtifactSignatureEvidence = Field(
        default_factory=unavailable_artifact_signature_evidence
    )
    review_sla_due_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def exactly_one_owner(self) -> "TeamArtifactWorkspace":
        owners = [
            member
            for member in self.members
            if member.allowed and member.role == TeamArtifactRole.OWNER
        ]
        if len(owners) != 1:
            raise ValueError("A team artifact workspace requires exactly one owner.")
        if self.review_stages and [stage.stage_order for stage in self.review_stages] != list(
            range(1, len(self.review_stages) + 1)
        ):
            raise ValueError("Artifact review stages must be contiguous and ordered.")
        if self.governance_gates and {
            gate.key for gate in self.governance_gates
        } != set(TeamArtifactGovernanceGateKey):
            raise ValueError("Artifact governance evidence must include all four gates.")
        return self


class TeamArtifactEditResult(ContractModel):
    state: str = Field(pattern=r"^(APPLIED|CONFLICT)$")
    workspace: TeamArtifactWorkspace
    conflict: TeamArtifactConflict | None = None

    @model_validator(mode="after")
    def coherent_result(self) -> "TeamArtifactEditResult":
        if (self.state == "CONFLICT") != (self.conflict is not None):
            raise ValueError("A conflict result must contain conflict evidence.")
        return self


class TeamArtifactCapabilitiesEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: TeamArtifactCapabilities


class TeamArtifactPreflightEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: TeamArtifactPreflight


class TeamArtifactAccessRequestEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: TeamArtifactAccessRequest


class TeamArtifactWorkspaceEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: TeamArtifactWorkspace


class TeamArtifactEditEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: TeamArtifactEditResult


class TeamArtifactShareEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: TeamArtifactShare


class TeamArtifactCommentEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: TeamArtifactComment


class TeamArtifactCommentsEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: list[TeamArtifactComment]
