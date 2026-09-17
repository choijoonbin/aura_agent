from __future__ import annotations

from pydantic import Field

from .contract_model import ContractModel


class WorkflowCapability(ContractModel):
    available: bool
    configured: bool
    reason_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    recovery_hint: str | None = Field(default=None, max_length=500)


class ResearchDeliveryCapabilities(ContractModel):
    artifact: WorkflowCapability
    proposal: WorkflowCapability
    export: WorkflowCapability
    handoff: WorkflowCapability
    share: WorkflowCapability
    routine: WorkflowCapability


class ResearchCapabilities(ContractModel):
    raw_export: WorkflowCapability
    pdf_export: WorkflowCapability
    receipt_download: WorkflowCapability
    audit_download: WorkflowCapability
    fork: WorkflowCapability
    merge: WorkflowCapability
    keep_local: WorkflowCapability
    sensitivity_recalculation: WorkflowCapability
    cache_fallback: WorkflowCapability
    delivery: ResearchDeliveryCapabilities


class ResearchCapabilitiesEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Research capabilities loaded."
    success: bool = True
    data: ResearchCapabilities
