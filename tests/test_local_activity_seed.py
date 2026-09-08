from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest
from psycopg import connect

from dwp_agent.local_activity_seed import (
    LocalActivitySeedConfigurationError,
    TENANT_ID,
    USER_ID,
    _fixtures,
    _id,
    _validate_existing_seed_targets,
    seed_local_activity,
)
from dwp_agent.run_store import _apply_migrations


def test_local_activity_seed_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DWP_AGENT_LOCAL_ACTIVITY_SEED_ENABLED", raising=False)
    assert seed_local_activity() is None


def test_local_activity_seed_is_fixed_to_verified_account_and_local_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (TENANT_ID, USER_ID) == (1, "900018")
    monkeypatch.setenv("DWP_AGENT_LOCAL_ACTIVITY_SEED_ENABLED", "true")
    monkeypatch.setenv("DWP_ENVIRONMENT", "production")
    with pytest.raises(LocalActivitySeedConfigurationError, match="only"):
        seed_local_activity()


def test_local_activity_seed_rejects_identity_or_provenance_collisions() -> None:
    fixtures = _fixtures()
    fixture = fixtures[0]
    request_id = f"local-activity-{fixture['key']}"
    connection = _ExistingRowsConnection([
        (_id(str(fixture["key"])), TENANT_ID, USER_ID, request_id, "LIVE")
    ])

    with pytest.raises(LocalActivitySeedConfigurationError, match="collides"):
        _validate_existing_seed_targets(connection, fixtures, require_all=False)


class _ExistingRowsConnection:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def execute(self, _query: str, _parameters: tuple):
        return self

    def fetchall(self) -> list[tuple]:
        return self.rows


@pytest.mark.integration
def test_local_activity_seed_is_idempotent_and_never_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Local activity seed tests require a dedicated test database.")
    _apply_migrations(database_url)
    monkeypatch.setenv("DWP_AGENT_LOCAL_ACTIVITY_SEED_ENABLED", "true")
    monkeypatch.setenv("DWP_ENVIRONMENT", "local")
    monkeypatch.setenv("DWP_AGENT_DATABASE_URL", database_url)
    try:
        first = seed_local_activity()
        second = seed_local_activity()
        assert first == second
        with connect(database_url) as connection:
            run_row = connection.execute(
                """SELECT COUNT(*), COUNT(*) FILTER (WHERE data_provenance = 'SAMPLE'),
                          COUNT(*) FILTER (WHERE audit_link_state = 'PENDING')
                     FROM ai_agent_runs
                    WHERE tenant_id = 1 AND user_id = '900018'
                      AND request_id LIKE 'local-activity-%'"""
            ).fetchone()
            stage_count = connection.execute(
                """SELECT COUNT(*) FROM ai_agent_run_stages stage
                     JOIN ai_agent_runs run ON run.run_id = stage.run_id
                    WHERE run.tenant_id = 1 AND run.user_id = '900018'
                      AND run.request_id LIKE 'local-activity-%'"""
            ).fetchone()[0]
        assert run_row == (4, 4, 4)
        assert stage_count >= 16
    finally:
        with connect(database_url) as connection:
            connection.execute(
                """DELETE FROM ai_agent_runs
                    WHERE tenant_id = 1 AND user_id = '900018'
                      AND request_id LIKE 'local-activity-%'
                      AND data_provenance = 'SAMPLE'"""
            )


@pytest.mark.integration
def test_local_activity_seed_rejects_cross_tenant_run_id_collision_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Local activity seed tests require a dedicated test database.")
    _apply_migrations(database_url)
    collision_id = _id(str(_fixtures()[0]["key"]))
    foreign_tenant = 799_000_000
    monkeypatch.setenv("DWP_AGENT_LOCAL_ACTIVITY_SEED_ENABLED", "true")
    monkeypatch.setenv("DWP_ENVIRONMENT", "local")
    monkeypatch.setenv("DWP_AGENT_DATABASE_URL", database_url)

    try:
        with connect(database_url) as connection:
            connection.execute(
                """INSERT INTO ai_agent_runs (
                       run_id, tenant_id, user_id, request_id, query_hash, agent_key,
                       agent_revision, run_state, answer_state, risk_tier,
                       policy_outcome, status_code, locale, correlation_id,
                       completed_at, data_provenance)
                   VALUES (%s, %s, 'foreign-owner', 'foreign-request', %s,
                           'DWP_ASSISTANT', 1, 'COMPLETED', 'ABSTAINED', 'L1',
                           'ALLOW', 'NO_EVIDENCE', 'ko-KR', 'foreign-correlation',
                           CURRENT_TIMESTAMP, 'LIVE')""",
                (collision_id, foreign_tenant, "f" * 64),
            )

        with pytest.raises(LocalActivitySeedConfigurationError, match="collides"):
            seed_local_activity()

        with connect(database_url) as connection:
            target_count = connection.execute(
                """SELECT COUNT(*) FROM ai_agent_runs
                    WHERE tenant_id = %s AND user_id = %s
                      AND request_id LIKE 'local-activity-%%'""",
                (TENANT_ID, USER_ID),
            ).fetchone()[0]
            collision = connection.execute(
                """SELECT tenant_id, user_id, request_id, data_provenance
                     FROM ai_agent_runs WHERE run_id = %s""",
                (collision_id,),
            ).fetchone()
            telemetry_count = connection.execute(
                """SELECT COUNT(*) FROM ai_agent_run_stages WHERE run_id = %s""",
                (collision_id,),
            ).fetchone()[0]
        assert target_count == 0
        assert collision == (
            foreign_tenant,
            "foreign-owner",
            "foreign-request",
            "LIVE",
        )
        assert telemetry_count == 0
    finally:
        with connect(database_url) as connection:
            connection.execute(
                "DELETE FROM ai_agent_runs WHERE run_id = %s", (collision_id,)
            )
