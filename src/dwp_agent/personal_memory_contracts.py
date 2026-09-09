from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator

from .contracts import ContractModel
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
    DELETED = "DELETED"


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


class UpdateMemoryRequest(MutationCommand):
    memory: ExplicitMemoryValue


class ChangeMemoryStateRequest(HighRiskMutationCommand):
    memory_state: MemoryState

    @field_validator("memory_state")
    @classmethod
    def non_deleted_state(cls, value: MemoryState) -> MemoryState:
        if value == MemoryState.DELETED:
            raise ValueError("Use the deletion command to delete a memory.")
        return value


class DeleteMemoryRequest(HighRiskMutationCommand):
    pass


class PersonalMemory(ContractModel):
    memory_id: UUID
    kind: MemoryKind
    state: MemoryState
    revision: int = Field(ge=1)
    memory: ExplicitMemoryValue
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
