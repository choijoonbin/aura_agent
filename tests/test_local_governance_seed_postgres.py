from __future__ import annotations

import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent.governance_catalog import SOURCE_DEFINITIONS
from dwp_agent.local_governance_seed import _seed_tenant
from dwp_agent.run_store import _apply_migrations
from dwp_agent.workplace_actions import all_workplace_actions


@pytest.mark.integration
def test_local_governance_seed_is_complete_audited_and_idempotent() -> None:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test")):
        pytest.fail("Local seed integration tests require a dedicated test database.")

    _apply_migrations(database_url)
    tenant_id = 700_000_000 + uuid4().int % 100_000_000

    first = _seed_tenant(database_url, tenant_id)
    replay = _seed_tenant(database_url, tenant_id)

    assert replay == first
    assert first.source_count == len(SOURCE_DEFINITIONS)
    assert first.action_count == len(all_workplace_actions())
    with connect(database_url) as connection:
        sources = connection.execute(
            """SELECT access_mode, enabled, connection_state
                 FROM ai_data_source_policies WHERE tenant_id = %s""",
            (tenant_id,),
        ).fetchall()
        actions = connection.execute(
            """SELECT enabled, confirmation_required, execution_policy
                 FROM ai_action_policies WHERE tenant_id = %s""",
            (tenant_id,),
        ).fetchall()
        safety = connection.execute(
            """SELECT public_web_enabled, require_citations
                 FROM ai_safety_policies WHERE tenant_id = %s""",
            (tenant_id,),
        ).fetchone()
        retention = connection.execute(
            """SELECT retention_days, legal_hold
                 FROM ai_conversation_retention_policies WHERE tenant_id = %s""",
            (tenant_id,),
        ).fetchone()
        events = connection.execute(
            """SELECT category, COUNT(*) FROM ai_governance_events
                 WHERE tenant_id = %s AND event_type = 'local-governance.seeded'
                 GROUP BY category""",
            (tenant_id,),
        ).fetchall()

    assert len(sources) == len(SOURCE_DEFINITIONS)
    assert all(row == ("SOURCE_PERMISSIONS", True, "CONNECTED") for row in sources)
    assert len(actions) == len(all_workplace_actions())
    assert all(row == (False, True, "BLOCKED") for row in actions)
    assert safety == (False, True)
    assert retention == (90, False)
    assert dict(events) == {
        "ACTION": 1,
        "RETENTION": 1,
        "SAFETY": 1,
        "SOURCE": 1,
    }
