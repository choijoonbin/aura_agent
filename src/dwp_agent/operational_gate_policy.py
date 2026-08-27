from __future__ import annotations

from .operational_gate_contracts import (
    GateActorRole,
    GateApprovalEligibilityReason,
    GateEnvironment,
    GateStatus,
    OperationalGateApprovalEligibility,
    OperationalGateKey,
    OperationalGateSummary,
)
from .operational_gate_store_errors import OperationalGateSeparationOfDutyViolation


_DEVELOPMENT_ONLY_OPTIONS = {
    (OperationalGateKey.NETWORK_ISOLATION, "DEVELOPMENT_PUBLIC_ONLY"),
    (OperationalGateKey.EVALUATION_DATASET, "SYNTHETIC_DEVELOPMENT_ONLY"),
    (OperationalGateKey.RETENTION_LEGAL_HOLD, "DWP_DEVELOPMENT_DEFAULT"),
}
_NEVER_DELIVERABLE_OPTIONS = {
    (OperationalGateKey.ACTION_APPROVAL, "BLOCKED"),
    (OperationalGateKey.TENANT_KMS, "LOCAL_DEVELOPMENT_KEY"),
}


def option_allowed_for_environment(
    gate_key: OperationalGateKey,
    environment: GateEnvironment,
    selected_option: str | None,
) -> bool:
    if not selected_option:
        return False
    option = selected_option.strip().upper()
    if (gate_key, option) in _NEVER_DELIVERABLE_OPTIONS:
        return False
    if (gate_key, option) in _DEVELOPMENT_ONLY_OPTIONS:
        return environment == GateEnvironment.DEVELOPMENT
    return True


def approval_eligibility(
    gate: OperationalGateSummary, actor_user_id: str
) -> OperationalGateApprovalEligibility:
    if gate.status != GateStatus.READY_FOR_APPROVAL:
        return OperationalGateApprovalEligibility(
            eligible=False,
            reason=GateApprovalEligibilityReason.NOT_READY_FOR_APPROVAL,
        )
    actor_roles = (
        (GateActorRole.OWNER, gate.owner_user_id),
        (GateActorRole.CONFIGURATOR, gate.last_configured_by),
        (GateActorRole.VALIDATOR, gate.last_validated_by),
    )
    for role, user_id in actor_roles:
        if user_id and actor_user_id == user_id:
            return OperationalGateApprovalEligibility(
                eligible=False,
                reason=GateApprovalEligibilityReason.SEPARATION_OF_DUTY,
                conflicting_role=role,
            )
    return OperationalGateApprovalEligibility(
        eligible=True,
        reason=GateApprovalEligibilityReason.ELIGIBLE,
    )


def require_independent_approver(
    gate: OperationalGateSummary, actor_user_id: str
) -> None:
    eligibility = approval_eligibility(gate, actor_user_id)
    if (
        not eligibility.eligible
        and eligibility.reason == GateApprovalEligibilityReason.SEPARATION_OF_DUTY
    ):
        role = eligibility.conflicting_role or GateActorRole.OWNER
        raise OperationalGateSeparationOfDutyViolation(role.value)
