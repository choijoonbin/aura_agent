from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import connect
from psycopg.rows import dict_row

from .governed_domain_core import GovernedPayloadCodec
from .personal_routine_execution_provider import RoutineExecutionProviderUnavailable


@dataclass(frozen=True)
class RoutineExecutionRecoveryDirective:
    action: str
    command_id: UUID
    reason_code: str
    change_reason: str
    prior_provider_receipt_id: str

    def provider_payload(self) -> dict[str, str]:
        return {
            "action": self.action,
            "commandId": str(self.command_id),
            "reasonCode": self.reason_code,
            "changeReason": self.change_reason,
            "priorProviderReceiptId": self.prior_provider_receipt_id,
        }


def load_routine_recovery_directive(
    database_url: str,
    codec: GovernedPayloadCodec,
    *,
    routine_run_id: UUID,
    tenant_id: int,
    recovery_action: str | None,
    recovery_command_id: UUID | None,
) -> RoutineExecutionRecoveryDirective | None:
    if recovery_action is None:
        return None
    if recovery_command_id is None:
        raise RoutineExecutionProviderUnavailable("ROUTINE_RECOVERY_DECISION_INVALID")
    with connect(database_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """SELECT provider_receipt_envelope, recovery_decision_envelope
                 FROM ai_personal_routine_executions
                WHERE routine_run_id = %s AND tenant_id = %s""",
            (routine_run_id, tenant_id),
        ).fetchone()
    if (
        row is None
        or row["provider_receipt_envelope"] is None
        or row["recovery_decision_envelope"] is None
    ):
        raise RoutineExecutionProviderUnavailable("ROUTINE_RECOVERY_EVIDENCE_UNAVAILABLE")
    provider_receipt = codec.decrypt_json(
        row["provider_receipt_envelope"],
        tenant_id=tenant_id,
        resource_type="personal-routine-execution",
        resource_id=str(routine_run_id),
        field="provider-receipt",
    )
    decision = codec.decrypt_json(
        row["recovery_decision_envelope"],
        tenant_id=tenant_id,
        resource_type="personal-routine-execution",
        resource_id=str(routine_run_id),
        field="recovery-decision",
    )
    if decision.get("commandId") != str(recovery_command_id):
        raise RoutineExecutionProviderUnavailable("ROUTINE_RECOVERY_DECISION_MISMATCH")
    return RoutineExecutionRecoveryDirective(
        action=recovery_action,
        command_id=recovery_command_id,
        reason_code=str(decision["reasonCode"]),
        change_reason=str(decision["changeReason"]),
        prior_provider_receipt_id=str(provider_receipt["providerReceiptId"]),
    )
