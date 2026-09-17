from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
)
from .personal_domain_security import PersonalDomainIdentity
from .personal_routine_contracts import RoutineExecutionReceipt, RoutineExecutionRun


class PersonalRoutineExecutionQueries:
    def _locked_execution(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        routine_id: UUID,
        run_id: UUID,
        *,
        lock: bool = True,
    ) -> Any:
        row = connection.execute(
            EXECUTION_SELECT
            + " WHERE routine_run_id = %s AND routine_id = %s"
            + " AND tenant_id = %s AND user_id = %s"
            + (" FOR UPDATE" if lock else ""),
            (run_id, routine_id, identity.tenant_id, identity.user_id),
        ).fetchone()
        if row is None:
            raise GovernedDomainNotFound("The routine execution is unavailable.")
        return row

    def _execution(self, connection: Any, row: Any) -> RoutineExecutionRun:
        receipt = None
        if row["receipt_id"] is not None:
            provider = self._provider_receipt(row)
            payload = self._receipt_payload(row, provider)
            expected = self.fingerprints.value(
                tenant_id=int(row["tenant_id"]),
                purpose="personal-routine-execution-receipt",
                payload=payload,
            )
            if not self.fingerprints.matches(expected, row["receipt_fingerprint"]):
                raise GovernedDomainUnavailable(
                    "The routine execution receipt failed integrity validation."
                )
            receipt = RoutineExecutionReceipt(
                receipt_id=row["receipt_id"],
                routine_run_id=row["routine_run_id"],
                routine_id=row["routine_id"],
                routine_revision=row["routine_revision"],
                terminal_state=row["run_state"],
                provider_receipt_id=provider["providerReceiptId"],
                result_sha256=row["result_sha256"],
                evidence_count=row["evidence_count"],
                proposals_created=row["proposals_created"],
                approval_gated_actions_created=row["approval_gated_actions_created"],
                external_writes_performed=row["external_writes_performed"],
                notification_state=row["notification_state"],
                authorization_decision_revision=provider[
                    "authorizationDecisionRevision"
                ],
                authorized_sources=provider["authorizedSources"],
                completed_at=row["completed_at"],
            )
        return RoutineExecutionRun(
            routine_run_id=row["routine_run_id"],
            routine_id=row["routine_id"],
            routine_revision=row["routine_revision"],
            trigger=row["trigger_type"],
            state=row["run_state"],
            version=row["version"],
            attempt_count=row["attempt_count"],
            maximum_attempts=row["maximum_attempts"],
            scheduled_for=row["scheduled_for"],
            next_attempt_at=row["next_attempt_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            evidence_count=row["evidence_count"],
            proposals_created=row["proposals_created"],
            approval_gated_actions_created=row["approval_gated_actions_created"],
            tokens_used=row["tokens_used"],
            elapsed_ms=row["elapsed_ms"],
            notification_state=row["notification_state"],
            safe_error_code=row["safe_error_code"],
            recovery_hint=row["recovery_hint"],
            compensation_required=row["compensation_required"],
            receipt=receipt,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _provider_receipt(self, row: Any) -> dict[str, object]:
        if row["provider_receipt_envelope"] is None:
            raise GovernedDomainUnavailable(
                "The routine execution provider receipt is unavailable."
            )
        return self.codec.decrypt_json(
            row["provider_receipt_envelope"],
            tenant_id=int(row["tenant_id"]),
            resource_type="personal-routine-execution",
            resource_id=str(row["routine_run_id"]),
            field="provider-receipt",
        )

    @staticmethod
    def _receipt_payload(row: Any, provider: dict[str, object]) -> dict[str, object]:
        return {
            "receiptId": str(row["receipt_id"]),
            "routineRunId": str(row["routine_run_id"]),
            "routineId": str(row["routine_id"]),
            "routineRevision": int(row["routine_revision"]),
            "terminalState": row["run_state"],
            "providerReceiptId": str(provider["providerReceiptId"]),
            "resultSha256": row["result_sha256"],
            "evidenceCount": int(row["evidence_count"]),
            "proposalsCreated": int(row["proposals_created"]),
            "approvalGatedActionsCreated": int(
                row["approval_gated_actions_created"]
            ),
            "notificationState": row["notification_state"],
            "authorizationDecisionRevision": int(
                provider["authorizationDecisionRevision"]
            ),
            "authorizedSources": list(provider["authorizedSources"]),
            "completedAt": row["completed_at"].isoformat(),
        }

    @staticmethod
    def _require_monthly_run_budget(
        connection: Any,
        tenant_id: int,
        user_id: str,
        routine_id: UUID,
        maximum_runs: int,
        reference: datetime,
    ) -> None:
        month = reference.astimezone(UTC).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        row = connection.execute(
            """SELECT COUNT(*) AS count
                 FROM ai_personal_routine_executions
                WHERE tenant_id = %s AND user_id = %s AND routine_id = %s
                  AND created_at >= %s""",
            (tenant_id, user_id, routine_id, month),
        ).fetchone()
        if int(row["count"]) >= maximum_runs:
            raise GovernedDomainConflict("The routine monthly run budget is exhausted.")

    @staticmethod
    def _execution_event(
        connection: Any,
        *,
        run_id: UUID,
        routine_id: UUID,
        tenant_id: int,
        user_id: str,
        event_type: str,
        previous_state: str | None,
        current_state: str,
        version: int,
        attempt_count: int,
        lease_generation: int,
        safe_error_code: str | None = None,
        receipt_fingerprint: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO ai_personal_routine_execution_events (
                   event_id, routine_run_id, routine_id, tenant_id, user_id,
                   event_type, previous_state, current_state, version,
                   attempt_count, lease_generation, safe_error_code,
                   receipt_fingerprint)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(),
                run_id,
                routine_id,
                tenant_id,
                user_id,
                event_type,
                previous_state,
                current_state,
                version,
                attempt_count,
                lease_generation,
                safe_error_code,
                receipt_fingerprint,
            ),
        )

EXECUTION_SELECT = """SELECT routine_run_id, routine_id, tenant_id, user_id,
       routine_revision, trigger_type, run_state, version, scheduled_for,
       next_attempt_at, attempt_count, maximum_attempts, lease_generation,
       lease_token, lease_expires_at, correlation_id, evidence_count,
       proposals_created, approval_gated_actions_created,
       external_writes_performed, tokens_used, elapsed_ms,
       notification_state, provider_receipt_envelope, result_sha256,
       receipt_id, receipt_fingerprint, safe_error_code, recovery_hint,
       compensation_required, compensation_requested, created_at, updated_at,
       started_at, completed_at
  FROM ai_personal_routine_executions"""
