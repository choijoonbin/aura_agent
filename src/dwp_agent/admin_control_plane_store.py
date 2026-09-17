from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .admin_control_plane_contracts import (
    CommandDecision,
    CommandProblem,
    CommandReceipt,
    CommandReview,
    CreateGovernedCommandRequest,
    GovernedCommand,
    GovernedCommandDecisionRequest,
    GovernedCommandKind,
    GovernedCommandRestartRequest,
    GovernedCommandState,
    GovernedCommandTransitionRequest,
)
from .admin_control_plane_errors import (
    AdminControlPlaneConflict,
    AdminControlPlaneDenied,
    AdminControlPlaneNotFound,
    AdminControlPlaneUnavailable,
)
from .admin_control_plane_registry import (
    ADMIN_COMMAND_REGISTRY,
    AdminCommandResourceStrategy,
    command_spec,
)
from .canonical_json import canonical_json_bytes
from .admin_evaluation_gate import EvaluationDatasetMissing, EvaluationDatasetNotApproved, require_pii_approved_dataset
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedFingerprints,
    GovernedPayloadCodec,
    advisory_lock,
    require_command_replay,
)
from .transactional_outbox import enqueue_internal_intent


_CREATE_KINDS = {
    kind for kind, spec in ADMIN_COMMAND_REGISTRY.items()
    if spec.resource_strategy == AdminCommandResourceStrategy.CREATE_TARGET
}
_SIMULATION_KINDS = {
    GovernedCommandKind.MODEL_ROUTE_SIMULATE,
    GovernedCommandKind.EMERGENCY_RECOVERY_SIMULATE,
    GovernedCommandKind.SAFETY_SIMULATE,
    GovernedCommandKind.COST_SIMULATE,
}
_PII_GATED_EVALUATION_KINDS = {
    GovernedCommandKind.EVALUATION_COMPARE,
    GovernedCommandKind.EVALUATION_RUN,
    GovernedCommandKind.EVALUATION_RERUN,
}


class AdminControlPlaneStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise AdminControlPlaneUnavailable("Control-plane encryption is unavailable.") from error

    def create(
        self, *, tenant_id: int, actor_user_id: str, correlation_id: str,
        auth_session_id: str, request: CreateGovernedCommandRequest,
    ) -> GovernedCommand:
        proof = self.fingerprints.command(
            tenant_id=tenant_id, session_id=auth_session_id,
            purpose="admin-control-command:create",
            payload=request.model_dump(mode="json", by_alias=True),
        )
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(connection, "admin-control", tenant_id, request.command_id)
                existing = connection.execute(
                    _SELECT + " WHERE c.tenant_id = %s AND c.command_id = %s",
                    (tenant_id, request.command_id),
                ).fetchone()
                if existing is not None:
                    require_command_replay(
                        existing["auth_session_fingerprint"],
                        existing["request_fingerprint"], proof,
                    )
                    return self._record(existing, actor_user_id)
                if request.kind in _PII_GATED_EVALUATION_KINDS:
                    try:
                        require_pii_approved_dataset(
                            connection,
                            self.codec,
                            tenant_id=tenant_id,
                            dataset_id=str(
                                request.payload.get("datasetId") or request.target.id
                            ),
                            lock=True,
                        )
                    except EvaluationDatasetMissing as error:
                        raise AdminControlPlaneNotFound(str(error)) from error
                    except EvaluationDatasetNotApproved as error:
                        raise AdminControlPlaneDenied(str(error)) from error
                snapshot, current_version = self._target_snapshot(
                    connection, tenant_id, request.target.type, request.target.id,
                    kind=request.kind,
                    expected_version=request.expected_version,
                    allow_absent=request.kind in _CREATE_KINDS or request.kind in _SIMULATION_KINDS,
                )
                spec = command_spec(request.kind)
                unversioned_target = (
                    spec.allow_unversioned_target and request.expected_version == 0
                )
                if current_version != request.expected_version and not unversioned_target:
                    raise AdminControlPlaneConflict(
                        "The control-plane target changed. Reload its authoritative snapshot."
                    )
                if request.kind in _CREATE_KINDS and current_version != 0:
                    raise AdminControlPlaneConflict("The requested target already exists.")
                snapshot_hash = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
                review = CommandReview(
                    reason=request.reason, ticket_ref=request.ticket_ref,
                    evidence_refs=request.evidence_refs, preflight=request.preflight,
                )
                state = (GovernedCommandState.QUEUED if request.kind in _SIMULATION_KINDS
                         else GovernedCommandState.AWAITING_APPROVAL)
                problem = None
                row = connection.execute(
                    """INSERT INTO ai_admin_control_commands (
                           command_id, tenant_id, maker_user_id, correlation_id,
                           auth_session_fingerprint, request_fingerprint, command_kind,
                           target_type, target_id, expected_target_version,
                           target_snapshot_hash, command_state, approval_required,
                           review_envelope, payload_envelope, problem_envelope)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s)
                    RETURNING *""",
                    (
                        request.command_id, tenant_id, actor_user_id, correlation_id,
                        proof.session_fingerprint, proof.request_fingerprint,
                        request.kind.value, request.target.type, request.target.id,
                        current_version, snapshot_hash, state.value,
                        request.kind not in _SIMULATION_KINDS,
                        self._encrypt(tenant_id, request.command_id, "review", review.model_dump(mode="json", by_alias=True)),
                        self._encrypt(tenant_id, request.command_id, "payload", request.payload),
                        self._encrypt(tenant_id, request.command_id, "problem", problem.model_dump(mode="json", by_alias=True)) if problem else None,
                    ),
                ).fetchone()
                self._event(connection, row, request.command_id, actor_user_id, correlation_id, "CREATED", None, {})
                if request.kind in _SIMULATION_KINDS:
                    self._enqueue(connection, row, request.command_id)
                return self._record(row, actor_user_id)
        except GovernedDomainConflict as error:
            raise AdminControlPlaneConflict(str(error)) from error
        except (
            AdminControlPlaneConflict,
            AdminControlPlaneDenied,
            AdminControlPlaneNotFound,
        ):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise AdminControlPlaneUnavailable("Control-plane command storage is unavailable.") from error

    def get(self, *, tenant_id: int, actor_user_id: str, command_id: UUID) -> GovernedCommand:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    _SELECT + " WHERE c.tenant_id = %s AND c.command_id = %s",
                    (tenant_id, command_id),
                ).fetchone()
                if row is None:
                    raise AdminControlPlaneNotFound("The governed command is unavailable.")
                return self._record(row, actor_user_id)
        except AdminControlPlaneNotFound:
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise AdminControlPlaneUnavailable("Control-plane command storage is unavailable.") from error

    def list(
        self, *, tenant_id: int, actor_user_id: str,
        state: GovernedCommandState | None, limit: int,
    ) -> list[GovernedCommand]:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    _SELECT + " WHERE c.tenant_id = %s AND (%s IS NULL OR c.command_state = %s) "
                    "ORDER BY c.created_at DESC LIMIT %s",
                    (tenant_id, state.value if state else None, state.value if state else None, limit),
                ).fetchall()
                return [self._record(row, actor_user_id) for row in rows]
        except (PsycopgError, ValueError, TypeError) as error:
            raise AdminControlPlaneUnavailable("Control-plane command storage is unavailable.") from error

    def decide(
        self, *, tenant_id: int, actor_user_id: str, correlation_id: str,
        command_id: UUID, request: GovernedCommandDecisionRequest,
    ) -> GovernedCommand:
        def transition(connection: Any, row: Any) -> tuple[GovernedCommandState, dict[str, object]]:
            _require_independent_checker(row["maker_user_id"], actor_user_id)
            if row["command_state"] != GovernedCommandState.AWAITING_APPROVAL.value:
                raise AdminControlPlaneConflict("This command is no longer awaiting a decision.")
            decision = CommandDecision(
                decision=request.decision, actor_user_id=actor_user_id,
                reason=request.reason, evidence_refs=request.evidence_refs,
                decided_at=datetime.now(UTC),
            )
            next_state = (
                GovernedCommandState.QUEUED
                if request.decision == "APPROVE" else GovernedCommandState.REJECTED
            )
            if next_state == GovernedCommandState.QUEUED:
                self._enqueue(connection, row, request.command_id)
            return next_state, {"decision": decision.model_dump(mode="json", by_alias=True)}

        return self._transition(
            tenant_id, actor_user_id, correlation_id, command_id,
            request.command_id, request.expected_version,
            f"DECISION_{request.decision}", transition,
        )

    def cancel(
        self, *, tenant_id: int, actor_user_id: str, correlation_id: str,
        command_id: UUID, request: GovernedCommandTransitionRequest,
    ) -> GovernedCommand:
        def transition(_: Any, row: Any) -> tuple[GovernedCommandState, dict[str, object]]:
            if row["command_state"] not in {"AWAITING_APPROVAL", "QUEUED", "RUNNING"}:
                raise AdminControlPlaneConflict("This command cannot be cancelled.")
            return GovernedCommandState.CANCELLED, {
                "reason": request.reason, "evidenceRefs": request.evidence_refs,
            }

        return self._transition(tenant_id, actor_user_id, correlation_id, command_id,
                                request.command_id, request.expected_version, "CANCELLED", transition)

    def retry(
        self, *, tenant_id: int, actor_user_id: str, correlation_id: str,
        command_id: UUID, request: GovernedCommandRestartRequest,
    ) -> GovernedCommand:
        def transition(connection: Any, row: Any) -> tuple[GovernedCommandState, dict[str, object]]:
            if row["command_state"] not in {"PARTIAL", "FAILED", "CANCELLED"}:
                raise AdminControlPlaneConflict("Only incomplete commands may be retried.")
            self._enqueue(connection, row, request.command_id)
            return GovernedCommandState.QUEUED, {
                "reason": request.reason, "evidenceRefs": request.evidence_refs,
            }

        return self._transition(tenant_id, actor_user_id, correlation_id, command_id,
                                request.command_id, request.expected_version, "RETRY_QUEUED", transition)

    def rollback(
        self, *, tenant_id: int, actor_user_id: str, correlation_id: str,
        command_id: UUID, request: GovernedCommandRestartRequest,
    ) -> GovernedCommand:
        def transition(connection: Any, row: Any) -> tuple[GovernedCommandState, dict[str, object]]:
            if row["command_state"] != GovernedCommandState.SUCCEEDED.value:
                raise AdminControlPlaneConflict("Only a completed command may request rollback.")
            if not row["receipt_envelope"]:
                raise AdminControlPlaneConflict("The completed command receipt is unavailable.")
            receipt = self._decrypt(
                tenant_id, command_id, "receipt", row["receipt_envelope"]
            )
            snapshot, current_version = self._target_snapshot(
                connection,
                tenant_id,
                row["target_type"],
                row["target_id"],
                kind=GovernedCommandKind(row["command_kind"]),
                expected_version=None,
                allow_absent=False,
            )
            snapshot_hash = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
            return GovernedCommandState.AWAITING_APPROVAL, {
                "reason": request.reason, "evidenceRefs": request.evidence_refs,
                "rollbackRequested": True,
                "rollbackSourceReceiptRef": receipt.get("domainReceiptRef"),
                "__commandUpdates": {
                    "makerUserId": actor_user_id,
                    "checkerUserId": None,
                    "decisionEnvelope": None,
                    "expectedTargetVersion": current_version,
                    "targetSnapshotHash": snapshot_hash,
                },
            }

        return self._transition(tenant_id, actor_user_id, correlation_id, command_id,
                                request.command_id, request.expected_version,
                                "ROLLBACK_REQUESTED", transition, reset_completion=True)

    def _transition(
        self, tenant_id: int, actor_user_id: str, correlation_id: str,
        command_id: UUID, attempt_id: UUID, expected_version: int, event_type: str,
        apply: Any, *, reset_completion: bool = False,
    ) -> GovernedCommand:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    _SELECT + " WHERE c.tenant_id = %s AND c.command_id = %s FOR UPDATE",
                    (tenant_id, command_id),
                ).fetchone()
                if row is None:
                    raise AdminControlPlaneNotFound("The governed command is unavailable.")
                replay = connection.execute(
                    "SELECT event_type FROM ai_admin_control_command_events WHERE tenant_id = %s AND transition_command_id = %s",
                    (tenant_id, attempt_id),
                ).fetchone()
                if replay is not None:
                    if replay["event_type"] != event_type:
                        raise AdminControlPlaneConflict("The transition command ID is already bound.")
                    return self._record(row, actor_user_id)
                if int(row["revision"]) != expected_version:
                    raise AdminControlPlaneConflict("The command version changed. Reload and retry.")
                previous = row["command_state"]
                next_state, evidence = apply(connection, row)
                command_updates = evidence.pop("__commandUpdates", {})
                decision_envelope = row["decision_envelope"]
                checker_user_id = row["checker_user_id"]
                if "decision" in evidence:
                    decision_envelope = self._encrypt(tenant_id, command_id, "decision", evidence["decision"])
                    checker_user_id = actor_user_id
                updated = connection.execute(
                    """UPDATE ai_admin_control_commands
                          SET command_state = %s, revision = revision + 1,
                              maker_user_id = %s, checker_user_id = %s,
                              decision_envelope = %s,
                              expected_target_version = %s,
                              target_snapshot_hash = %s,
                              receipt_envelope = CASE WHEN %s THEN NULL ELSE receipt_envelope END,
                              completed_at = CASE WHEN %s THEN NULL ELSE completed_at END,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE tenant_id = %s AND command_id = %s
                    RETURNING *""",
                    (
                     next_state.value,
                     command_updates.get("makerUserId", row["maker_user_id"]),
                     command_updates.get("checkerUserId", checker_user_id),
                     command_updates.get("decisionEnvelope", decision_envelope),
                     command_updates.get("expectedTargetVersion", row["expected_target_version"]),
                     command_updates.get("targetSnapshotHash", row["target_snapshot_hash"]),
                     reset_completion, reset_completion, tenant_id, command_id),
                ).fetchone()
                self._event(connection, updated, attempt_id, actor_user_id, correlation_id,
                            event_type, previous, evidence)
                return self._record(updated, actor_user_id)
        except (AdminControlPlaneConflict, AdminControlPlaneDenied, AdminControlPlaneNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise AdminControlPlaneUnavailable("Control-plane transition storage is unavailable.") from error

    def _target_snapshot(
        self, connection: Any, tenant_id: int, resource_type: str,
        resource_id: str, *, kind: GovernedCommandKind,
        expected_version: int | None, allow_absent: bool,
    ) -> tuple[dict[str, object], int]:
        spec = command_spec(kind)
        if spec.resource_strategy == AdminCommandResourceStrategy.DELEGATED_TARGET:
            if expected_version is None:
                raise AdminControlPlaneConflict(
                    "A delegated command requires an explicit provider target version."
                )
            return {
                "resourceType": resource_type,
                "resourceId": resource_id,
                "delegatedExpectedVersion": expected_version,
            }, expected_version
        if spec.resource_strategy == AdminCommandResourceStrategy.AI_POLICY:
            policy = connection.execute(
                """SELECT policy_version, emergency_disabled, allowed_model_routes,
                          budget_enforcement_mode, period_token_limit,
                          evaluation_gate_status, updated_at
                     FROM ai_execution_policies WHERE tenant_id = %s""",
                (tenant_id,),
            ).fetchone()
            if policy is not None:
                snapshot = {key: (value.isoformat() if isinstance(value, datetime) else value)
                            for key, value in dict(policy).items()}
                return snapshot, int(policy["policy_version"])
            if allow_absent:
                return {"resourceType": resource_type, "resourceId": resource_id, "absent": True}, 0
            raise AdminControlPlaneNotFound("The tenant AI execution policy is unavailable.")
        if (
            spec.resource_strategy == AdminCommandResourceStrategy.RESULT
            and expected_version == 0
        ):
            return {"resourceType": resource_type, "resourceId": resource_id, "absent": True}, 0
        row = connection.execute(
            """SELECT resource_version, snapshot_hash, snapshot_envelope
                 FROM ai_admin_control_resources
                WHERE tenant_id = %s AND resource_type = %s AND resource_id = %s""",
            (tenant_id, resource_type, resource_id),
        ).fetchone()
        if row is not None:
            return {"authoritativeSnapshotHash": row["snapshot_hash"]}, int(row["resource_version"])
        if allow_absent:
            return {"resourceType": resource_type, "resourceId": resource_id, "absent": True}, 0
        raise AdminControlPlaneNotFound("The authoritative target snapshot is unavailable.")


    def _record(self, row: Any, actor_user_id: str) -> GovernedCommand:
        command_id = row["command_id"]
        tenant_id = row["tenant_id"]
        state = GovernedCommandState(row["command_state"])
        review = CommandReview.model_validate(self._decrypt(tenant_id, command_id, "review", row["review_envelope"]))
        decision = CommandDecision.model_validate(
            self._decrypt(tenant_id, command_id, "decision", row["decision_envelope"])
        ) if row["decision_envelope"] else None
        receipt = CommandReceipt.model_validate(
            self._decrypt(tenant_id, command_id, "receipt", row["receipt_envelope"])
        ) if row["receipt_envelope"] else None
        problem = CommandProblem.model_validate(
            self._decrypt(tenant_id, command_id, "problem", row["problem_envelope"])
        ) if row["problem_envelope"] else None
        transitions, block = _allowed_transitions(state, row["maker_user_id"], actor_user_id)
        return GovernedCommand(
            command_id=command_id, kind=row["command_kind"], state=state,
            target={"type": row["target_type"], "id": row["target_id"]},
            expected_version=row["expected_target_version"],
            maker_user_id=row["maker_user_id"], checker_user_id=row["checker_user_id"],
            approval_required=row["approval_required"], review=review,
            allowed_transitions=transitions, transition_block_reason=block,
            can_approve="APPROVE" in transitions, progress_percent=row["progress_percent"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            receipt=receipt, problem=problem, version=row["revision"], decision=decision,
        )

    def _enqueue(self, connection: Any, row: Any, attempt_id: UUID) -> None:
        enqueue_internal_intent(
            connection, codec=self.codec, fingerprints=self.fingerprints,
            tenant_id=row["tenant_id"], user_id=row["maker_user_id"],
            topic="ADMIN_CONTROL_COMMAND", aggregate_type="ADMIN_CONTROL_COMMAND",
            aggregate_id=str(row["command_id"]),
            payload={"commandId": str(row["command_id"]), "attemptId": str(attempt_id),
                     "kind": row["command_kind"], "targetType": row["target_type"],
                     "targetId": row["target_id"], "expectedVersion": row["expected_target_version"]},
            retention_until=datetime.now(UTC) + timedelta(days=90),
        )

    def _event(self, connection: Any, row: Any, attempt_id: UUID, actor: str,
               correlation: str, event_type: str, previous: str | None,
               evidence: dict[str, object]) -> None:
        event_id = uuid4()
        envelope = self._encrypt(row["tenant_id"], event_id, "event-evidence", evidence) if evidence else None
        connection.execute(
            """INSERT INTO ai_admin_control_command_events (
                   event_id, command_id, tenant_id, transition_command_id,
                   actor_user_id, correlation_id, event_type, previous_state,
                   current_state, revision, evidence_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (event_id, row["command_id"], row["tenant_id"], attempt_id, actor,
             correlation, event_type, previous, row["command_state"], row["revision"], envelope),
        )

    def _encrypt(self, tenant_id: int, resource_id: UUID, field: str,
                 payload: dict[str, object]) -> str:
        return self.codec.encrypt_json(payload, tenant_id=tenant_id,
                                       resource_type="admin-control-command",
                                       resource_id=str(resource_id), field=field)

    def _decrypt(self, tenant_id: int, resource_id: UUID, field: str, envelope: str) -> dict[str, object]:
        return self.codec.decrypt_json(envelope, tenant_id=tenant_id,
                                       resource_type="admin-control-command",
                                       resource_id=str(resource_id), field=field)


def _allowed_transitions(state: GovernedCommandState, maker: str, actor: str) -> tuple[list[str], str | None]:
    if state == GovernedCommandState.AWAITING_APPROVAL:
        if maker == actor:
            return ["CANCEL"], "Maker-checker separation prevents self-approval."
        return ["APPROVE", "REJECT", "CANCEL"], None
    if state in {GovernedCommandState.QUEUED, GovernedCommandState.RUNNING}:
        return ["CANCEL"], None
    if state in {GovernedCommandState.PARTIAL, GovernedCommandState.FAILED, GovernedCommandState.CANCELLED}:
        return ["RETRY"], None
    if state == GovernedCommandState.SUCCEEDED:
        return ["ROLLBACK"], None
    return [], "This command is terminal."


def _require_independent_checker(maker: str, checker: str) -> None:
    if maker == checker:
        raise AdminControlPlaneDenied("Maker-checker separation prevents self-approval.")


@lru_cache(maxsize=1)
def get_admin_control_plane_store() -> AdminControlPlaneStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise AdminControlPlaneUnavailable("Control-plane command storage is unavailable.")
    return AdminControlPlaneStore(database_url)


_SELECT = "SELECT c.* FROM ai_admin_control_commands c"
