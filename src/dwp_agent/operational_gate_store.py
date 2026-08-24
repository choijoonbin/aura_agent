from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from psycopg import connect

from .governance_store import GovernanceStoreUnavailable
from .operational_gate_catalog import operational_gate_definition
from .operational_gate_contracts import (
    ConfigureOperationalGateRequest,
    CreateOperationalGateEvidenceRequest,
    DecideOperationalGateRequest,
    GateDecision,
    GateEnvironment,
    GateStatus,
    GateValidationOutcome,
    OperationalGateDetail,
    OperationalGateKey,
    OperationalGatePortfolio,
    OperationalGateSummary,
    ValidateOperationalGateRequest,
)
from .operational_gate_repository import OperationalGateRepositoryMixin
from .operational_gate_policy import approval_eligibility, require_independent_approver
from .operational_gate_store_errors import (
    OperationalGateConflict,
    OperationalGateInvalidTransition,
    OperationalGateMissingEvidence,
)


class PostgresOperationalGateStore(OperationalGateRepositoryMixin):
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def portfolio(
        self, *, tenant_id: str, actor_user_id: str, environment: GateEnvironment
    ) -> OperationalGatePortfolio:
        tenant = int(tenant_id)
        with connect(self.database_url) as connection:
            self._ensure_gates(connection, tenant, actor_user_id, environment)
            rows = connection.execute(
                self._gate_select() + " ORDER BY gate_key",
                (tenant, environment.value),
            ).fetchall()
        gates = [self._gate(row) for row in rows]
        approved = sum(gate.status == GateStatus.APPROVED for gate in gates)
        required = sum(gate.delivery_critical for gate in gates)
        approved_required = sum(
            gate.delivery_critical and gate.status == GateStatus.APPROVED for gate in gates
        )
        return OperationalGatePortfolio(
            environment=environment,
            total_count=len(gates),
            required_count=required,
            approved_count=approved,
            ready_for_approval_count=sum(
                gate.status == GateStatus.READY_FOR_APPROVAL for gate in gates
            ),
            blocked_count=sum(gate.status == GateStatus.BLOCKED for gate in gates),
            expired_count=sum(gate.status == GateStatus.EXPIRED for gate in gates),
            completion_percent=round((approved_required / required) * 100) if required else 100,
            delivery_ready=required > 0 and approved_required == required,
            gates=gates,
        )

    def detail(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        environment: GateEnvironment,
        gate_key: OperationalGateKey,
    ) -> OperationalGateDetail:
        tenant = int(tenant_id)
        with connect(self.database_url) as connection:
            self._ensure_gates(connection, tenant, actor_user_id, environment)
            row = self._locked_gate(
                connection, tenant, environment, gate_key, lock=False
            )
            evidence_rows = connection.execute(
                """SELECT evidence_id, evidence_type, title, reference, checksum_sha256,
                          notes, created_by, created_at
                     FROM ai_operational_gate_evidence
                    WHERE tenant_id = %s AND environment = %s AND gate_key = %s
                      AND configuration_revision = %s
                    ORDER BY created_at DESC, evidence_id DESC""",
                (tenant, environment.value, gate_key.value, row[15]),
            ).fetchall()
            event_rows = connection.execute(
                """SELECT event_id, event_type, actor_user_id, correlation_id,
                          change_reason, previous_value, current_value, created_at
                     FROM ai_governance_events
                    WHERE tenant_id = %s AND category = 'GATE'
                      AND target_type = 'OPERATIONAL_GATE' AND target_key = %s
                    ORDER BY created_at DESC, event_id DESC
                    LIMIT 100""",
                (tenant, f"{environment.value}:{gate_key.value}"),
            ).fetchall()
        gate = self._gate(row)
        present_types = {item[1] for item in evidence_rows}
        return OperationalGateDetail(
            gate=gate,
            evidence=[self._evidence(item) for item in evidence_rows],
            missing_evidence_types=[
                item for item in gate.required_evidence_types if item.value not in present_types
            ],
            approval_eligibility=approval_eligibility(gate, actor_user_id),
            events=[self._audit_event(item) for item in event_rows],
        )

    def configure(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        environment: GateEnvironment,
        gate_key: OperationalGateKey,
        request: ConfigureOperationalGateRequest,
    ) -> OperationalGateDetail:
        tenant = int(tenant_id)
        definition = operational_gate_definition(gate_key)
        if request.selected_option not in definition.options:
            raise OperationalGateInvalidTransition(
                f"{request.selected_option} is not valid for {gate_key.value}."
            )
        with connect(self.database_url) as connection:
            self._ensure_gates(connection, tenant, actor_user_id, environment)
            current_row = self._locked_gate(connection, tenant, environment, gate_key)
            self._require_version(current_row[10], request.expected_version)
            result = connection.execute(
                """UPDATE ai_operational_gates
                      SET status = 'CONFIGURING', selected_option = %s,
                          owner_user_id = %s, configuration_ref = %s, notes = %s,
                          validation_summary = NULL, last_configured_by = %s,
                          last_validated_by = NULL, approved_by = NULL,
                          effective_at = NULL, expires_at = NULL,
                          configuration_revision = configuration_revision + 1,
                          policy_version = policy_version + 1,
                          updated_by = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND environment = %s AND gate_key = %s
                      AND policy_version = %s""",
                (
                    request.selected_option,
                    request.owner_user_id,
                    request.configuration_ref,
                    request.notes,
                    actor_user_id,
                    actor_user_id,
                    tenant,
                    environment.value,
                    gate_key.value,
                    request.expected_version,
                ),
            )
            if result.rowcount != 1:
                raise OperationalGateConflict("The operational gate changed. Reload and retry.")
            row = self._locked_gate(connection, tenant, environment, gate_key, lock=False)
            updated = self._gate(row)
            self._event(
                connection,
                tenant,
                "operational-gate.configured",
                gate_key,
                environment,
                actor_user_id,
                correlation_id,
                request.change_reason,
                self._gate(current_row).model_dump(mode="json"),
                updated.model_dump(mode="json"),
            )
        return self.detail(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            environment=environment,
            gate_key=gate_key,
        )

    def add_evidence(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        environment: GateEnvironment,
        gate_key: OperationalGateKey,
        request: CreateOperationalGateEvidenceRequest,
    ) -> OperationalGateDetail:
        tenant = int(tenant_id)
        with connect(self.database_url) as connection:
            self._ensure_gates(connection, tenant, actor_user_id, environment)
            current_row = self._locked_gate(connection, tenant, environment, gate_key)
            self._require_version(current_row[10], request.expected_version)
            current = self._gate(current_row)
            if not current.selected_option or not current.owner_user_id:
                raise OperationalGateInvalidTransition(
                    "Configure the operational gate before adding evidence."
                )
            effective_status = self._effective_status(
                GateStatus(current_row[1]), current_row[12]
            )
            if effective_status == GateStatus.EXPIRED:
                raise OperationalGateInvalidTransition(
                    "Reconfigure an expired operational gate before adding new evidence."
                )
            if effective_status == GateStatus.APPROVED:
                raise OperationalGateInvalidTransition(
                    "Reconfigure an approved operational gate before changing its evidence basis."
                )
            evidence_id = uuid4()
            connection.execute(
                """INSERT INTO ai_operational_gate_evidence (
                       evidence_id, tenant_id, environment, gate_key, evidence_type,
                       title, reference, checksum_sha256, notes, created_by,
                       configuration_revision)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    evidence_id,
                    tenant,
                    environment.value,
                    gate_key.value,
                    request.evidence_type.value,
                    request.title,
                    request.reference,
                    request.checksum_sha256,
                    request.notes,
                    actor_user_id,
                    current_row[15],
                ),
            )
            result = connection.execute(
                """UPDATE ai_operational_gates
                      SET status = 'CONFIGURING', validation_summary = NULL,
                          last_validated_by = NULL, approved_by = NULL,
                          effective_at = NULL, expires_at = NULL,
                          policy_version = policy_version + 1,
                          updated_by = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND environment = %s AND gate_key = %s
                      AND policy_version = %s""",
                (
                    actor_user_id,
                    tenant,
                    environment.value,
                    gate_key.value,
                    request.expected_version,
                ),
            )
            if result.rowcount != 1:
                raise OperationalGateConflict("The operational gate changed. Reload and retry.")
            updated_row = self._locked_gate(
                connection, tenant, environment, gate_key, lock=False
            )
            updated = self._gate(updated_row).model_dump(mode="json")
            updated["evidence"] = {
                "evidenceId": str(evidence_id),
                "evidenceType": request.evidence_type.value,
                "reference": request.reference,
            }
            self._event(
                connection,
                tenant,
                "operational-gate.evidence-added",
                gate_key,
                environment,
                actor_user_id,
                correlation_id,
                request.change_reason,
                current.model_dump(mode="json"),
                updated,
            )
        return self.detail(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            environment=environment,
            gate_key=gate_key,
        )

    def validate(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        environment: GateEnvironment,
        gate_key: OperationalGateKey,
        request: ValidateOperationalGateRequest,
    ) -> OperationalGateDetail:
        tenant = int(tenant_id)
        definition = operational_gate_definition(gate_key)
        with connect(self.database_url) as connection:
            self._ensure_gates(connection, tenant, actor_user_id, environment)
            current_row = self._locked_gate(connection, tenant, environment, gate_key)
            self._require_version(current_row[10], request.expected_version)
            current = self._gate(current_row)
            if current.status not in {
                GateStatus.CONFIGURING,
                GateStatus.BLOCKED,
            }:
                raise OperationalGateInvalidTransition(
                    f"{current.status.value} gates cannot be validated."
                )
            if not current.selected_option or not current.owner_user_id:
                raise OperationalGateInvalidTransition(
                    "Configure the operational gate before validation."
                )
            if request.outcome == GateValidationOutcome.PASS:
                present = {
                    item[0]
                    for item in connection.execute(
                        """SELECT DISTINCT evidence_type
                             FROM ai_operational_gate_evidence
                            WHERE tenant_id = %s AND environment = %s AND gate_key = %s
                              AND configuration_revision = %s""",
                        (tenant, environment.value, gate_key.value, current_row[15]),
                    ).fetchall()
                }
                missing = [
                    evidence.value
                    for evidence in definition.required_evidence_types
                    if evidence.value not in present
                ]
                if missing:
                    raise OperationalGateMissingEvidence(missing)
                next_status = GateStatus.READY_FOR_APPROVAL
            else:
                next_status = GateStatus.BLOCKED
            result = connection.execute(
                """UPDATE ai_operational_gates
                      SET status = %s, validation_summary = %s,
                          last_validated_by = %s, approved_by = NULL,
                          effective_at = NULL, expires_at = NULL,
                          policy_version = policy_version + 1,
                          updated_by = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND environment = %s AND gate_key = %s
                      AND policy_version = %s""",
                (
                    next_status.value,
                    request.validation_summary,
                    actor_user_id,
                    actor_user_id,
                    tenant,
                    environment.value,
                    gate_key.value,
                    request.expected_version,
                ),
            )
            if result.rowcount != 1:
                raise OperationalGateConflict("The operational gate changed. Reload and retry.")
            row = self._locked_gate(connection, tenant, environment, gate_key, lock=False)
            self._event(
                connection,
                tenant,
                "operational-gate.validated",
                gate_key,
                environment,
                actor_user_id,
                correlation_id,
                request.change_reason,
                current.model_dump(mode="json"),
                self._gate(row).model_dump(mode="json"),
            )
        return self.detail(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            environment=environment,
            gate_key=gate_key,
        )

    def decide(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        environment: GateEnvironment,
        gate_key: OperationalGateKey,
        request: DecideOperationalGateRequest,
    ) -> OperationalGateDetail:
        tenant = int(tenant_id)
        with connect(self.database_url) as connection:
            self._ensure_gates(connection, tenant, actor_user_id, environment)
            current_row = self._locked_gate(connection, tenant, environment, gate_key)
            self._require_version(current_row[10], request.expected_version)
            current = self._gate(current_row)
            if current.status != GateStatus.READY_FOR_APPROVAL:
                raise OperationalGateInvalidTransition(
                    "Only a validated gate can receive an approval decision."
                )
            self._require_independent_approver(current, actor_user_id)
            now = datetime.now(timezone.utc)
            approved = request.decision == GateDecision.APPROVE
            next_status = GateStatus.APPROVED if approved else GateStatus.BLOCKED
            result = connection.execute(
                """UPDATE ai_operational_gates
                      SET status = %s, approved_by = %s,
                          effective_at = %s, expires_at = %s,
                          policy_version = policy_version + 1,
                          updated_by = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND environment = %s AND gate_key = %s
                      AND policy_version = %s""",
                (
                    next_status.value,
                    actor_user_id if approved else None,
                    now if approved else None,
                    now + timedelta(days=request.valid_days) if approved else None,
                    actor_user_id,
                    tenant,
                    environment.value,
                    gate_key.value,
                    request.expected_version,
                ),
            )
            if result.rowcount != 1:
                raise OperationalGateConflict("The operational gate changed. Reload and retry.")
            row = self._locked_gate(connection, tenant, environment, gate_key, lock=False)
            self._event(
                connection,
                tenant,
                "operational-gate.approved" if approved else "operational-gate.rejected",
                gate_key,
                environment,
                actor_user_id,
                correlation_id,
                request.change_reason,
                current.model_dump(mode="json"),
                self._gate(row).model_dump(mode="json"),
            )
        return self.detail(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            environment=environment,
            gate_key=gate_key,
        )

    @staticmethod
    def _require_independent_approver(
        gate: OperationalGateSummary, actor_user_id: str
    ) -> None:
        require_independent_approver(gate, actor_user_id)

_STORE: PostgresOperationalGateStore | None = None
_STORE_LOCK = threading.Lock()


def get_operational_gate_store() -> PostgresOperationalGateStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            raise GovernanceStoreUnavailable(
                "DWAI-ON operational gates require the configured Agent database."
            )
        _STORE = PostgresOperationalGateStore(database_url)
        return _STORE


def reset_operational_gate_store_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None
