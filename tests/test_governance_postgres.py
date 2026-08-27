from __future__ import annotations

import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent.contracts import CitationSourceType
from dwp_agent.governance_contracts import (
    ActionExecutionPolicy,
    BootstrapGovernancePoliciesRequest,
    ConnectionState,
    DataClassification,
    SourceAccessMode,
    UpdateActionPolicyRequest,
    UpdateDataSourcePolicyRequest,
)
from dwp_agent.governance_store import (
    GovernancePolicyNotInitialized,
    PostgresGovernanceStore,
)
from dwp_agent.run_store import _apply_migrations


@pytest.mark.integration
def test_governance_reads_do_not_enable_or_create_runtime_policies() -> None:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test")):
        pytest.fail("Governance integration tests require a dedicated test database.")

    _apply_migrations(database_url)
    store = PostgresGovernanceStore(database_url)
    tenant_id = str(700_000_000 + uuid4().int % 100_000_000)

    assert store.source_policies(tenant_id=tenant_id, actor_user_id="reader") == []
    assert store.action_policies(tenant_id=tenant_id, actor_user_id="reader") == []
    with pytest.raises(GovernancePolicyNotInitialized):
        store.safety_policy(tenant_id=tenant_id, actor_user_id="reader")
    with pytest.raises(GovernancePolicyNotInitialized):
        store.update_source_policy(
            tenant_id=tenant_id,
            actor_user_id="governance-admin",
            correlation_id="source-before-bootstrap",
            source_key=CitationSourceType.WORK_ITEM,
            request=UpdateDataSourcePolicyRequest(
                enabled=False,
                access_mode=SourceAccessMode.BLOCKED,
                classification=DataClassification.INTERNAL,
                expected_version=1,
                change_reason="Reject a source update before explicit initialization.",
            ),
        )
    with pytest.raises(GovernancePolicyNotInitialized):
        store.update_action_policy(
            tenant_id=tenant_id,
            actor_user_id="governance-admin",
            correlation_id="action-before-bootstrap",
            action_key="CALENDAR.EVENT.CREATE",
            request=UpdateActionPolicyRequest(
                enabled=False,
                confirmation_required=True,
                execution_policy=ActionExecutionPolicy.BLOCKED,
                expected_version=1,
                change_reason="Reject an action update before explicit initialization.",
            ),
        )

    source_request = BootstrapGovernancePoliciesRequest(
        idempotency_key=uuid4(),
        expected_existing_count=0,
        change_reason="Initialize blocked source policies for this tenant.",
    )
    sources = store.bootstrap_source_policies(
        tenant_id=tenant_id,
        actor_user_id="governance-admin",
        correlation_id="source-bootstrap",
        request=source_request,
    )
    replayed_sources = store.bootstrap_source_policies(
        tenant_id=tenant_id,
        actor_user_id="governance-admin",
        correlation_id="source-bootstrap-retry",
        request=source_request,
    )
    actions = store.bootstrap_action_policies(
        tenant_id=tenant_id,
        actor_user_id="governance-admin",
        correlation_id="action-bootstrap",
        request=BootstrapGovernancePoliciesRequest(
            idempotency_key=uuid4(),
            expected_existing_count=0,
            change_reason="Initialize blocked action policies for this tenant.",
        ),
    )
    safety = store.bootstrap_safety_policy(
        tenant_id=tenant_id,
        actor_user_id="governance-admin",
        correlation_id="safety-bootstrap",
        request=BootstrapGovernancePoliciesRequest(
            idempotency_key=uuid4(),
            expected_existing_count=0,
            change_reason="Initialize fail-closed safety controls for this tenant.",
        ),
    )

    assert sources
    assert replayed_sources == sources
    assert all(not policy.enabled for policy in sources)
    assert all(policy.access_mode == SourceAccessMode.BLOCKED for policy in sources)
    assert all(policy.connection_state == ConnectionState.BLOCKED for policy in sources)
    assert actions
    assert all(not policy.enabled for policy in actions)
    assert all(policy.confirmation_required for policy in actions)
    assert all(
        policy.execution_policy == ActionExecutionPolicy.BLOCKED for policy in actions
    )
    assert safety.public_web_enabled is False

    with connect(database_url) as connection:
        event_counts = dict(connection.execute(
            """SELECT category, COUNT(*) FROM ai_governance_events
                 WHERE tenant_id = %s AND event_type LIKE '%%.bootstrapped'
                 GROUP BY category""",
            (int(tenant_id),),
        ).fetchall())
    assert event_counts == {"SOURCE": 1, "ACTION": 1, "SAFETY": 1}
