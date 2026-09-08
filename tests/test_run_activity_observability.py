from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from psycopg import connect

from dwp_agent.activity_store import ActivityFilters, InMemoryAgentActivityStore
from dwp_agent.main import app
from dwp_agent.contracts import AskResponse
from dwp_agent.run_observability import (
    RunSourceHealthStatus,
    RunStageKey,
    SourceHealthObservation,
    audit_record_id,
)
from dwp_agent.run_store import (
    InMemoryRunStore,
    PostgresRunStore,
    RunStart,
    RunStoreUnavailable,
    _apply_migrations,
)
from dwp_agent.user_run_store import InMemoryUserRunStore
from dwp_agent.user_run_store import (
    _configure_read_snapshot,
    _postgres_observability,
)


def test_measured_run_contract_uses_fenced_stages_skips_and_real_source_timing() -> None:
    now = [datetime(2026, 9, 7, 1, 0, tzinfo=timezone.utc)]
    store = InMemoryRunStore(clock=lambda: now[0])
    start = _start(audit_id=str(uuid4()))
    lease = store.begin(start)
    assert lease is not None

    now[0] += timedelta(milliseconds=200)
    store.advance_stage(
        lease, tenant_id="1", user_id="900018", request_id=start.request_id,
        stage=RunStageKey.PERSISTING,
    )
    store.record_source_health(
        lease, tenant_id="1", user_id="900018", request_id=start.request_id,
        observations=(SourceHealthObservation(
            source_type="WORK_ITEM", status=RunSourceHealthStatus.SUCCESS,
            observed_at=now[0], latency_ms=84,
        ),),
    )
    now[0] += timedelta(milliseconds=300)
    store.complete(
        _response(start, lease.run_id), lease=lease, tenant_id="1", user_id="900018"
    )

    summary = InMemoryUserRunStore(store).get(
        tenant_id="1", user_id="900018", run_id=UUID(lease.run_id)
    )
    assert summary is not None
    assert summary.activity_title == "DWAI·ON Assistant execution"
    assert summary.attempt == 1
    assert summary.lease.status == "RELEASED"
    assert summary.current_stage == "COMPLETED"
    assert summary.progress_percent == 100
    assert summary.measurement_status == "MEASURED"
    assert [(stage.key, stage.state) for stage in summary.stages] == [
        ("AUTHORIZING", "COMPLETED"),
        ("RETRIEVING", "SKIPPED"),
        ("REASONING", "SKIPPED"),
        ("VERIFYING", "SKIPPED"),
        ("PERSISTING", "COMPLETED"),
        ("COMPLETED", "COMPLETED"),
    ]
    assert summary.stages[0].duration_ms == 200
    assert summary.source_health[0].latency_ms == 84
    assert summary.source_health[0].last_success_at == now[0] - timedelta(milliseconds=300)
    assert summary.audit_evidence.audit_record_id == audit_record_id(start.audit_id or "")
    assert summary.audit_evidence.status == "PENDING"
    assert "query" not in summary.model_dump_json().lower()


def test_failed_attempt_is_partial_and_rejects_backward_or_cross_owner_updates() -> None:
    now = [datetime(2026, 9, 7, 2, 0, tzinfo=timezone.utc)]
    store = InMemoryRunStore(clock=lambda: now[0])
    start = _start(audit_id=str(uuid4()))
    lease = store.begin(start)
    assert lease is not None
    for stage in (RunStageKey.RETRIEVING, RunStageKey.REASONING):
        now[0] += timedelta(seconds=1)
        store.advance_stage(
            lease, tenant_id="1", user_id="900018", request_id=start.request_id,
            stage=stage,
        )
    with pytest.raises(RunStoreUnavailable):
        store.advance_stage(
            lease, tenant_id="1", user_id="900018", request_id=start.request_id,
            stage=RunStageKey.RETRIEVING,
        )
    with pytest.raises(RunStoreUnavailable):
        store.record_source_health(
            lease, tenant_id="1", user_id="foreign", request_id=start.request_id,
            observations=(SourceHealthObservation(
                source_type="MAIL", status=RunSourceHealthStatus.UNAVAILABLE,
                observed_at=now[0], latency_ms=3_000,
            ),),
        )
    now[0] += timedelta(seconds=1)
    store.fail(lease, "ASK_RUNTIME_FAILED")

    summary = InMemoryUserRunStore(store).get(
        tenant_id="1", user_id="900018", run_id=UUID(lease.run_id)
    )
    assert summary is not None
    assert summary.run_state == "FAILED"
    assert summary.current_stage == "FAILED"
    assert summary.progress_percent == 40
    assert summary.measurement_status == "PARTIAL"
    assert [stage.state for stage in summary.stages] == [
        "COMPLETED", "COMPLETED", "FAILED", "FAILED"
    ]


def test_sample_runs_are_visible_only_in_dwaion_run_views_not_activity_kpis() -> None:
    store = InMemoryRunStore()
    start = _start(audit_id=str(uuid4()))
    lease = store.begin(start)
    assert lease is not None
    store.complete(
        _response(start, lease.run_id), lease=lease, tenant_id="1", user_id="900018"
    )
    store._activity[lease.run_id] = replace(  # local seed provenance boundary
        store._activity[lease.run_id], data_provenance="SAMPLE"
    )

    user_rows = InMemoryUserRunStore(store).list(
        tenant_id="1", user_id="900018", limit=10, run_state=None
    )
    activity = InMemoryAgentActivityStore(store)
    page, has_more = activity.page(
        tenant_id="1", user_id="900018", filters=ActivityFilters(),
        snapshot_at=datetime.now(timezone.utc), now=datetime.now(timezone.utc),
        after=None, limit=10,
    )
    assert user_rows[0].data_provenance == "SAMPLE"
    assert page == [] and has_more is False
    assert activity.counts(
        tenant_id="1", user_id="900018", filters=ActivityFilters(),
        now=datetime.now(timezone.utc),
    ) == {}


def test_postgres_projection_keeps_telemetry_on_the_selected_attempt_snapshot() -> None:
    run_id = uuid4()
    observed_at = datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)
    row = [None] * 19
    row[0] = run_id
    row[13] = 1
    connection = _TelemetryConnection(
        stage_rows=[
            (run_id, 1, "AUTHORIZING", "COMPLETED", 10, observed_at, observed_at),
            (run_id, 2, "AUTHORIZING", "ACTIVE", 10, observed_at, None),
        ],
        source_rows=[
            (run_id, 1, "MAIL", "SUCCESS", 40, observed_at, observed_at),
            (run_id, 2, "MAIL", "UNAVAILABLE", 3000, observed_at, None),
        ],
    )

    stages, sources = _postgres_observability(
        connection, "1", "900018", [tuple(row)]
    )

    assert [(stage.state, stage.completed_at) for stage in stages[run_id]] == [
        ("COMPLETED", observed_at)
    ]
    assert [(source.status, source.latency_ms) for source in sources[run_id]] == [
        ("SUCCESS", 40)
    ]


def test_postgres_projection_configures_one_repeatable_read_snapshot() -> None:
    connection = _TelemetryConnection(stage_rows=[], source_rows=[])
    _configure_read_snapshot(connection)
    assert connection.queries == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        "SET LOCAL statement_timeout = '5s'",
    ]


def test_public_contract_exposes_provenance_as_a_closed_enum() -> None:
    schemas = app.openapi()["components"]["schemas"]
    assert schemas["RunDataProvenance"]["enum"] == ["LIVE", "SAMPLE"]


@pytest.mark.integration
def test_postgres_completion_is_fenced_by_tenant_user_request_and_audit() -> None:
    database_url = _integration_database_url()
    _apply_migrations(database_url)
    tenant_id = str(700_000_000 + uuid4().int % 100_000_000)
    user_id = "observability-owner"
    start = _start(audit_id=str(uuid4()), tenant_id=tenant_id, user_id=user_id)
    store = PostgresRunStore(database_url, _IntegrationEncryption())  # type: ignore[arg-type]
    lease = store.begin(start)
    assert lease is not None
    response = _response(start, lease.run_id)

    try:
        for candidate, candidate_tenant, candidate_user in (
            (response, str(int(tenant_id) + 1), user_id),
            (response, tenant_id, "foreign-user"),
            (response.model_copy(update={"request_id": "foreign-request"}), tenant_id, user_id),
            (response.model_copy(update={"audit_id": str(uuid4())}), tenant_id, user_id),
        ):
            with pytest.raises(RunStoreUnavailable, match="could not be completed"):
                store.complete(
                    candidate,
                    lease=lease,
                    tenant_id=candidate_tenant,
                    user_id=candidate_user,
                )

        with connect(database_url) as connection:
            before = connection.execute(
                """SELECT run_state, request_id, current_audit_id
                     FROM ai_agent_runs WHERE run_id = %s""",
                (UUID(lease.run_id),),
            ).fetchone()
        assert before == ("RUNNING", start.request_id, start.audit_id)

        store.complete(
            response, lease=lease, tenant_id=tenant_id, user_id=user_id
        )
        with connect(database_url) as connection:
            completed = connection.execute(
                """SELECT run_state, audit_link_state,
                          COUNT(stage_key) FILTER (WHERE stage_key = 'COMPLETED')
                     FROM ai_agent_runs
                     LEFT JOIN ai_agent_run_stages USING (run_id)
                    WHERE run_id = %s
                    GROUP BY run_state, audit_link_state""",
                (UUID(lease.run_id),),
            ).fetchone()
        assert completed == ("COMPLETED", "PENDING", 1)
    finally:
        with connect(database_url) as connection:
            connection.execute(
                "DELETE FROM ai_agent_runs WHERE tenant_id = %s", (int(tenant_id),)
            )


@pytest.mark.integration
def test_postgres_observability_reads_one_repeatable_snapshot() -> None:
    database_url = _integration_database_url()
    _apply_migrations(database_url)
    tenant_id = str(700_000_000 + uuid4().int % 100_000_000)
    user_id = "snapshot-owner"
    start = _start(audit_id=str(uuid4()), tenant_id=tenant_id, user_id=user_id)
    store = PostgresRunStore(database_url, _IntegrationEncryption())  # type: ignore[arg-type]
    lease = store.begin(start)
    assert lease is not None

    try:
        with connect(database_url) as reader:
            _configure_read_snapshot(reader)
            selected = reader.execute(
                """SELECT run_id, lease_generation FROM ai_agent_runs
                    WHERE tenant_id = %s AND user_id = %s AND run_id = %s""",
                (int(tenant_id), user_id, UUID(lease.run_id)),
            ).fetchone()
            assert selected == (UUID(lease.run_id), 1)
            projected = [None] * 19
            projected[0], projected[13] = selected

            with connect(database_url) as writer:
                writer.execute(
                    """UPDATE ai_agent_run_stages
                          SET stage_state = 'COMPLETED', completed_at = CURRENT_TIMESTAMP
                        WHERE run_id = %s AND lease_generation = 1
                          AND stage_key = 'AUTHORIZING'""",
                    (UUID(lease.run_id),),
                )
                writer.execute(
                    """INSERT INTO ai_agent_run_stages (
                           run_id, lease_generation, stage_key, stage_state,
                           sequence, started_at)
                       VALUES (%s, 1, 'RETRIEVING', 'ACTIVE', 20, CURRENT_TIMESTAMP)""",
                    (UUID(lease.run_id),),
                )

            stages, _ = _postgres_observability(
                reader, tenant_id, user_id, [tuple(projected)]
            )
            assert [(stage.key, stage.state) for stage in stages[UUID(lease.run_id)]] == [
                ("AUTHORIZING", "ACTIVE")
            ]

        with connect(database_url) as connection:
            visible_after_snapshot = connection.execute(
                """SELECT stage_key, stage_state FROM ai_agent_run_stages
                    WHERE run_id = %s AND lease_generation = 1
                    ORDER BY sequence""",
                (UUID(lease.run_id),),
            ).fetchall()
        assert visible_after_snapshot == [
            ("AUTHORIZING", "COMPLETED"),
            ("RETRIEVING", "ACTIVE"),
        ]
    finally:
        with connect(database_url) as connection:
            connection.execute(
                "DELETE FROM ai_agent_runs WHERE tenant_id = %s", (int(tenant_id),)
            )


class _Rows:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple]:
        return self.rows


class _TelemetryConnection:
    def __init__(self, *, stage_rows: list[tuple], source_rows: list[tuple]) -> None:
        self.stage_rows = stage_rows
        self.source_rows = source_rows
        self.queries: list[str] = []

    def execute(self, query: str, _parameters: tuple | None = None) -> _Rows:
        self.queries.append(query.strip())
        if "ai_agent_run_stages" in query:
            return _Rows(self.stage_rows)
        if "ai_agent_run_source_health" in query:
            return _Rows(self.source_rows)
        return _Rows([])


class _IntegrationEncryption:
    def encrypt_bytes(self, *_args, **_kwargs) -> str:
        return "dwp2.observability-test-envelope"


def _integration_database_url() -> str:
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    database_name = urlparse(database_url).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Run observability tests require a dedicated test database.")
    return database_url


def _start(
    *, audit_id: str, tenant_id: str = "1", user_id: str = "900018"
) -> RunStart:
    return RunStart(
        run_id=str(uuid4()), tenant_id=tenant_id, user_id=user_id,
        request_id=f"request-{uuid4()}", query_hash="a" * 64,
        agent_key="DWP_ASSISTANT", agent_revision=1, risk_tier="L1",
        policy_outcome="ALLOW", locale="ko-KR", correlation_id="safe-correlation",
        audit_id=audit_id,
    )


def _response(start: RunStart, run_id: str) -> AskResponse:
    return AskResponse.model_validate({
        "runId": run_id, "auditId": start.audit_id, "requestId": start.request_id,
        "correlationId": start.correlation_id, "state": "ABSTAINED", "sourceCount": 0,
        "policy": {"outcome": "ALLOW", "riskTier": "L1", "code": "NO_EVIDENCE",
                   "explanation": "No evidence.", "modelAllowed": False},
        "modelRoute": {"state": "NOT_INVOKED"},
        "agentRegistry": {"entryKey": "DWP_ASSISTANT", "revision": 1,
                          "artifactVersion": "test-v1", "riskTier": "LOW",
                          "resolution": "ACTIVE"},
        "statusCode": "NO_EVIDENCE", "completedAt": datetime.now(timezone.utc),
    })
