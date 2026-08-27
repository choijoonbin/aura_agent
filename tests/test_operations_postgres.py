from __future__ import annotations

import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent.operations_contracts import BootstrapRetentionPolicyRequest
from dwp_agent.operations_store import (
    PostgresOperationsStore,
    RetentionPolicyConflict,
    RetentionPolicyNotConfigured,
)
from dwp_agent.run_store import _apply_migrations


@pytest.mark.integration
def test_retention_reads_are_pure_and_bootstrap_is_audited_and_idempotent() -> None:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test")):
        pytest.fail("Operations integration tests require a dedicated test database.")
    _apply_migrations(database_url)
    tenant_id = str(800_000_000 + uuid4().int % 100_000_000)
    store = PostgresOperationsStore(database_url)

    with pytest.raises(RetentionPolicyNotConfigured):
        store.retention_policy(tenant_id=tenant_id)
    with pytest.raises(RetentionPolicyNotConfigured):
        store.overview(tenant_id=tenant_id)
    with connect(database_url) as connection:
        count = connection.execute(
            """SELECT COUNT(*) FROM ai_conversation_retention_policies
                WHERE tenant_id = %s""",
            (int(tenant_id),),
        ).fetchone()[0]
    assert count == 0

    idempotency_key = uuid4()
    request = BootstrapRetentionPolicyRequest(
        idempotency_key=idempotency_key,
        expected_existing_count=0,
        retention_days=365,
        change_reason="Initialize the tenant records retention policy.",
    )
    created = store.bootstrap_retention_policy(
        tenant_id=tenant_id,
        actor_user_id="records-admin",
        correlation_id="retention-bootstrap",
        request=request,
    )
    replayed = store.bootstrap_retention_policy(
        tenant_id=tenant_id,
        actor_user_id="records-admin",
        correlation_id="retention-bootstrap-retry",
        request=request,
    )
    with pytest.raises(RetentionPolicyConflict, match="idempotency key"):
        store.bootstrap_retention_policy(
            tenant_id=tenant_id,
            actor_user_id="records-admin",
            correlation_id="retention-bootstrap-conflict",
            request=request.model_copy(update={"retention_days": 730}),
        )

    assert created.retention_days == 365
    assert replayed == created
    with connect(database_url) as connection:
        row = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM ai_conversation_retention_policies
                     WHERE tenant_id = %s),
                   (SELECT COUNT(*) FROM ai_governance_events
                     WHERE tenant_id = %s AND category = 'RETENTION'
                       AND event_type = 'retention-policy.bootstrapped')""",
            (int(tenant_id), int(tenant_id)),
        ).fetchone()
    assert row == (1, 1)
