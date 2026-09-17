from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical_json import canonical_json_bytes


class ArtifactReviewNotificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    commandId: UUID
    artifactId: UUID
    workspaceId: UUID
    stageId: UUID
    assigneeSubjectId: str = Field(min_length=1, max_length=160)
    workspaceRevision: int = Field(ge=1)
    reasonCode: str = Field(pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$")
    changeReason: str = Field(min_length=1, max_length=500)
    deliveryState: str = Field(pattern=r"^DELIVERED$")
    providerReceiptId: str = Field(min_length=1, max_length=240)
    resultSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    deliveredAt: datetime

    @field_validator("assigneeSubjectId", "changeReason", "providerReceiptId")
    @classmethod
    def nonblank_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Review notification evidence cannot be blank.")
        return normalized

    @field_validator("deliveredAt")
    @classmethod
    def aware_delivery(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Provider deliveredAt must include an offset.")
        return value

    @model_validator(mode="after")
    def verify_result_digest(self) -> "ArtifactReviewNotificationResult":
        expected = review_notification_result_sha256(
            command_id=self.commandId,
            artifact_id=self.artifactId,
            workspace_id=self.workspaceId,
            stage_id=self.stageId,
            assignee_subject_id=self.assigneeSubjectId,
            workspace_revision=self.workspaceRevision,
            reason_code=self.reasonCode,
            change_reason=self.changeReason,
            delivery_state=self.deliveryState,
            provider_receipt_id=self.providerReceiptId,
            delivered_at=self.deliveredAt,
        )
        if self.resultSha256 != expected:
            raise ValueError("Review notification result digest is not canonical.")
        return self


def review_notification_result_sha256(
    *,
    command_id: UUID,
    artifact_id: UUID,
    workspace_id: UUID,
    stage_id: UUID,
    assignee_subject_id: str,
    workspace_revision: int,
    reason_code: str,
    change_reason: str,
    delivery_state: str,
    provider_receipt_id: str,
    delivered_at: datetime,
) -> str:
    if delivered_at.utcoffset() is None:
        raise ValueError("Review notification delivery time must include an offset.")
    payload = {
        "commandId": str(command_id),
        "artifactId": str(artifact_id),
        "workspaceId": str(workspace_id),
        "stageId": str(stage_id),
        "assigneeSubjectId": assignee_subject_id,
        "workspaceRevision": workspace_revision,
        "reasonCode": reason_code,
        "changeReason": change_reason,
        "deliveryState": delivery_state,
        "providerReceiptId": provider_receipt_id,
        "deliveredAt": delivered_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
