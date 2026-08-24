from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from dwp_agent.operational_gate_catalog import OPERATIONAL_GATE_CATALOG
from dwp_agent.operational_gate_contracts import (
    ConfigureOperationalGateRequest,
    GateCategory,
    GateEvidenceType,
    GateStatus,
    OperationalGateKey,
    OperationalGateOption,
    OperationalGateSummary,
)
from dwp_agent.operational_gate_store import PostgresOperationalGateStore
from dwp_agent.operational_gate_store_errors import OperationalGateSeparationOfDutyViolation
from dwp_agent.run_store import _migration_sort_key


def test_operational_gate_catalog_is_complete_and_actionable() -> None:
    keys = [definition.gate_key for definition in OPERATIONAL_GATE_CATALOG]

    assert len(keys) == len(OperationalGateKey) == 13
    assert len(set(keys)) == len(keys)
    for definition in OPERATIONAL_GATE_CATALOG:
        assert definition.recommended_option in definition.options
        assert definition.required_evidence_types
        assert all(isinstance(item, GateEvidenceType) for item in definition.required_evidence_types)


def test_operational_gate_contract_rejects_embedded_secret_material() -> None:
    with pytest.raises(ValidationError, match="references, not secret material"):
        ConfigureOperationalGateRequest(
            selected_option="MANAGED_IDENTITY",
            owner_user_id="cloud-security",
            configuration_ref="api_key=do-not-store-this",
            expected_version=1,
            change_reason="Configure the production model identity.",
        )


@pytest.mark.parametrize("actor", ["owner", "configurator", "validator"])
def test_gate_approval_requires_an_independent_actor(actor: str) -> None:
    gate = _gate()

    with pytest.raises(OperationalGateSeparationOfDutyViolation):
        PostgresOperationalGateStore._require_independent_approver(gate, actor)

    PostgresOperationalGateStore._require_independent_approver(gate, "independent-auditor")


def test_expired_approval_is_resolved_without_mutating_audit_history() -> None:
    expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert (
        PostgresOperationalGateStore._effective_status(GateStatus.APPROVED, expired_at)
        == GateStatus.EXPIRED
    )
    assert (
        PostgresOperationalGateStore._effective_status(GateStatus.BLOCKED, expired_at)
        == GateStatus.BLOCKED
    )


def test_agent_migrations_sort_by_numeric_version() -> None:
    paths = [
        Path("V10__scope_evidence.sql"),
        Path("V2__create_conversations.sql"),
        Path("V9__create_gates.sql"),
    ]

    assert [path.name for path in sorted(paths, key=_migration_sort_key)] == [
        "V2__create_conversations.sql",
        "V9__create_gates.sql",
        "V10__scope_evidence.sql",
    ]


def _gate() -> OperationalGateSummary:
    now = datetime.now(timezone.utc)
    return OperationalGateSummary(
        gate_key=OperationalGateKey.MODEL_CREDENTIALS,
        category=GateCategory.AI_RUNTIME,
        external_owner="CLOUD_SECURITY",
        delivery_critical=True,
        selected_option="MANAGED_IDENTITY",
        options=[OperationalGateOption(code="MANAGED_IDENTITY", recommended=True)],
        required_evidence_types=[GateEvidenceType.SECURITY_REVIEW],
        status=GateStatus.READY_FOR_APPROVAL,
        owner_user_id="owner",
        last_configured_by="configurator",
        last_validated_by="validator",
        evidence_count=1,
        configuration_revision=1,
        policy_version=4,
        updated_at=now,
    )
