from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from .contract_model import ContractModel


class AskPersonalizationState(StrEnum):
    NOT_EVALUATED = "NOT_EVALUATED"
    NOT_PERMITTED = "NOT_PERMITTED"
    DISABLED = "DISABLED"
    EMPTY = "EMPTY"
    APPLIED = "APPLIED"
    BYPASSED = "BYPASSED"
    UNAVAILABLE = "UNAVAILABLE"


class AskPersonalization(ContractModel):
    state: AskPersonalizationState = AskPersonalizationState.NOT_EVALUATED
    applied_kinds: list[
        Literal["RESPONSE_LENGTH", "OUTPUT_FORMAT", "TONE", "WORKING_STYLE"]
    ] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_application_evidence(self) -> "AskPersonalization":
        if len(self.applied_kinds) != len(set(self.applied_kinds)):
            raise ValueError("Applied personalization kinds must be unique.")
        if self.state == AskPersonalizationState.APPLIED and not self.applied_kinds:
            raise ValueError("Applied personalization requires at least one preference kind.")
        if self.state != AskPersonalizationState.APPLIED and self.applied_kinds:
            raise ValueError("Only applied personalization may expose preference kinds.")
        return self
