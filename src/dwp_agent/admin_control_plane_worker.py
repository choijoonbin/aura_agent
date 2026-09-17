from __future__ import annotations
import hashlib
import json
import os
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4
from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row
from .admin_control_plane_contracts import (
    CommandReceipt,
    GovernedCommand,
    GovernedCommandObservation,
    GovernedCommandState,
)
from .admin_control_plane_adapters import AdminCommandExecutionRejected
from .admin_control_plane_errors import (
    AdminControlPlaneConflict,
    AdminControlPlaneNotFound,
    AdminControlPlaneUnavailable,
)
from .admin_control_plane_registry import (
    AdminCommandResourceStrategy,
    command_spec,
)
from .admin_control_plane_result_validation import (
    result_validator,
    validate_external_admin_observation,
)
from .admin_control_plane_store import AdminControlPlaneStore
from .ai_control_audit import record_ai_control_event
from .ai_control_contracts import EvaluationEvidenceState, TenantAIExecutionPolicy
from .canonical_json import canonical_json_bytes
from .governed_domain_core import GovernedPayloadCodec
class AdminControlPlaneWorkerStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            self.codec = GovernedPayloadCodec()
            self.public_store = AdminControlPlaneStore(database_url)
        except Exception as error:
            raise AdminControlPlaneUnavailable("Control-plane worker encryption is unavailable.") from error
    def observe(
        self, *, tenant_id: int, worker_id: str, correlation_id: str,
        governed_command_id: UUID, request: GovernedCommandObservation,
    ) -> GovernedCommand:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    "SELECT * FROM ai_admin_control_commands WHERE tenant_id = %s AND command_id = %s FOR UPDATE",
                    (tenant_id, governed_command_id),
                ).fetchone()
                if row is None:
                    raise AdminControlPlaneNotFound("The governed command is unavailable.")
                replay = connection.execute(
                    "SELECT event_type, current_state FROM ai_admin_control_command_events WHERE tenant_id = %s AND transition_command_id = %s",
                    (tenant_id, request.command_id),
                ).fetchone()
                if replay is not None:
                    if replay["current_state"] != request.state.value:
                        raise AdminControlPlaneConflict("The worker command ID is already bound.")
                    return self.public_store._record(row, worker_id)
                if int(row["revision"]) != request.expected_version:
                    raise AdminControlPlaneConflict("The command version changed before worker observation.")
                self._require_transition(row["command_state"], request.state)
                event_id = uuid4()
                receipt_envelope = problem_envelope = None
                completed_at = None
                if request.state in {GovernedCommandState.SUCCEEDED, GovernedCommandState.ROLLED_BACK}:
                    spec = command_spec(row["command_kind"])
                    attempt = connection.execute(
                        """SELECT transition_command_id FROM ai_admin_control_command_events
                            WHERE tenant_id = %s AND command_id = %s
                              AND current_state = 'QUEUED'
                            ORDER BY revision DESC LIMIT 1""",
                        (tenant_id, governed_command_id),
                    ).fetchone()
                    if (request.tenant_id != int(row["tenant_id"])
                            or request.correlation_id != row["correlation_id"]
                            or attempt is None
                            or request.attempt_id != attempt["transition_command_id"]):
                        raise AdminControlPlaneConflict(
                            "The worker result context is not bound to the governed command.")
                    self._validate_result_snapshot(row, request, spec)
                    self._verify_target_binding(connection, row, spec)
                    if (
                        spec.resource_strategy == AdminCommandResourceStrategy.AI_POLICY
                        and spec.result_resource_type is None
                    ):
                        self._apply_ai_policy_mutation(connection, row, request, spec.operation)
                    self._verify_result_version(connection, row, request, spec)
                    self._write_resource(connection, row, request, spec)
                    receipt = CommandReceipt(
                        receipt_id=uuid4(), audit_event_id=event_id,
                        completed_at=datetime.now(UTC), result_summary=request.result_summary or "",
                        domain_receipt_ref=request.domain_receipt_ref,
                        rollback_ref=request.rollback_ref
                        if request.state == GovernedCommandState.ROLLED_BACK else None,
                    )
                    receipt_envelope = self._encrypt(tenant_id, governed_command_id, "receipt", receipt.model_dump(mode="json", by_alias=True))
                    completed_at = receipt.completed_at
                elif request.problem is not None:
                    problem_envelope = self._encrypt(
                        tenant_id, governed_command_id, "problem",
                        request.problem.model_dump(mode="json", by_alias=True),
                    )
                updated = connection.execute(
                    """UPDATE ai_admin_control_commands
                          SET command_state = %s, revision = revision + 1,
                              progress_percent = %s, receipt_envelope = %s,
                              problem_envelope = %s, completed_at = %s,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE tenant_id = %s AND command_id = %s
                    RETURNING *""",
                    (request.state.value, request.progress_percent, receipt_envelope,
                     problem_envelope, completed_at, tenant_id, governed_command_id),
                ).fetchone()
                evidence = {
                    "domainReceiptRef": request.domain_receipt_ref,
                    "resultVersion": request.result_version,
                    "resultSnapshotSha256": hashlib.sha256(
                        canonical_json_bytes(request.result_snapshot or {})
                    ).hexdigest() if request.result_snapshot is not None else None,
                    "problem": request.problem.model_dump(mode="json", by_alias=True)
                    if request.problem else None,
                }
                evidence_envelope = self._encrypt(tenant_id, event_id, "event-evidence", evidence)
                connection.execute(
                    """INSERT INTO ai_admin_control_command_events (
                           event_id, command_id, tenant_id, transition_command_id,
                           actor_user_id, correlation_id, event_type, previous_state,
                           current_state, revision, evidence_envelope)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (event_id, governed_command_id, tenant_id, request.command_id,
                     worker_id, correlation_id, "WORKER_OBSERVED", row["command_state"],
                     request.state.value, updated["revision"], evidence_envelope),
                )
                return self.public_store._record(updated, worker_id)
        except (AdminControlPlaneConflict, AdminControlPlaneNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise AdminControlPlaneUnavailable("Control-plane worker storage is unavailable.") from error
    @staticmethod
    def _require_transition(current: str, target: GovernedCommandState) -> None:
        allowed = {
            "QUEUED": {"RUNNING", "PARTIAL", "FAILED", "SUCCEEDED", "ROLLED_BACK"},
            "RUNNING": {"PARTIAL", "FAILED", "SUCCEEDED", "ROLLED_BACK"},
        }
        if target.value not in allowed.get(current, set()):
            raise AdminControlPlaneConflict("The worker state transition is not allowed.")
    @staticmethod
    def _verify_target_binding(connection: Any, row: Any, spec: Any) -> None:
        expected_version = int(row["expected_target_version"])
        if spec.resource_strategy == AdminCommandResourceStrategy.DELEGATED_TARGET:
            observed = expected_version
            snapshot = {
                "resourceType": row["target_type"],
                "resourceId": row["target_id"],
                "delegatedExpectedVersion": expected_version,
            }
        elif spec.resource_strategy == AdminCommandResourceStrategy.AI_POLICY:
            policy = connection.execute(
                """SELECT policy_version, emergency_disabled, allowed_model_routes,
                          budget_enforcement_mode, period_token_limit,
                          evaluation_gate_status, updated_at
                     FROM ai_execution_policies WHERE tenant_id = %s FOR UPDATE""",
                (row["tenant_id"],),
            ).fetchone()
            if policy is None:
                observed = 0
                snapshot = {
                    "resourceType": row["target_type"],
                    "resourceId": row["target_id"],
                    "absent": True,
                }
            else:
                observed = int(policy["policy_version"])
                snapshot = {
                    key: value.isoformat() if isinstance(value, datetime) else value
                    for key, value in dict(policy).items()
                }
        elif spec.resource_strategy == AdminCommandResourceStrategy.RESULT and expected_version == 0:
            observed = 0
            snapshot = {
                "resourceType": row["target_type"],
                "resourceId": row["target_id"],
                "absent": True,
            }
        else:
            resource = connection.execute(
                """SELECT resource_version, snapshot_hash FROM ai_admin_control_resources
                    WHERE tenant_id = %s AND resource_type = %s AND resource_id = %s FOR UPDATE""",
                (row["tenant_id"], row["target_type"], row["target_id"]),
            ).fetchone()
            observed = int(resource["resource_version"]) if resource else 0
            snapshot = (
                {"authoritativeSnapshotHash": resource["snapshot_hash"]}
                if resource is not None
                else {
                    "resourceType": row["target_type"],
                    "resourceId": row["target_id"],
                    "absent": True,
                }
            )
        if observed != expected_version:
            raise AdminControlPlaneConflict("The authoritative target changed during execution.")
        digest = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
        if digest != row["target_snapshot_hash"]:
            raise AdminControlPlaneConflict("The authoritative target snapshot binding changed.")
    @staticmethod
    def _verify_result_version(
        connection: Any, row: Any, request: GovernedCommandObservation, spec: Any
    ) -> None:
        if request.result_version is None:
            raise AdminControlPlaneConflict("The worker result version is unavailable.")
        if spec.resource_strategy.value == "AI_POLICY" and spec.mode.value == "EXTERNAL_ADAPTER":
            previous = int(row["expected_target_version"])
        elif spec.result_resource_type is not None:
            resource_type, resource_id = spec.output_resource(
                target_type=row["target_type"],
                target_id=row["target_id"],
                command_id=str(row["command_id"]),
            )
            resource = connection.execute(
                """SELECT resource_version FROM ai_admin_control_resources
                    WHERE tenant_id = %s AND resource_type = %s AND resource_id = %s FOR UPDATE""",
                (row["tenant_id"], resource_type, resource_id),
            ).fetchone()
            previous = int(resource["resource_version"]) if resource else 0
        else:
            previous = int(row["expected_target_version"])
        if request.result_version != previous + 1:
            raise AdminControlPlaneConflict("The worker result version is not the next authoritative version.")
    def _validate_result_snapshot(self, row: Any, request: GovernedCommandObservation,
                                  spec: Any) -> None:
        snapshot = request.result_snapshot or {}
        if spec.mode.value == "EXTERNAL_ADAPTER":
            try:
                validate_external_admin_observation(
                    row=row, request=request, spec=spec,
                    payload=self.public_store._decrypt(
                        int(row["tenant_id"]), row["command_id"], "payload", row["payload_envelope"],
                    ),
                )
            except (AdminCommandExecutionRejected, ValueError, TypeError) as error:
                raise AdminControlPlaneConflict(
                    "The external result contract or identity binding is invalid.") from error
            return
        if request.state == GovernedCommandState.ROLLED_BACK and snapshot.get("tombstoned") is True:
            return
        if (
            spec.resource_strategy == AdminCommandResourceStrategy.AI_POLICY
            and spec.result_resource_type is None
        ):
            TenantAIExecutionPolicy.model_validate(snapshot)
            return
        resource_type, _ = spec.output_resource(
            target_type=row["target_type"],
            target_id=row["target_id"],
            command_id=str(row["command_id"]),
        )
        validator = result_validator(resource_type)
        if validator is not None:
            result = validator.model_validate(snapshot)
            semantic_version = getattr(result, "result_version", None)
            if semantic_version is None:
                semantic_version = getattr(result, "version", None)
            if semantic_version is not None and semantic_version != request.result_version:
                raise AdminControlPlaneConflict("The typed result version is inconsistent.")
    def _apply_ai_policy_mutation(
        self, connection: Any, row: Any, request: GovernedCommandObservation, operation: str
    ) -> None:
        if request.result_snapshot is None or request.result_version is None:
            raise AdminControlPlaneConflict("The AI policy result snapshot is unavailable.")
        desired = TenantAIExecutionPolicy.model_validate(request.result_snapshot)
        current = self._ai_policy(connection, int(row["tenant_id"]), lock=True)
        expected = int(row["expected_target_version"])
        if current.policy_version != expected or desired.policy_version != expected + 1:
            raise AdminControlPlaneConflict("The AI execution policy changed before execution.")
        if request.state == GovernedCommandState.ROLLED_BACK or operation == "EXTERNAL":
            result = connection.execute(
                """UPDATE ai_execution_policies SET
                       emergency_disabled = %s,
                       allowed_model_routes = %s::jsonb,
                       allowed_tool_keys = %s::jsonb,
                       allowed_knowledge_sources = %s::jsonb,
                       max_output_tokens_per_request = %s,
                       budget_enforcement_mode = %s,
                       period_token_limit = %s,
                       alert_threshold_percent = %s,
                       require_evaluation_pass = %s,
                       evaluation_gate_status = %s,
                       evaluation_observed_at = %s,
                       evaluation_policy_version = %s,
                       policy_version = policy_version + 1,
                       updated_by = %s, updated_at = CURRENT_TIMESTAMP
                     WHERE tenant_id = %s AND policy_version = %s""",
                (
                    desired.emergency_disabled,
                    json.dumps([route.model_dump(mode="json") for route in desired.allowed_model_routes]),
                    json.dumps(desired.allowed_tool_keys),
                    json.dumps(desired.allowed_knowledge_sources),
                    desired.max_output_tokens_per_request,
                    desired.budget_enforcement_mode.value,
                    desired.period_token_limit,
                    desired.alert_threshold_percent,
                    desired.require_evaluation_pass,
                    desired.evaluation_gate_status.value,
                    desired.evaluation_observed_at,
                    desired.evaluation_policy_version,
                    self._worker_actor(row),
                    row["tenant_id"],
                    expected,
                ),
            )
            event_type = (
                "ai-control.admin-command-rolled-back"
                if request.state == GovernedCommandState.ROLLED_BACK
                else "ai-control.admin-external-command-applied"
            )
        elif operation == "MODEL_ROUTING_UPDATE":
            result = connection.execute(
                """UPDATE ai_execution_policies SET
                       allowed_model_routes = %s::jsonb,
                       policy_version = policy_version + 1,
                       updated_by = %s, updated_at = CURRENT_TIMESTAMP
                     WHERE tenant_id = %s AND policy_version = %s""",
                (
                    json.dumps([
                        route.model_dump(mode="json")
                        for route in desired.allowed_model_routes
                    ]),
                    self._worker_actor(row),
                    row["tenant_id"],
                    expected,
                ),
            )
            event_type = "ai-control.admin-routing-updated"
        elif operation in {"EMERGENCY_STOP", "EMERGENCY_RECOVERY"}:
            disabled = operation == "EMERGENCY_STOP"
            if desired.emergency_disabled is not disabled:
                raise AdminControlPlaneConflict("The emergency control result is inconsistent.")
            result = connection.execute(
                """UPDATE ai_execution_policies
                      SET emergency_disabled = %s, policy_version = policy_version + 1,
                          updated_by = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND policy_version = %s""",
                (disabled, self._worker_actor(row), row["tenant_id"], expected),
            )
            event_type = "ai-control.admin-emergency-stopped" if disabled else "ai-control.admin-emergency-recovered"
        elif operation == "TOKEN_BUDGET":
            result = connection.execute(
                """UPDATE ai_execution_policies
                      SET budget_enforcement_mode = %s, period_token_limit = %s,
                          policy_version = policy_version + 1,
                          updated_by = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND policy_version = %s""",
                (
                    desired.budget_enforcement_mode.value,
                    desired.period_token_limit,
                    self._worker_actor(row),
                    row["tenant_id"],
                    expected,
                ),
            )
            event_type = "ai-control.admin-token-budget-updated"
        else:
            raise AdminControlPlaneConflict("The internal AI policy operation is unsupported.")
        if result.rowcount != 1:
            raise AdminControlPlaneConflict("The AI execution policy changed during execution.")
        observed = self._ai_policy(connection, int(row["tenant_id"]), lock=False)
        review = self.public_store._decrypt(
            row["tenant_id"], row["command_id"], "review", row["review_envelope"]
        )
        record_ai_control_event(
            connection,
            int(row["tenant_id"]),
            event_type,
            self._worker_actor(row),
            row["correlation_id"],
            str(review.get("reason", "Governed DWAI-ON admin command."))[:500],
            current,
            observed,
            target_key=str(row["command_id"]),
        )
    @staticmethod
    def _ai_policy(connection: Any, tenant_id: int, *, lock: bool) -> TenantAIExecutionPolicy:
        row = connection.execute(
            """SELECT emergency_disabled, allowed_model_routes, allowed_tool_keys,
                      allowed_knowledge_sources, max_output_tokens_per_request,
                      budget_enforcement_mode, period_token_limit, alert_threshold_percent,
                      require_evaluation_pass, evaluation_gate_status,
                      evaluation_observed_at, evaluation_policy_version,
                      policy_version, updated_at
                 FROM ai_execution_policies WHERE tenant_id = %s"""
            + (" FOR UPDATE" if lock else ""),
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise AdminControlPlaneConflict("The tenant AI execution policy is unavailable.")
        value = dict(row)
        value["evaluation_evidence_state"] = EvaluationEvidenceState.UNAVAILABLE
        return TenantAIExecutionPolicy.model_validate(value)
    @staticmethod
    def _worker_actor(row: Any) -> str:
        return f"admin-worker:{row['command_id']}"
    def _write_resource(
        self, connection: Any, row: Any, request: GovernedCommandObservation, spec: Any
    ) -> None:
        if (
            spec.resource_strategy == AdminCommandResourceStrategy.AI_POLICY
            and spec.result_resource_type is None
        ):
            return
        snapshot = request.result_snapshot or {}
        digest = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
        resource_type, resource_id = spec.output_resource(
            target_type=row["target_type"],
            target_id=row["target_id"],
            command_id=str(row["command_id"]),
        )
        envelope = self.codec.encrypt_json(
            snapshot, tenant_id=row["tenant_id"], resource_type="admin-control-resource",
            resource_id=f'{resource_type}:{resource_id}', field="snapshot",
        )
        previous = connection.execute(
            """SELECT * FROM ai_admin_control_resources
                WHERE tenant_id = %s AND resource_type = %s AND resource_id = %s
                FOR UPDATE""",
            (row["tenant_id"], resource_type, resource_id),
        ).fetchone()
        if previous is not None:
            connection.execute(
                """INSERT INTO ai_admin_control_resource_versions (
                       tenant_id, resource_type, resource_id, resource_version,
                       snapshot_hash, snapshot_envelope, source_command_id, execution_state)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'SUCCEEDED')
                   ON CONFLICT DO NOTHING""",
                (
                    previous["tenant_id"], previous["resource_type"], previous["resource_id"],
                    previous["resource_version"], previous["snapshot_hash"],
                    previous["snapshot_envelope"], previous["updated_by_command_id"],
                ),
            )
        if snapshot.get("tombstoned") is True:
            connection.execute(
                """INSERT INTO ai_admin_control_resource_versions (
                       tenant_id, resource_type, resource_id, resource_version,
                       snapshot_hash, snapshot_envelope, source_command_id, execution_state)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (
                    row["tenant_id"], resource_type, resource_id, request.result_version,
                    digest, envelope, row["command_id"], request.state.value,
                ),
            )
            connection.execute(
                """DELETE FROM ai_admin_control_resources
                    WHERE tenant_id = %s AND resource_type = %s AND resource_id = %s""",
                (row["tenant_id"], resource_type, resource_id),
            )
            return
        connection.execute(
            """INSERT INTO ai_admin_control_resources (
                   tenant_id, resource_type, resource_id, resource_version,
                   snapshot_hash, snapshot_envelope, updated_by_command_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, resource_type, resource_id) DO UPDATE SET
                   resource_version = EXCLUDED.resource_version,
                   snapshot_hash = EXCLUDED.snapshot_hash,
                   snapshot_envelope = EXCLUDED.snapshot_envelope,
                   updated_by_command_id = EXCLUDED.updated_by_command_id,
                   updated_at = CURRENT_TIMESTAMP""",
            (row["tenant_id"], resource_type, resource_id,
             request.result_version, digest, envelope, row["command_id"]),
        )
        connection.execute(
            """INSERT INTO ai_admin_control_resource_versions (
                   tenant_id, resource_type, resource_id, resource_version,
                   snapshot_hash, snapshot_envelope, source_command_id, execution_state)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (
                row["tenant_id"], resource_type, resource_id, request.result_version,
                digest, envelope, row["command_id"], request.state.value,
            ),
        )
    def _encrypt(self, tenant_id: int, resource_id: UUID, field: str,
                 payload: dict[str, object]) -> str:
        return self.codec.encrypt_json(
            payload, tenant_id=tenant_id, resource_type="admin-control-command",
            resource_id=str(resource_id), field=field,
        )
@lru_cache(maxsize=1)
def get_admin_control_plane_worker_store() -> AdminControlPlaneWorkerStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise AdminControlPlaneUnavailable("Control-plane worker storage is unavailable.")
    return AdminControlPlaneWorkerStore(database_url)
