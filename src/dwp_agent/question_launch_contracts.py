from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator

from .contracts import ContractModel


class CreateQuestionLaunchRequest(ContractModel):
    question: str = Field(min_length=2, max_length=4_000)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("Question must contain at least two non-whitespace characters.")
        return normalized


class ConsumeQuestionLaunchRequest(ContractModel):
    launch_id: UUID


class QuestionLaunchReceipt(ContractModel):
    launch_id: UUID
    expires_at: datetime


class QuestionLaunchPayload(ContractModel):
    question: str = Field(min_length=2, max_length=4_000)


class QuestionLaunchReceiptEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Question launch prepared."
    success: bool = True
    data: QuestionLaunchReceipt


class QuestionLaunchPayloadEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Question launch consumed."
    success: bool = True
    data: QuestionLaunchPayload
