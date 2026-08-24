from __future__ import annotations

from .operational_gate_contracts import (
    GateActorRole,
    GateApprovalEligibilityReason,
    GateStatus,
    OperationalGateApprovalEligibility,
    OperationalGateSummary,
)
from .operational_gate_store_errors import OperationalGateSeparationOfDutyViolation


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
