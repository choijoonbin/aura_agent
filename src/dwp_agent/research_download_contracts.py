from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .contract_model import ContractModel
from .dwaion_workflow_contracts import ResearchResult


class ResearchRawDownload(ContractModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    plan_id: UUID
    plan_revision: int = Field(ge=1)
    result: ResearchResult
    completed_at: datetime
    integrity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResearchReceiptDownload(ContractModel):
    schema_version: Literal[1] = 1
    receipt_id: UUID
    run_id: UUID
    plan_id: UUID
    plan_revision: int = Field(ge=1)
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    citation_count: int = Field(ge=1)
    completed_at: datetime
    integrity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResearchAuditDownloadEvent(ContractModel):
    event_id: UUID
    event_type: str = Field(min_length=1, max_length=48)
    actor_user_id: str = Field(min_length=1, max_length=160)
    correlation_id: str = Field(min_length=1, max_length=160)
    command_id: UUID
    previous_state: str | None = Field(default=None, max_length=32)
    current_state: str = Field(min_length=1, max_length=32)
    revision: int = Field(ge=1)
    occurred_at: datetime
    integrity_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
