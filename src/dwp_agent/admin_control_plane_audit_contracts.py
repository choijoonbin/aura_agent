from __future__ import annotations

from pydantic import Field, model_validator

from .admin_control_plane_validation import normalize_governed_review
from .contract_model import ContractModel


class GovernedCommandEvidenceInput(ContractModel):
    reason: str = Field(min_length=5, max_length=2_000)
    evidence_refs: list[str] = Field(max_length=100)

    @model_validator(mode="after")
    def normalize_audit_evidence(self) -> "GovernedCommandEvidenceInput":
        self.reason, self.evidence_refs, _ = normalize_governed_review(
            self.reason, self.evidence_refs
        )
        return self
