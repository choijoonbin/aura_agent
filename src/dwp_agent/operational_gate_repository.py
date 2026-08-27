from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from psycopg import connect

from .operational_gate_catalog import OPERATIONAL_GATE_CATALOG, operational_gate_definition
from .operational_gate_contracts import (
    BootstrapOperationalGatesRequest,
    GateEnvironment,
    GateStatus,
    OperationalGateAuditEvent,
    OperationalGateEvidence,
    OperationalGateKey,
    OperationalGateOption,
    OperationalGatePortfolio,
    OperationalGateSummary,
)
from .operational_gate_store_errors import OperationalGateConflict


class OperationalGateRepositoryMixin:
    def _ensure_gates(
        self, connection, tenant: int, actor: str, environment: GateEnvironment
    ) -> int:
        created = 0
        for definition in OPERATIONAL_GATE_CATALOG:
            result = connection.execute(
                """INSERT INTO ai_operational_gates (
                       tenant_id, environment, gate_key, updated_by)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (tenant_id, environment, gate_key) DO NOTHING""",
                (tenant, environment.value, definition.gate_key.value, actor),
            )
            created += result.rowcount
        return created

    def bootstrap(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        environment: GateEnvironment,
        request: BootstrapOperationalGatesRequest,
    ) -> OperationalGatePortfolio:
        tenant = int(tenant_id)
        command_key = f"{environment.value}:BOOTSTRAP:{request.idempotency_key}"
        with connect(self.database_url) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"dwp-agent:gates:{tenant}:{environment.value}",),
            )
            replayed = connection.execute(
                """SELECT 1 FROM ai_governance_events
                    WHERE tenant_id = %s AND category = 'GATE'
                      AND event_type = 'operational-gates.bootstrapped'
                      AND target_type = 'OPERATIONAL_GATE_PORTFOLIO'
                      AND target_key = %s""",
                (tenant, command_key),
            ).fetchone()
            if replayed is None:
                existing_count = connection.execute(
                    """SELECT COUNT(*) FROM ai_operational_gates
                        WHERE tenant_id = %s AND environment = %s""",
                    (tenant, environment.value),
                ).fetchone()[0]
                if existing_count != request.expected_existing_count:
                    raise OperationalGateConflict(
                        "The operational gate portfolio changed. Reload and retry."
                    )
                created_count = self._ensure_gates(
                    connection, tenant, actor_user_id, environment
                )
                connection.execute(
                    """INSERT INTO ai_governance_events (
                           event_id, tenant_id, category, event_type, target_type,
                           target_key, actor_user_id, correlation_id, change_reason,
                           previous_value, current_value)
                       VALUES (%s, %s, 'GATE', 'operational-gates.bootstrapped',
                               'OPERATIONAL_GATE_PORTFOLIO', %s, %s, %s, %s,
                               %s::jsonb, %s::jsonb)""",
                    (
                        uuid4(),
                        tenant,
                        command_key,
                        actor_user_id,
                        correlation_id,
                        request.change_reason,
                        json.dumps({"existingCount": existing_count}),
                        json.dumps(
                            {
                                "createdCount": created_count,
                                "totalCount": existing_count + created_count,
                            }
                        ),
                    ),
                )
        return self.portfolio(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            environment=environment,
        )

    def _locked_gate(
        self,
        connection,
        tenant: int,
        environment: GateEnvironment,
        gate_key: OperationalGateKey,
        *,
        lock: bool = True,
    ):
        row = connection.execute(
            self._gate_select(" FOR UPDATE" if lock else "", include_gate_key=True),
            (tenant, environment.value, gate_key.value),
        ).fetchone()
        if row is None:
            raise KeyError(gate_key.value)
        return row

    @staticmethod
    def _gate_select(suffix: str = "", *, include_gate_key: bool = False) -> str:
        return (
            "SELECT gate.gate_key, gate.status, gate.selected_option, gate.owner_user_id, "
            "gate.configuration_ref, gate.notes, gate.validation_summary, "
            "gate.last_configured_by, gate.last_validated_by, gate.approved_by, "
            "gate.policy_version, gate.effective_at, gate.expires_at, gate.updated_at, "
            "(SELECT COUNT(*) FROM ai_operational_gate_evidence evidence "
            "WHERE evidence.tenant_id = gate.tenant_id "
            "AND evidence.environment = gate.environment "
            "AND evidence.gate_key = gate.gate_key "
            "AND evidence.configuration_revision = gate.configuration_revision) "
            "AS evidence_count, gate.configuration_revision "
            "FROM ai_operational_gates gate "
            "WHERE gate.tenant_id = %s AND gate.environment = %s"
            + (" AND gate.gate_key = %s" if include_gate_key else "")
            + suffix
        )

    @staticmethod
    def _gate(row) -> OperationalGateSummary:
        gate_key = OperationalGateKey(row[0])
        definition = operational_gate_definition(gate_key)
        return OperationalGateSummary(
            gate_key=gate_key,
            category=definition.category,
            external_owner=definition.external_owner,
            delivery_critical=definition.delivery_critical,
            selected_option=row[2],
            options=[
                OperationalGateOption(
                    code=option, recommended=option == definition.recommended_option
                )
                for option in definition.options
            ],
            required_evidence_types=list(definition.required_evidence_types),
            status=OperationalGateRepositoryMixin._effective_status(
                GateStatus(row[1]), row[12]
            ),
            owner_user_id=row[3],
            configuration_ref=row[4],
            notes=row[5],
            validation_summary=row[6],
            last_configured_by=row[7],
            last_validated_by=row[8],
            approved_by=row[9],
            evidence_count=row[14],
            configuration_revision=row[15],
            policy_version=row[10],
            effective_at=row[11],
            expires_at=row[12],
            updated_at=row[13],
        )

    @staticmethod
    def _effective_status(status: GateStatus, expires_at: datetime | None) -> GateStatus:
        if (
            status == GateStatus.APPROVED
            and expires_at is not None
            and expires_at <= datetime.now(timezone.utc)
        ):
            return GateStatus.EXPIRED
        return status

    @staticmethod
    def _evidence(row) -> OperationalGateEvidence:
        return OperationalGateEvidence(
            evidence_id=row[0],
            evidence_type=row[1],
            title=row[2],
            reference=row[3],
            checksum_sha256=row[4],
            notes=row[5],
            created_by=row[6],
            created_at=row[7],
        )

    @staticmethod
    def _audit_event(row) -> OperationalGateAuditEvent:
        return OperationalGateAuditEvent(
            event_id=row[0],
            event_type=row[1],
            actor_user_id=row[2],
            correlation_id=row[3],
            change_reason=row[4],
            previous_status=OperationalGateRepositoryMixin._snapshot_status(row[5]),
            current_status=OperationalGateRepositoryMixin._snapshot_status(row[6]),
            created_at=row[7],
        )

    @staticmethod
    def _snapshot_status(snapshot: dict | None) -> GateStatus | None:
        if not snapshot or not snapshot.get("status"):
            return None
        return GateStatus(snapshot["status"])

    @staticmethod
    def _require_version(actual: int, expected: int) -> None:
        if actual != expected:
            raise OperationalGateConflict("The operational gate changed. Reload and retry.")

    @staticmethod
    def _event(
        connection,
        tenant: int,
        event_type: str,
        gate_key: OperationalGateKey,
        environment: GateEnvironment,
        actor: str,
        correlation: str,
        reason: str,
        previous: dict | None,
        current: dict | None,
    ) -> None:
        connection.execute(
            """INSERT INTO ai_governance_events (
                   event_id, tenant_id, category, event_type, target_type, target_key,
                   actor_user_id, correlation_id, change_reason, previous_value, current_value)
               VALUES (%s, %s, 'GATE', %s, 'OPERATIONAL_GATE', %s, %s, %s, %s,
                       %s::jsonb, %s::jsonb)""",
            (
                uuid4(),
                tenant,
                event_type,
                f"{environment.value}:{gate_key.value}",
                actor,
                correlation,
                reason,
                json.dumps(previous) if previous else None,
                json.dumps(current) if current else None,
            ),
        )
