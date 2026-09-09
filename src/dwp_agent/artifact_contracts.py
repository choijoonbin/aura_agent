from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, PrivateAttr, field_validator, model_validator

from .contracts import CitationSourceType, ContractModel
from .governed_domain_contracts import HighRiskMutationCommand, MutationCommand


class ArtifactType(StrEnum):
    DOCUMENT = "DOCUMENT"
    WORK_PLAN = "WORK_PLAN"
    COMPARISON = "COMPARISON"


class ArtifactState(StrEnum):
    DRAFT = "DRAFT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class DlpOutcome(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"


class ExportFormat(StrEnum):
    MARKDOWN = "MARKDOWN"
    DOCX = "DOCX"
    PDF = "PDF"


class ArtifactExportState(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ArtifactSourceReference(ContractModel):
    source_type: CitationSourceType
    reference: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}$",
    )


class ArtifactConversationSource(ContractModel):
    conversation_id: UUID
    assistant_message_id: UUID


class ArtifactSourceEvidence(ContractModel):
    source: ArtifactSourceReference
    verification_state: str = "UNVERIFIED"
    freshness: str = "UNKNOWN"
    verified_at: datetime | None = None
    verification_evidence_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def coherent_verification(self) -> "ArtifactSourceEvidence":
        sealed = self.verification_state == "SERVER_VERIFIED"
        if sealed and (
            self.verified_at is None
            or self.verification_evidence_fingerprint is None
            or self.freshness != "SNAPSHOT_AT_CONVERSATION"
        ):
            raise ValueError("Source verification evidence is incomplete or unsealed.")
        if not sealed and (
            self.verification_state != "UNVERIFIED"
            or self.verified_at is not None
            or self.verification_evidence_fingerprint is not None
            or self.freshness != "UNKNOWN"
        ):
            raise ValueError("Source verification state is unsupported.")
        return self


class ArtifactCapabilities(ContractModel):
    immutable_versions_available: bool = True
    version_restore_available: bool = False
    collaborative_editing_available: bool = False
    deterministic_preflight_available: bool = True
    enterprise_dlp_connector_available: bool = False
    source_verification_available: bool = False
    source_verification_scope: str = "UNAVAILABLE"
    manual_source_verification_available: bool = False
    source_freshness_available: bool = False
    personal_publish_state_available: bool = True
    recipient_sharing_available: bool = False
    external_sharing_available: bool = False
    export_request_available: bool = True
    export_execution_available: bool = False
    supported_export_formats: list[ExportFormat] = Field(
        default_factory=lambda: list(ExportFormat)
    )
    export_storage_scope: str = "POSTGRES_ENCRYPTED"


class ArtifactCapabilitiesEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: ArtifactCapabilities


class ArtifactDraftContent(ContractModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=100_000)
    format: str = Field(default="MARKDOWN", pattern=r"^MARKDOWN$")

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Artifact title cannot be blank.")
        return normalized


class CreateArtifactRequest(MutationCommand):
    _verified_source_references: frozenset[str] = PrivateAttr(default_factory=frozenset)

    artifact_type: ArtifactType
    content: ArtifactDraftContent
    sources: list[ArtifactSourceReference] = Field(default_factory=list, max_length=20)
    source_conversation: ArtifactConversationSource | None = None

    @field_validator("sources")
    @classmethod
    def unique_sources(
        cls, value: list[ArtifactSourceReference]
    ) -> list[ArtifactSourceReference]:
        keys = {(source.source_type, source.reference) for source in value}
        if len(keys) != len(value):
            raise ValueError("Artifact source references must be unique.")
        return value

    @model_validator(mode="after")
    def validate_source_provenance(self) -> "CreateArtifactRequest":
        if self.source_conversation is not None and self.sources:
            raise ValueError(
                "Conversation-bound artifact sources must be derived by the server."
            )
        return self


class AutosaveArtifactRequest(MutationCommand):
    content: ArtifactDraftContent
    sources: list[ArtifactSourceReference] = Field(default_factory=list, max_length=20)

    @field_validator("sources")
    @classmethod
    def unique_sources(
        cls, value: list[ArtifactSourceReference]
    ) -> list[ArtifactSourceReference]:
        keys = {(source.source_type, source.reference) for source in value}
        if len(keys) != len(value):
            raise ValueError("Artifact source references must be unique.")
        return value


class CreateArtifactVersionRequest(MutationCommand):
    pass


class RunArtifactPreflightRequest(MutationCommand):
    version_number: int = Field(ge=1)


class PublishArtifactRequest(HighRiskMutationCommand):
    version_number: int = Field(ge=1)
    preflight_id: UUID


class ExportArtifactRequest(HighRiskMutationCommand):
    version_number: int = Field(ge=1)
    preflight_id: UUID
    export_format: ExportFormat


class GovernedArtifact(ContractModel):
    artifact_id: UUID
    artifact_type: ArtifactType
    state: ArtifactState
    revision: int = Field(ge=1)
    draft_revision: int = Field(ge=1)
    current_version_number: int = Field(ge=0)
    published_version_number: int | None = Field(default=None, ge=1)
    content: ArtifactDraftContent
    sources: list[ArtifactSourceReference] = Field(max_length=20)
    capabilities: ArtifactCapabilities = Field(default_factory=ArtifactCapabilities)
    created_at: datetime
    updated_at: datetime


class ArtifactVersionReceipt(ContractModel):
    artifact_id: UUID
    artifact_revision: int = Field(ge=1)
    version_number: int = Field(ge=1)
    content_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_count: int = Field(ge=0, le=20)
    immutable: bool = True
    created_at: datetime


class ArtifactVersionSummary(ContractModel):
    artifact_id: UUID
    version_number: int = Field(ge=1)
    content_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_count: int = Field(ge=0, le=20)
    immutable: bool = True
    created_at: datetime


class ArtifactVersionDetail(ArtifactVersionSummary):
    content: ArtifactDraftContent
    source_evidence: list[ArtifactSourceEvidence] = Field(max_length=20)


class DlpFinding(ContractModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{1,63}$")
    severity: DlpOutcome
    field: str = Field(pattern=r"^(title|body|source)$")


class ArtifactPreflightReceipt(ContractModel):
    preflight_id: UUID
    artifact_id: UUID
    artifact_revision: int = Field(ge=1)
    version_number: int = Field(ge=1)
    policy_key: str = "DWP_DETERMINISTIC_DLP_V1"
    policy_version: int = 1
    outcome: DlpOutcome
    findings: list[DlpFinding]
    evaluated_at: datetime
    expires_at: datetime
    current: bool = True
    publish_allowed: bool
    export_allowed: bool


class ArtifactPublicationReceipt(ContractModel):
    artifact_id: UUID
    artifact_revision: int = Field(ge=1)
    published_version_number: int = Field(ge=1)
    state: ArtifactState = ArtifactState.PUBLISHED
    publication_scope: str = "PERSONAL_WORKSPACE_STATE_ONLY"
    recipient_sharing_performed: bool = False
    external_write_performed: bool = False


class ArtifactExportReceipt(ContractModel):
    export_job_id: UUID
    artifact_id: UUID
    artifact_revision: int = Field(ge=1)
    version_number: int = Field(ge=1)
    export_format: ExportFormat
    state: ArtifactExportState = ArtifactExportState.PENDING
    execution_available: bool = False
    file_available: bool = False
    external_write_performed: bool = False
    media_type: str | None = None
    file_name: str | None = None
    byte_size: int | None = Field(default=None, ge=1)
    content_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    requested_at: datetime | None = None
    completed_at: datetime | None = None
    safe_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )

    @model_validator(mode="after")
    def coherent_file_state(self) -> "ArtifactExportReceipt":
        metadata = (
            self.media_type,
            self.file_name,
            self.byte_size,
            self.content_fingerprint,
        )
        if self.file_available != (self.state == ArtifactExportState.SUCCEEDED):
            raise ValueError("Only a succeeded export can expose a file.")
        if self.file_available and any(value is None for value in metadata):
            raise ValueError("A succeeded export requires complete file metadata.")
        if not self.file_available and any(value is not None for value in metadata):
            raise ValueError("A non-file export cannot expose file metadata.")
        if self.state in {ArtifactExportState.SUCCEEDED, ArtifactExportState.FAILED}:
            if self.completed_at is None:
                raise ValueError("A terminal export requires completedAt.")
        return self


class ArtifactEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: GovernedArtifact


class ArtifactListEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: list[GovernedArtifact]


class ArtifactVersionEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: ArtifactVersionReceipt


class ArtifactVersionSummaryListEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: list[ArtifactVersionSummary]


class ArtifactVersionDetailEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: ArtifactVersionDetail


class ArtifactPreflightEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: ArtifactPreflightReceipt


class ArtifactPublicationEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: ArtifactPublicationReceipt


class ArtifactExportEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: ArtifactExportReceipt
