from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from .contract_model import ContractModel


HOME_WIDGET_SCHEMA_VERSION = 1
HOME_WIDGET_BATCH_PATH = "/internal/home/v1/widget-data:batch"
HOME_WIDGET_COMMAND_PATH = "/internal/home/v1/widget-actions:execute"
HOME_WIDGET_MAX_BATCH = 100
HOME_WIDGET_MAX_ITEM_LIMIT = 50


class HomeWidgetState(StrEnum):
    AVAILABLE = "AVAILABLE"
    EMPTY = "EMPTY"
    PARTIAL = "PARTIAL"
    FORBIDDEN = "FORBIDDEN"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"


class HomeWidgetActionKind(StrEnum):
    SOURCE_ROUTE = "SOURCE_ROUTE"
    COMMAND = "COMMAND"


class HomeWidgetRequest(ContractModel):
    instance_id: UUID
    definition_key: str = Field(min_length=1, max_length=160)
    definition_version: str = Field(min_length=1, max_length=80)
    definition_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    renderer_binding_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration: dict[str, JsonValue] = Field(default_factory=dict, max_length=50)
    item_limit: int = Field(ge=1, le=HOME_WIDGET_MAX_ITEM_LIMIT)


class HomeWidgetBatchRequest(ContractModel):
    schema_version: int
    widgets: list[HomeWidgetRequest] = Field(min_length=1, max_length=HOME_WIDGET_MAX_BATCH)

    @model_validator(mode="after")
    def validate_contract(self) -> "HomeWidgetBatchRequest":
        if self.schema_version != HOME_WIDGET_SCHEMA_VERSION:
            raise ValueError("The Home widget schema version is unsupported.")
        instance_ids = {widget.instance_id for widget in self.widgets}
        if len(instance_ids) != len(self.widgets):
            raise ValueError("Home widget instance identifiers must be unique.")
        if any(widget.configuration for widget in self.widgets):
            raise ValueError("The DWAI-ON Home widget has no configurable authority input.")
        return self


class HomeWidgetSourceState(ContractModel):
    source_key: str
    generated_at: datetime
    expires_at: datetime
    last_success_at: datetime | None = None
    reason_code: str | None = None
    retryable: bool
    result_version: str | None = None


class HomeWidgetAction(ContractModel):
    action_id: str
    label_key: str
    kind: HomeWidgetActionKind
    source_route: str | None = None
    command_key: str | None = None
    expected_result_version: str | None = None
    requires_confirmation: bool = False


class HomeWidgetResult(ContractModel):
    instance_id: UUID
    definition_key: str
    definition_manifest_hash: str
    renderer_binding_revision: str
    state: HomeWidgetState
    source: HomeWidgetSourceState
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    actions: list[HomeWidgetAction] = Field(default_factory=list, max_length=8)
    redactions: list[str] = Field(default_factory=list, max_length=50)


class HomeWidgetBatchResponse(ContractModel):
    schema_version: int = HOME_WIDGET_SCHEMA_VERSION
    tenant_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    authority_decision_revision: str
    results: list[HomeWidgetResult]


class DwaionArtifactHomeItem(ContractModel):
    artifact_id: UUID
    title: str = Field(min_length=1, max_length=200)
    artifact_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    state: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    revision: int = Field(ge=1)
    updated_at: datetime


class DwaionArtifactHomePayload(ContractModel):
    visible_count: int = Field(ge=0)
    items: list[DwaionArtifactHomeItem] = Field(max_length=HOME_WIDGET_MAX_ITEM_LIMIT)


class DwaionArtifactHomeProjection(ContractModel):
    visible_count: int = Field(ge=0)
    slots: list[DwaionArtifactHomeItem | None] = Field(
        max_length=HOME_WIDGET_MAX_ITEM_LIMIT
    )
