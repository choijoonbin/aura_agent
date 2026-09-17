from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel
from .dwaion_workflow_contracts import WorkflowCapability
from .governed_domain_contracts import HighRiskMutationCommand, MutationCommand


class MemoryPreferenceState(StrEnum):
    UNSET = "UNSET"
    DISABLED = "DISABLED"
    ENABLED = "ENABLED"


class MemoryKind(StrEnum):
    RESPONSE_LENGTH = "RESPONSE_LENGTH"
    OUTPUT_FORMAT = "OUTPUT_FORMAT"
    TONE = "TONE"
    WORKING_STYLE = "WORKING_STYLE"


class MemoryState(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    EXPIRED = "EXPIRED"
    DELETED = "DELETED"


class MemoryScope(StrEnum):
    ASK = "ASK"
    RESEARCH = "RESEARCH"
    PROPOSALS = "PROPOSALS"
    ROUTINES = "ROUTINES"
    ARTIFACTS = "ARTIFACTS"


class MemoryOrigin(StrEnum):
    MANUAL = "MANUAL"


def _memory_capability(
    available: bool, reason_code: str | None, recovery_hint: str | None
) -> WorkflowCapability:
    return WorkflowCapability(
        available=available,
        configured=available,
        reason_code=reason_code,
        recovery_hint=recovery_hint,
    )


class MemoryEvidenceCapabilities(ContractModel):
    manual_provenance: WorkflowCapability = Field(default_factory=lambda: _memory_capability(
        True, None, None
    ))
    ai_derived_memory: WorkflowCapability = Field(default_factory=lambda: _memory_capability(
        False, "AI_DERIVED_MEMORY_NOT_ENABLED",
        "Use explicit manual memory, or configure an approval-gated derivation provider.",
    ))
    confidence_scoring: WorkflowCapability = Field(default_factory=lambda: _memory_capability(
        False, "MEMORY_CONFIDENCE_NOT_APPLICABLE",
        "Confidence is unavailable because only explicit manual memories are accepted.",
    ))
    fact_vector: WorkflowCapability = Field(default_factory=lambda: _memory_capability(
        False, "MEMORY_FACT_VECTOR_NOT_APPLICABLE",
        "Fact vectors require an approval-gated AI-derived memory provider.",
    ))
    usage_metrics: WorkflowCapability = Field(default_factory=lambda: _memory_capability(
        True, None, None
    ))
    usage_trail: WorkflowCapability = Field(default_factory=lambda: _memory_capability(
        False, "MEMORY_USAGE_TRAIL_NOT_CONFIGURED",
        "Configure an audited per-use evidence stream before showing a usage trail.",
    ))
    kms_binding: WorkflowCapability = Field(default_factory=lambda: _memory_capability(
        True, None, None
    ))


class AiSourceKey(StrEnum):
    WORK_ITEM = "WORK_ITEM"
    MAIL = "MAIL"
    CALENDAR = "CALENDAR"


class ExplicitMemoryValue(ContractModel):
    value: str = Field(min_length=1, max_length=500)

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Memory value cannot be blank.")
        return normalized


class UpdateMemoryPreferenceRequest(HighRiskMutationCommand):
    memory_state: MemoryPreferenceState

    @field_validator("memory_state")
    @classmethod
    def explicit_state(cls, value: MemoryPreferenceState) -> MemoryPreferenceState:
        if value == MemoryPreferenceState.UNSET:
            raise ValueError("Memory must be explicitly enabled or disabled.")
        return value


class UpdateMemoryRuntimePreferenceRequest(HighRiskMutationCommand):
    runtime_application_state: MemoryPreferenceState

    @field_validator("runtime_application_state")
    @classmethod
    def explicit_state(cls, value: MemoryPreferenceState) -> MemoryPreferenceState:
        if value == MemoryPreferenceState.UNSET:
            raise ValueError("Runtime personalization must be explicitly enabled or disabled.")
        return value


class UpdateAiSourcePreferenceRequest(HighRiskMutationCommand):
    enabled: bool


class CreateMemoryRequest(MutationCommand):
    kind: MemoryKind
    memory: ExplicitMemoryValue
    scope: list[MemoryScope] = Field(default_factory=lambda: [MemoryScope.ASK], min_length=1)
    expires_at: datetime | None = None

    @field_validator("scope")
    @classmethod
    def unique_scope(cls, value: list[MemoryScope]) -> list[MemoryScope]:
        if len(value) != len(set(value)):
            raise ValueError("Memory scope selectors must be unique.")
        return value

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Memory expiry must include a timezone.")
        return value


class UpdateMemoryRequest(MutationCommand):
    memory: ExplicitMemoryValue | None = None
    scope: list[MemoryScope] | None = Field(default=None, min_length=1)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "UpdateMemoryRequest":
        if (
            self.memory is None
            and self.scope is None
            and "expires_at" not in self.model_fields_set
        ):
            raise ValueError("A memory value, scope, or expiry update is required.")
        if self.scope is not None and len(self.scope) != len(set(self.scope)):
            raise ValueError("Memory scope selectors must be unique.")
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            raise ValueError("Memory expiry must include a timezone.")
        return self


class ChangeMemoryStateRequest(HighRiskMutationCommand):
    memory_state: MemoryState

    @field_validator("memory_state")
    @classmethod
    def non_deleted_state(cls, value: MemoryState) -> MemoryState:
        if value in {MemoryState.DELETED, MemoryState.EXPIRED}:
            raise ValueError("Deleted and expired states are derived by governed commands.")
        return value


class DeleteMemoryRequest(HighRiskMutationCommand):
    pass


class PersonalMemory(ContractModel):
    memory_id: UUID
    kind: MemoryKind
    state: MemoryState
    revision: int = Field(ge=1)
    memory: ExplicitMemoryValue
    scope: list[MemoryScope] = Field(default_factory=lambda: [MemoryScope.ASK], min_length=1)
    expires_at: datetime | None = None
    origin: MemoryOrigin = MemoryOrigin.MANUAL
    source_type: str = "USER_EXPLICIT_ENTRY"
    confidence: float | None = Field(default=None, ge=0, le=1)
    fact_vector: list[str] = Field(default_factory=list, max_length=0)
    use_count: int = Field(default=0, ge=0)
    last_used_at: datetime | None = None
    encryption_provider: str | None = Field(default=None, min_length=1, max_length=64)
    encryption_key_version: str | None = Field(default=None, max_length=128)
    encryption_key_reference_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RuntimeMemorySelection:
    storage_enabled: bool
    runtime_enabled: bool
    memories: tuple[PersonalMemory, ...]


class PersonalAiControls(ContractModel):
    memory_state: MemoryPreferenceState
    runtime_application_state: MemoryPreferenceState = MemoryPreferenceState.UNSET
    revision: int = Field(ge=0)
    memory_enabled: bool
    runtime_application_enabled: bool = False
    memory_effective: bool
    explicit_memory_storage_available: bool = True
    runtime_application_available: bool = True
    team_memory_available: bool = False
    automatic_memory_inference: bool = False
    sensitive_memory_allowed: bool = False
    background_credential_storage: bool = False
    external_action_without_approval: bool = False
    evidence_capabilities: MemoryEvidenceCapabilities = Field(
        default_factory=MemoryEvidenceCapabilities
    )
    source_preferences: list["AiSourcePreference"] = Field(default_factory=list)
    updated_at: datetime | None = None


class AiSourcePreference(ContractModel):
    source_key: AiSourceKey
    available: bool
    enabled: bool
    effective: bool
    effect_scope: str = "PERSONAL_ROUTINE_DRY_RUN_ONLY"
    proactive_analysis_integration_available: bool = False
    revision: int = Field(ge=0)
    retention: str = "REFERENCE_ONLY_NO_RAW_COPY"
    updated_at: datetime | None = None


class MemoryEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: PersonalMemory


class MemoryListEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: list[PersonalMemory]


class PersonalAiControlsEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: PersonalAiControls


class AiSourcePreferenceEnvelope(ContractModel):
    status: str = "SUCCESS"
    success: bool = True
    data: AiSourcePreference
