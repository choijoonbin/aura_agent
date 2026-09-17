from __future__ import annotations

import hashlib
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
from .admin_model_routing_contracts import LatestRoutingSimulation
from .admin_control_plane_errors import (
    AdminControlPlaneConflict,
    AdminControlPlaneNotFound,
    AdminControlPlaneUnavailable,
)
from .admin_control_plane_store import AdminControlPlaneStore
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
                receipt_envelope = None
                problem_envelope = None
                completed_at = None
                if request.state in {GovernedCommandState.SUCCEEDED, GovernedCommandState.ROLLED_BACK}:
                    if row["command_kind"] == "MODEL_ROUTE_SIMULATE":
                        LatestRoutingSimulation.model_validate(request.result_snapshot)
                    self._verify_target_version(connection, row, request)
                    self._write_resource(connection, row, request)
                    receipt = CommandReceipt(
                        receipt_id=uuid4(), audit_event_id=event_id,
                        completed_at=datetime.now(UTC), result_summary=request.result_summary or "",
                        domain_receipt_ref=request.domain_receipt_ref,
                        rollback_ref=request.domain_receipt_ref
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
    def _verify_target_version(connection: Any, row: Any, request: GovernedCommandObservation) -> None:
        if row["target_type"] in {"MODEL_ROUTING", "AI_POLICY", "SAFETY_POLICY"}:
            policy = connection.execute(
                "SELECT policy_version FROM ai_execution_policies WHERE tenant_id = %s FOR UPDATE",
                (row["tenant_id"],),
            ).fetchone()
            observed = int(policy["policy_version"]) if policy else 0
        else:
            resource = connection.execute(
                """SELECT resource_version FROM ai_admin_control_resources
                    WHERE tenant_id = %s AND resource_type = %s AND resource_id = %s FOR UPDATE""",
                (row["tenant_id"], row["target_type"], row["target_id"]),
            ).fetchone()
            observed = int(resource["resource_version"]) if resource else 0
        if observed != int(row["expected_target_version"]):
            raise AdminControlPlaneConflict("The authoritative target changed during execution.")
        if request.result_version is None or request.result_version < max(1, observed):
            raise AdminControlPlaneConflict("The worker result version is not monotonic.")

    def _write_resource(self, connection: Any, row: Any, request: GovernedCommandObservation) -> None:
        snapshot = request.result_snapshot or {}
        digest = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
        resource_type = (
            "MODEL_ROUTE_SIMULATION"
            if row["command_kind"] == "MODEL_ROUTE_SIMULATE"
            else row["target_type"]
        )
        envelope = self.codec.encrypt_json(
            snapshot, tenant_id=row["tenant_id"], resource_type="admin-control-resource",
            resource_id=f'{resource_type}:{row["target_id"]}', field="snapshot",
        )
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
            (row["tenant_id"], resource_type, row["target_id"],
             request.result_version, digest, envelope, row["command_id"]),
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
