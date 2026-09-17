from __future__ import annotations

from dataclasses import replace
import hashlib
from typing import Any, Mapping
from uuid import UUID, NAMESPACE_URL, uuid5

from psycopg import connect
from psycopg.rows import dict_row

from .admin_control_plane_adapters import (
    AdminCommandAdapter,
    AdminCommandExecutionContext,
    AdminCommandExecutionRejected,
    AdminCommandExecutionResult,
    AdminCommandExecutionTransient,
    admin_command_capabilities,
    resolve_admin_command_adapter,
)
from .admin_control_plane_contracts import (
    CommandProblem,
    GovernedCommandKind,
    GovernedCommandObservation,
    GovernedCommandState,
)
from .admin_control_plane_internal_executor import AdminInternalCommandExecutor
from .admin_control_plane_result_validation import (
    require_external_admin_result_contract,
    validate_external_admin_result,
)
from .admin_control_plane_registry import (
    AdminCommandExecutionMode,
    AdminCommandSpec,
    command_spec,
)
from .admin_control_plane_worker import AdminControlPlaneWorkerStore
from .governed_domain_core import GovernedPayloadCodec
from .admin_evaluation_gate import (
    EvaluationDatasetMissing,
    EvaluationDatasetNotApproved,
    require_pii_approved_dataset,
)
from .transactional_outbox import OutboxLease
from .canonical_json import canonical_json_bytes


class PostgresAdminControlCommandExecutor:
    worker_id = "dwp-agent-admin-control-executor"

    def __init__(
        self,
        database_url: str,
        *,
        adapters: Mapping[str, AdminCommandAdapter] | None = None,
    ) -> None:
        self.database_url = database_url
        self.codec = GovernedPayloadCodec()
        self.worker_store = AdminControlPlaneWorkerStore(database_url)
        self.internal = AdminInternalCommandExecutor(database_url, worker_id=self.worker_id)
        self.adapters = dict(adapters or {})

    def process(self, outbox_lease: OutboxLease) -> str:
        context = self._load_context(outbox_lease)
        if context.state in {
            GovernedCommandState.SUCCEEDED,
            GovernedCommandState.PARTIAL,
            GovernedCommandState.FAILED,
            GovernedCommandState.REJECTED,
            GovernedCommandState.CANCELLED,
            GovernedCommandState.ROLLED_BACK,
        }:
            return context.state.value
        if context.state == GovernedCommandState.QUEUED:
            running = self.worker_store.observe(
                tenant_id=context.tenant_id,
                worker_id=self.worker_id,
                correlation_id=context.correlation_id,
                governed_command_id=context.command_id,
                request=GovernedCommandObservation(
                    command_id=_observation_id(outbox_lease.outbox_id, "RUNNING"),
                    expected_version=context.command_revision,
                    state=GovernedCommandState.RUNNING,
                    progress_percent=5,
                ),
            )
            context = replace(context, state=running.state, command_revision=running.version)
        elif context.state != GovernedCommandState.RUNNING:
            raise AdminCommandExecutionRejected(
                "ADMIN_COMMAND_STATE_INVALID",
                f"The outbox intent is bound to a command in {context.state.value} state.",
                "Reconcile the command ledger and outbox intent before retrying.",
            )

        spec = command_spec(context.kind)
        try:
            result = (
                self.internal.execute(context, spec)
                if spec.mode == AdminCommandExecutionMode.INTERNAL
                else self._execute_external(context, spec)
            )
        except AdminCommandExecutionRejected as error:
            result = AdminCommandExecutionResult(
                state=GovernedCommandState.FAILED,
                problem=error.problem,
            )
        fenced_context = self._load_context(outbox_lease)
        if fenced_context.state in {
            GovernedCommandState.SUCCEEDED,
            GovernedCommandState.PARTIAL,
            GovernedCommandState.FAILED,
            GovernedCommandState.REJECTED,
            GovernedCommandState.CANCELLED,
            GovernedCommandState.ROLLED_BACK,
        }:
            return fenced_context.state.value
        if fenced_context.state != GovernedCommandState.RUNNING:
            raise AdminCommandExecutionTransient(
                "The admin command changed state before its result could be fenced."
            )
        context = fenced_context
        observed = self.worker_store.observe(
            tenant_id=context.tenant_id,
            worker_id=self.worker_id,
            correlation_id=context.correlation_id,
            governed_command_id=context.command_id,
            request=GovernedCommandObservation(
                command_id=_observation_id(outbox_lease.outbox_id, result.state.value),
                attempt_id=context.attempt_id if result.snapshot is not None else None,
                expected_version=context.command_revision,
                tenant_id=context.tenant_id if result.snapshot is not None else None,
                correlation_id=context.correlation_id if result.snapshot is not None else None,
                state=result.state,
                progress_percent=100,
                result_summary=result.summary,
                domain_receipt_ref=result.domain_receipt_ref,
                rollback_ref=result.rollback_ref,
                result_snapshot=result.snapshot,
                result_version=result.version,
                result_sha256=(
                    hashlib.sha256(canonical_json_bytes(result.snapshot)).hexdigest()
                    if result.snapshot is not None
                    else None
                ),
                problem=result.problem,
            ),
        )
        return observed.state.value

    def fail_dead_letter(self, outbox_lease: OutboxLease, *, safe_error_code: str) -> None:
        """Close a command after the outbox retry budget is exhausted."""
        context = self._load_context(outbox_lease, require_live_lease=False)
        if context.state not in {GovernedCommandState.QUEUED, GovernedCommandState.RUNNING}:
            return
        if context.state == GovernedCommandState.QUEUED:
            running = self.worker_store.observe(
                tenant_id=context.tenant_id,
                worker_id=self.worker_id,
                correlation_id=context.correlation_id,
                governed_command_id=context.command_id,
                request=GovernedCommandObservation(
                    command_id=_observation_id(outbox_lease.outbox_id, "RUNNING"),
                    expected_version=context.command_revision,
                    state=GovernedCommandState.RUNNING,
                    progress_percent=5,
                ),
            )
            revision = running.version
        else:
            revision = context.command_revision
        self.worker_store.observe(
            tenant_id=context.tenant_id,
            worker_id=self.worker_id,
            correlation_id=context.correlation_id,
            governed_command_id=context.command_id,
            request=GovernedCommandObservation(
                command_id=_observation_id(outbox_lease.outbox_id, "DEAD_LETTER"),
                expected_version=revision,
                state=GovernedCommandState.FAILED,
                progress_percent=100,
                problem=CommandProblem(
                    code=safe_error_code,
                    detail="The governed command exhausted its bounded infrastructure retry budget.",
                    recovery_hint="Restore the command adapter or database dependency, then submit an explicit governed retry.",
                ),
            ),
        )

    def _execute_external(
        self, context: AdminCommandExecutionContext, spec: AdminCommandSpec
    ) -> AdminCommandExecutionResult:
        require_external_admin_result_contract(context, spec)
        if context.kind in {
            GovernedCommandKind.EVALUATION_COMPARE,
            GovernedCommandKind.EVALUATION_RUN,
            GovernedCommandKind.EVALUATION_RERUN,
        }:
            with connect(self.database_url, row_factory=dict_row) as connection:
                self._require_pii_approved_dataset(
                    context, connection=connection, lock=True
                )
                return self._execute_adapter(context, spec)
        return self._execute_adapter(context, spec)

    def _execute_adapter(
        self, context: AdminCommandExecutionContext, spec: AdminCommandSpec
    ) -> AdminCommandExecutionResult:
        service = spec.service or ""
        adapter = resolve_admin_command_adapter(service, self.adapters)
        response = adapter.execute(service=service, context=context, spec=spec)
        try:
            self._verify_adapter_receipt(context, response)
        except AdminCommandExecutionRejected:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise AdminCommandExecutionRejected(
                "ADMIN_ADAPTER_RECEIPT_INVALID",
                "The domain adapter returned an invalid governed receipt.",
                "Repair the receipt schema and retry with the same governed attempt.",
            ) from error
        return AdminCommandExecutionResult(
            state=response.state,
            summary=response.result_summary,
            domain_receipt_ref=response.domain_receipt_ref,
            rollback_ref=response.rollback_ref,
            snapshot=response.result_snapshot,
            version=response.result_version,
            problem=response.problem,
        )

    @staticmethod
    def _verify_adapter_receipt(
        context: AdminCommandExecutionContext, response: Any
    ) -> None:
        target = response.target
        if not (
            response.command_id == context.command_id
            and response.tenant_id == context.tenant_id
            and response.correlation_id == context.correlation_id
            and response.attempt_id == context.attempt_id
            and response.kind == context.kind
            and target is not None
            and target.type == context.target_type
            and target.id == context.target_id
            and response.expected_version == context.expected_target_version
        ):
            raise AdminCommandExecutionRejected(
                "ADMIN_ADAPTER_BINDING_INVALID",
                "The domain result is not bound to the governed attempt, kind, target, and version.",
                "Reject the result and repair context propagation in the domain adapter.",
            )
        if response.state not in {
            GovernedCommandState.SUCCEEDED,
            GovernedCommandState.ROLLED_BACK,
        }:
            return
        snapshot = response.result_snapshot
        digest = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
        if response.result_sha256 != digest:
            raise AdminCommandExecutionRejected(
                "ADMIN_ADAPTER_BINDING_INVALID",
                "The domain result digest does not match its governed snapshot.",
                "Reject the result and repair canonical digest propagation in the domain adapter.",
            )
        validate_external_admin_result(context, response, command_spec(context.kind))

    def _require_pii_approved_dataset(
        self,
        context: AdminCommandExecutionContext,
        *,
        connection: Any,
        lock: bool,
    ) -> None:
        dataset_id = str(context.payload.get("datasetId") or context.target_id)
        try:
            require_pii_approved_dataset(
                connection,
                self.codec,
                tenant_id=context.tenant_id,
                dataset_id=dataset_id,
                lock=lock,
            )
        except EvaluationDatasetMissing as error:
            raise AdminCommandExecutionRejected(
                "EVALUATION_DATASET_STALE",
                "The evaluation dataset is unavailable.",
                "Reload the dataset and submit a new reviewed comparison.",
            ) from error
        except EvaluationDatasetNotApproved as error:
            raise AdminCommandExecutionRejected(
                "EVALUATION_DATASET_PII_NOT_APPROVED",
                "The evaluation dataset does not have a verified PII PASS decision.",
                "Complete independent PII review before running the comparison.",
            ) from error

    def _load_context(
        self, outbox_lease: OutboxLease, *, require_live_lease: bool = True
    ) -> AdminCommandExecutionContext:
        if (
            outbox_lease.topic != "ADMIN_CONTROL_COMMAND"
            or outbox_lease.aggregate_type != "ADMIN_CONTROL_COMMAND"
        ):
            raise ValueError("The outbox intent is not an admin control command.")
        try:
            command_id = UUID(outbox_lease.aggregate_id)
        except ValueError as error:
            raise ValueError("The admin command aggregate ID is invalid.") from error
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT c.*, o.state AS outbox_state, o.generation AS outbox_generation,
                          o.lease_token AS outbox_lease_token,
                          o.lease_expires_at AS outbox_lease_expires_at,
                          CURRENT_TIMESTAMP AS database_now
                     FROM ai_admin_control_commands c
                     JOIN ai_transactional_outbox o
                       ON o.tenant_id = c.tenant_id
                      AND o.aggregate_id = c.command_id::text
                    WHERE c.tenant_id = %s AND c.command_id = %s
                      AND o.outbox_id = %s
                    FOR UPDATE OF c, o""",
                (outbox_lease.tenant_id, command_id, outbox_lease.outbox_id),
            ).fetchone()
            if row is None:
                raise AdminCommandExecutionRejected(
                    "ADMIN_INTENT_BINDING_INVALID",
                    "The admin outbox intent is not bound to this tenant and command.",
                    "Quarantine the intent and reconcile the immutable command ledger.",
                )
            if require_live_lease and not (
                row["outbox_state"] == "CLAIMED"
                and int(row["outbox_generation"]) == outbox_lease.generation
                and row["outbox_lease_token"] == outbox_lease.lease_token
                and row["outbox_lease_expires_at"] > row["database_now"]
            ):
                raise AdminCommandExecutionTransient("The admin command outbox lease is stale or expired.")
            if not require_live_lease and not (
                row["outbox_state"] == "DEAD_LETTER"
                and int(row["outbox_generation"]) == outbox_lease.generation
            ):
                raise AdminCommandExecutionTransient(
                    "The admin command dead-letter fence is stale."
                )
            rollback_row = connection.execute(
                """SELECT event_id, evidence_envelope
                     FROM ai_admin_control_command_events
                    WHERE tenant_id = %s AND command_id = %s
                      AND event_type = 'ROLLBACK_REQUESTED'
                    ORDER BY occurred_at DESC LIMIT 1""",
                (outbox_lease.tenant_id, command_id),
            ).fetchone()
        payload = self._decrypt(command_id, outbox_lease.tenant_id, "payload", row["payload_envelope"])
        review = self._decrypt(command_id, outbox_lease.tenant_id, "review", row["review_envelope"])
        attempt_id, kind = self._verify_intent(outbox_lease, row, command_id)
        if row["approval_required"] and row["command_state"] in {"QUEUED", "RUNNING"} and not row["checker_user_id"]:
            raise AdminCommandExecutionRejected(
                "ADMIN_APPROVAL_EVIDENCE_MISSING",
                "A high-risk admin command cannot execute without independent approval evidence.",
                "Return the command to maker-checker review.",
            )
        rollback_ref = self._rollback_ref(rollback_row, outbox_lease.tenant_id)
        command_spec(kind)
        return AdminCommandExecutionContext(
            command_id=command_id,
            attempt_id=attempt_id,
            tenant_id=outbox_lease.tenant_id,
            maker_user_id=row["maker_user_id"],
            correlation_id=row["correlation_id"],
            kind=kind,
            state=GovernedCommandState(row["command_state"]),
            command_revision=int(row["revision"]),
            target_type=row["target_type"],
            target_id=row["target_id"],
            expected_target_version=int(row["expected_target_version"]),
            payload=payload,
            review=review,
            rollback_requested=rollback_row is not None,
            rollback_source_receipt_ref=rollback_ref,
        )

    def _verify_intent(
        self, outbox_lease: OutboxLease, row: Any, command_id: UUID
    ) -> tuple[UUID, GovernedCommandKind]:
        intent = outbox_lease.payload
        try:
            attempt_id = UUID(str(intent.get("attemptId")))
            kind = GovernedCommandKind(str(intent.get("kind")))
        except (ValueError, TypeError) as error:
            raise self._invalid_intent() from error
        expected = {
            "commandId": str(command_id),
            "kind": row["command_kind"],
            "targetType": row["target_type"],
            "targetId": row["target_id"],
            "expectedVersion": int(row["expected_target_version"]),
        }
        if any(intent.get(key) != value for key, value in expected.items()):
            raise self._invalid_intent()
        return attempt_id, kind

    @staticmethod
    def _invalid_intent() -> AdminCommandExecutionRejected:
        return AdminCommandExecutionRejected(
            "ADMIN_INTENT_PAYLOAD_INVALID",
            "The admin outbox payload does not match its governed command.",
            "Quarantine the intent and recreate it from the governed command ledger.",
        )

    def _decrypt(self, command_id: UUID, tenant_id: int, field: str, envelope: str) -> dict[str, object]:
        return self.codec.decrypt_json(
            envelope,
            tenant_id=tenant_id,
            resource_type="admin-control-command",
            resource_id=str(command_id),
            field=field,
        )

    def _rollback_ref(self, row: Any | None, tenant_id: int) -> str | None:
        if row is None or not row["evidence_envelope"]:
            return None
        evidence = self.codec.decrypt_json(
            row["evidence_envelope"],
            tenant_id=tenant_id,
            resource_type="admin-control-command",
            resource_id=str(row["event_id"]),
            field="event-evidence",
        )
        return str(evidence.get("rollbackSourceReceiptRef") or "") or None


def _observation_id(outbox_id: UUID, phase: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"dwp-admin-control:{outbox_id}:{phase}")


__all__ = ["PostgresAdminControlCommandExecutor", "admin_command_capabilities"]
