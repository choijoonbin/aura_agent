import os
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent.delivery_gate import (
    DeliveryCapability,
    OperationalDeliveryConfigurationError,
    OperationalDeliveryNotReady,
    gate_environment,
    require_delivery_capability,
    validate_delivery_gate_runtime,
)
from dwp_agent.operational_gate_catalog import OPERATIONAL_GATE_CATALOG
from dwp_agent.operational_gate_contracts import (
    GateEnvironment,
    GateStatus,
    OperationalGateKey,
)
from dwp_agent.operational_gate_store import PostgresOperationalGateStore
from dwp_agent.operational_gate_policy import option_allowed_for_environment
from dwp_agent.operational_gate_store_errors import OperationalGateInvalidTransition
from dwp_agent.operational_gate_contracts import ConfigureOperationalGateRequest
from dwp_agent.run_store import _apply_migrations


class FakeGateStore:
    def __init__(
        self,
        statuses: dict[OperationalGateKey, GateStatus] | None = None,
        *,
        schema_ready: bool = True,
    ) -> None:
        self.statuses = statuses or {}
        self.ready = schema_ready
        self.runtime_calls = 0

    def schema_ready(self) -> bool:
        return self.ready

    def runtime_gate_states(self, *, gate_keys, **_kwargs):
        self.runtime_calls += 1
        return {
            gate_key: status
            for gate_key, status in self.statuses.items()
            if gate_key in gate_keys
        }


def approved_statuses() -> dict[OperationalGateKey, GateStatus]:
    return {
        definition.gate_key: GateStatus.APPROVED
        for definition in OPERATIONAL_GATE_CATALOG
        if definition.delivery_critical
    }


def test_local_environment_bypasses_customer_delivery_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_ENVIRONMENT", "local")
    store = FakeGateStore()

    require_delivery_capability(
        tenant_id="1",
        capability=DeliveryCapability.ACTION,
        store=store,
    )

    assert gate_environment() is None
    assert store.runtime_calls == 0


def test_ask_excludes_action_approval_but_action_requires_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_ENVIRONMENT", "production")
    statuses = approved_statuses()
    statuses[OperationalGateKey.ACTION_APPROVAL] = GateStatus.BLOCKED
    store = FakeGateStore(statuses)

    require_delivery_capability(
        tenant_id="1",
        capability=DeliveryCapability.ASK,
        store=store,
    )
    with pytest.raises(OperationalDeliveryNotReady) as captured:
        require_delivery_capability(
            tenant_id="1",
            capability=DeliveryCapability.ACTION,
            store=store,
        )

    assert captured.value.blocking_gates == (OperationalGateKey.ACTION_APPROVAL,)
    assert captured.value.environment == GateEnvironment.PRODUCTION


def test_missing_or_expired_gate_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_ENVIRONMENT", "staging")
    statuses = approved_statuses()
    statuses.pop(OperationalGateKey.SOURCE_ACL)
    statuses[OperationalGateKey.TENANT_KMS] = GateStatus.EXPIRED

    with pytest.raises(OperationalDeliveryNotReady) as captured:
        require_delivery_capability(
            tenant_id="1",
            capability=DeliveryCapability.ASK,
            store=FakeGateStore(statuses),
        )

    assert captured.value.environment == GateEnvironment.STAGING
    assert captured.value.blocking_gates == (
        OperationalGateKey.SOURCE_ACL,
        OperationalGateKey.TENANT_KMS,
    )


def test_shared_runtime_requires_the_operational_gate_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_ENVIRONMENT", "development")

    with pytest.raises(OperationalDeliveryConfigurationError, match="schema"):
        validate_delivery_gate_runtime(FakeGateStore(schema_ready=False))


def test_unknown_environment_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_ENVIRONMENT", "customer-live")

    with pytest.raises(OperationalDeliveryConfigurationError, match="DWP_ENVIRONMENT"):
        gate_environment()


@pytest.mark.parametrize(
    ("gate_key", "environment", "selected_option"),
    [
        (OperationalGateKey.ACTION_APPROVAL, GateEnvironment.DEVELOPMENT, "BLOCKED"),
        (OperationalGateKey.TENANT_KMS, GateEnvironment.DEVELOPMENT, "LOCAL_DEVELOPMENT_KEY"),
        (
            OperationalGateKey.NETWORK_ISOLATION,
            GateEnvironment.STAGING,
            "DEVELOPMENT_PUBLIC_ONLY",
        ),
        (
            OperationalGateKey.EVALUATION_DATASET,
            GateEnvironment.PRODUCTION,
            "SYNTHETIC_DEVELOPMENT_ONLY",
        ),
        (
            OperationalGateKey.RETENTION_LEGAL_HOLD,
            GateEnvironment.STAGING,
            "DWP_DEVELOPMENT_DEFAULT",
        ),
    ],
)
def test_environment_limited_gate_options_fail_closed(
    gate_key: OperationalGateKey,
    environment: GateEnvironment,
    selected_option: str,
) -> None:
    assert not option_allowed_for_environment(gate_key, environment, selected_option)
    store = PostgresOperationalGateStore("postgresql://unused")
    with pytest.raises(OperationalGateInvalidTransition, match="cannot authorize"):
        store.configure(
            tenant_id="1",
            actor_user_id="manager",
            correlation_id="gate-option-test",
            environment=environment,
            gate_key=gate_key,
            request=ConfigureOperationalGateRequest(
                selected_option=selected_option,
                owner_user_id="owner",
                expected_version=1,
                change_reason="Reject a non-deliverable environment option.",
            ),
        )


def test_development_only_option_is_valid_only_for_development() -> None:
    assert option_allowed_for_environment(
        OperationalGateKey.NETWORK_ISOLATION,
        GateEnvironment.DEVELOPMENT,
        "DEVELOPMENT_PUBLIC_ONLY",
    )


@pytest.mark.integration
def test_runtime_gate_read_does_not_create_tenant_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test")):
        pytest.fail("Delivery gate integration tests require a dedicated test database.")
    _apply_migrations(database_url)
    tenant_id = str(800_000_000 + uuid4().int % 100_000_000)
    monkeypatch.setenv("DWP_ENVIRONMENT", "production")

    with pytest.raises(OperationalDeliveryNotReady):
        require_delivery_capability(
            tenant_id=tenant_id,
            capability=DeliveryCapability.ASK,
            store=PostgresOperationalGateStore(database_url),
        )

    with connect(database_url) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM ai_operational_gates WHERE tenant_id = %s",
            (int(tenant_id),),
        ).fetchone()[0]
    assert count == 0


@pytest.mark.integration
def test_runtime_gate_projects_approved_but_non_deliverable_option_as_blocked() -> None:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test")):
        pytest.fail("Delivery gate integration tests require a dedicated test database.")
    _apply_migrations(database_url)
    tenant_id = 800_000_000 + uuid4().int % 100_000_000
    with connect(database_url) as connection:
        connection.execute(
            """INSERT INTO ai_operational_gates (
                   tenant_id, environment, gate_key, status, selected_option,
                   approved_by, effective_at, expires_at, updated_by)
               VALUES (%s, 'PRODUCTION', 'ACTION_APPROVAL', 'APPROVED', 'BLOCKED',
                       'approver', CURRENT_TIMESTAMP,
                       CURRENT_TIMESTAMP + INTERVAL '1 day', 'approver')""",
            (tenant_id,),
        )

    states = PostgresOperationalGateStore(database_url).runtime_gate_states(
        tenant_id=str(tenant_id),
        environment=GateEnvironment.PRODUCTION,
        gate_keys=frozenset({OperationalGateKey.ACTION_APPROVAL}),
    )

    assert states == {OperationalGateKey.ACTION_APPROVAL: GateStatus.BLOCKED}
