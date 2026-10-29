from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from psycopg import connect

from dwp_agent.activity_store import (
    ActivityFilters,
    InMemoryAgentActivityStore,
    _configure_activity_summary_snapshot,
)
from dwp_agent.audit_delivery_ack import acknowledge_agent_run_delivery
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
    assert summary.state_changed_at == now[0]
    assert summary.safe_error_code is None
    assert summary.recovery_hint is None
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
    assert summary.state_changed_at == now[0]
    assert summary.safe_error_code == "ASK_RUNTIME_FAILED"
    assert summary.recovery_hint == "RETRY_LATER"
    assert [stage.state for stage in summary.stages] == [
        "COMPLETED", "COMPLETED", "FAILED", "FAILED"
    ]


@pytest.mark.parametrize(
    ("failure_detail", "forbidden_fragments"),
    [
        ("PRIVATE_PROVIDER_STACK_CODE", ("PRIVATE_PROVIDER_STACK_CODE",)),
        (
            "PRIVATE provider text user@example.com",
            ("PRIVATE", "user@example.com"),
        ),
    ],
)
def test_unknown_or_raw_failure_details_are_reduced_to_one_public_code(
    failure_detail: str,
    forbidden_fragments: tuple[str, ...],
) -> None:
    now = datetime(2026, 9, 7, 2, 20, tzinfo=timezone.utc)
    store = InMemoryRunStore(clock=lambda: now)
    start = _start(audit_id=str(uuid4()))
    lease = store.begin(start)
    assert lease is not None

    store.fail(lease, failure_detail)

    summary = InMemoryUserRunStore(store).get(
        tenant_id="1", user_id="900018", run_id=UUID(lease.run_id)
    )
    assert summary is not None
    assert summary.safe_error_code == "AGENT_EXECUTION_FAILED"
    assert summary.recovery_hint == "CONTACT_SUPPORT"
    serialized = summary.model_dump_json()
    assert all(fragment not in serialized for fragment in forbidden_fragments)


def test_expired_user_run_projects_lease_expiry_as_effective_state_change() -> None:
    started_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    store = InMemoryRunStore(clock=lambda: started_at)
    start = _start(audit_id=str(uuid4()))
    lease = store.begin(start)
    assert lease is not None

    summary = InMemoryUserRunStore(store).get(
        tenant_id="1", user_id="900018", run_id=UUID(lease.run_id)
    )
    snapshot = store.activity_snapshots(tenant_id="1", user_id="900018")[0]

    assert summary is not None
    assert summary.lease.status == "EXPIRED"
    assert summary.state_changed_at == snapshot.lease_expires_at


def test_newer_source_failure_replaces_success_with_constraint_safe_snapshot() -> None:
    now = datetime(2026, 9, 7, 2, 30, tzinfo=timezone.utc)
    store = InMemoryRunStore(clock=lambda: now)
    start = _start(audit_id=str(uuid4()))
    lease = store.begin(start)
    assert lease is not None

    store.record_source_health(
        lease,
        tenant_id="1",
        user_id="900018",
        request_id=start.request_id,
        observations=(SourceHealthObservation(
            source_type="WORK_ITEM",
            status=RunSourceHealthStatus.SUCCESS,
            observed_at=now,
        ),),
    )
    store.record_source_health(
        lease,
        tenant_id="1",
        user_id="900018",
        request_id=start.request_id,
        observations=(SourceHealthObservation(
            source_type="WORK_ITEM",
            status=RunSourceHealthStatus.UNAVAILABLE,
            observed_at=now + timedelta(seconds=1),
        ),),
    )

    health = store.run_source_health(
        tenant_id="1",
        user_id="900018",
        run_id=UUID(lease.run_id),
        generation=lease.generation,
    )
    assert [(item.status, item.last_success_at) for item in health] == [
        ("UNAVAILABLE", now)
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

    user_rows, user_has_more = InMemoryUserRunStore(store).page(
        tenant_id="1",
        user_id="900018",
        limit=10,
        run_state=None,
        from_at=None,
        to_at=None,
        snapshot_at=datetime.now(timezone.utc),
        after=None,
    )
    activity = InMemoryAgentActivityStore(store)
    page, has_more = activity.page(
        tenant_id="1", user_id="900018", filters=ActivityFilters(),
        snapshot_at=datetime.now(timezone.utc), now=datetime.now(timezone.utc),
        after=None, limit=10,
    )
    assert user_rows[0].data_provenance == "SAMPLE"
    assert user_has_more is False
    assert page == [] and has_more is False
    assert activity.summary(
        tenant_id="1",
        user_id="900018",
        filters=ActivityFilters(),
        now=datetime.now(timezone.utc),
        attention_limit=5,
    ) == ({}, [])


def test_user_run_pages_apply_owner_time_range_and_stable_keyset() -> None:
    base = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    now = [base]
    store = InMemoryRunStore(clock=lambda: now[0])
    run_ids: list[UUID] = []
    for _ in range(3):
        lease = store.begin(_start(audit_id=str(uuid4())))
        assert lease is not None
        run_ids.append(UUID(lease.run_id))
        now[0] += timedelta(hours=1)
    foreign = store.begin(
        _start(audit_id=str(uuid4()), tenant_id="1", user_id="foreign")
    )
    assert foreign is not None

    reader = InMemoryUserRunStore(store)
    first, more = reader.page(
        tenant_id="1",
        user_id="900018",
        limit=1,
        run_state=None,
        from_at=base + timedelta(hours=1),
        to_at=base + timedelta(hours=3),
        snapshot_at=now[0],
        after=None,
    )
    second, tail_more = reader.page(
        tenant_id="1",
        user_id="900018",
        limit=1,
        run_state=None,
        from_at=base + timedelta(hours=1),
        to_at=base + timedelta(hours=3),
        snapshot_at=now[0],
        after=(first[-1].created_at, first[-1].run_id),
    )

    assert [row.run_id for row in first] == [run_ids[2]]
    assert [row.run_id for row in second] == [run_ids[1]]
    assert more is True and tail_more is False
    assert UUID(foreign.run_id) not in {row.run_id for row in first + second}


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


def test_activity_summary_counts_and_attention_share_one_repeatable_read_snapshot() -> None:
    connection = _TelemetryConnection(stage_rows=[], source_rows=[])
    _configure_activity_summary_snapshot(connection)
    assert connection.queries == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        "SET LOCAL statement_timeout = '5s'",
    ]


def test_public_contract_exposes_provenance_as_a_closed_enum() -> None:
    schemas = app.openapi()["components"]["schemas"]
    assert schemas["RunDataProvenance"]["enum"] == ["LIVE", "SAMPLE"]


@pytest.mark.integration
def test_postgres_completion_is_fenced_by_tenant_user_request_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            response,
            lease=lease,
            tenant_id=tenant_id,
            user_id=user_id,
            safe_error_code="MODEL_PROVIDER_UNAVAILABLE",
        )
        with connect(database_url) as connection:
            completed = connection.execute(
                """SELECT run_state, audit_link_state, safe_error_code,
                          state_changed_at >= created_at,
                          (SELECT COUNT(*) FROM ai_agent_run_stages stage
                            WHERE stage.run_id = run.run_id
                              AND stage.stage_key = 'COMPLETED')
                     FROM ai_agent_runs run
                    WHERE run_id = %s""",
                (UUID(lease.run_id),),
            ).fetchone()
        assert completed == (
            "COMPLETED",
            "PENDING",
            "MODEL_PROVIDER_UNAVAILABLE",
            True,
            1,
        )

        monkeypatch.setenv("DWP_AGENT_DATABASE_URL", database_url)
        delivered_event = {
            "eventId": str(audit_record_id(response.audit_id)),
            "tenantId": int(tenant_id),
            "sourceService": "dwp-agent-runtime",
            "targetType": "AGENT_RUN",
            "targetId": lease.run_id,
        }
        assert acknowledge_agent_run_delivery(delivered_event) is True
        assert acknowledge_agent_run_delivery(delivered_event) is True
        with connect(database_url) as connection:
            link_state = connection.execute(
                """SELECT run.audit_link_state, link.link_state
                     FROM ai_agent_runs run
                     JOIN ai_agent_run_audit_links link
                       ON link.run_id = run.run_id
                      AND link.lease_generation = run.lease_generation
                    WHERE run.run_id = %s""",
                (UUID(lease.run_id),),
            ).fetchone()
        assert link_state == ("LINKED", "LINKED")
    finally:
        with connect(database_url) as connection:
            connection.execute(
                "DELETE FROM ai_agent_runs WHERE tenant_id = %s", (int(tenant_id),)
            )


@pytest.mark.integration
def test_postgres_retry_preserves_and_acknowledges_each_audit_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _integration_database_url()
    _apply_migrations(database_url)
    tenant_id = str(700_000_000 + uuid4().int % 100_000_000)
    first = _start(audit_id=str(uuid4()), tenant_id=tenant_id)
    store = PostgresRunStore(database_url, _IntegrationEncryption())  # type: ignore[arg-type]
    first_lease = store.begin(first)
    assert first_lease is not None

    try:
        store.fail(first_lease, "ASK_RUNTIME_FAILED")
        retry = replace(
            first,
            run_id=str(uuid4()),
            audit_id=str(uuid4()),
            correlation_id="audit-retry",
        )
        retry_lease = store.begin(retry)
        assert retry_lease is not None
        assert retry_lease.run_id == first_lease.run_id
        assert retry_lease.generation == first_lease.generation + 1

        monkeypatch.setenv("DWP_AGENT_DATABASE_URL", database_url)
        first_event = {
            "eventId": str(audit_record_id(first.audit_id or "")),
            "tenantId": int(tenant_id),
            "sourceService": "dwp-agent-runtime",
            "targetType": "AGENT_RUN",
            "targetId": retry_lease.run_id,
        }
        retry_event = {
            **first_event,
            "eventId": str(audit_record_id(retry.audit_id or "")),
        }

        assert acknowledge_agent_run_delivery(first_event) is True
        assert acknowledge_agent_run_delivery(first_event) is True
        with connect(database_url) as connection:
            before_latest_ack = connection.execute(
                """SELECT run.audit_record_id, run.audit_link_state,
                          array_agg(link.link_state ORDER BY link.lease_generation)
                     FROM ai_agent_runs run
                     JOIN ai_agent_run_audit_links link ON link.run_id = run.run_id
                    WHERE run.run_id = %s
                    GROUP BY run.audit_record_id, run.audit_link_state""",
                (UUID(retry_lease.run_id),),
            ).fetchone()
        assert before_latest_ack == (
            audit_record_id(retry.audit_id or ""),
            "PENDING",
            ["LINKED", "PENDING"],
        )

        assert acknowledge_agent_run_delivery(retry_event) is True
        with connect(database_url) as connection:
            after_latest_ack = connection.execute(
                """SELECT run.audit_link_state,
                          array_agg(link.link_state ORDER BY link.lease_generation)
                     FROM ai_agent_runs run
                     JOIN ai_agent_run_audit_links link ON link.run_id = run.run_id
                    WHERE run.run_id = %s
                    GROUP BY run.audit_link_state""",
                (UUID(retry_lease.run_id),),
            ).fetchone()
        assert after_latest_ack == ("LINKED", ["LINKED", "LINKED"])
    finally:
        with connect(database_url) as connection:
            connection.execute(
                "DELETE FROM ai_agent_runs WHERE tenant_id = %s", (int(tenant_id),)
            )


@pytest.mark.integration
def test_postgres_current_projection_ack_is_safe_before_audit_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _integration_database_url()
    _apply_migrations(database_url)
    tenant_id = str(700_000_000 + uuid4().int % 100_000_000)
    start = _start(audit_id=str(uuid4()), tenant_id=tenant_id)
    store = PostgresRunStore(database_url, _IntegrationEncryption())  # type: ignore[arg-type]
    lease = store.begin(start)
    assert lease is not None
    event_id = audit_record_id(start.audit_id or "")

    try:
        # Recreate the V73-to-V79 rollout window: the legacy run projection is
        # present, but its exact attempt link has not been backfilled yet.
        with connect(database_url) as connection:
            connection.execute(
                "DELETE FROM ai_agent_run_audit_links WHERE run_id = %s",
                (UUID(lease.run_id),),
            )

        monkeypatch.setenv("DWP_AGENT_DATABASE_URL", database_url)
        assert acknowledge_agent_run_delivery({
            "eventId": str(event_id),
            "occurredAt": datetime.now(timezone.utc).isoformat(),
            "tenantId": int(tenant_id),
            "sourceService": "dwp-agent-runtime",
            "targetType": "AGENT_RUN",
            "targetId": lease.run_id,
        }) is True

        # Replaying the later online backfill must preserve the acknowledgement,
        # never recreate a permanently PENDING row after the spool was removed.
        with connect(database_url) as connection:
            connection.execute(
                "DELETE FROM sys_schema_history WHERE version = 'V79'"
            )
        _apply_migrations(database_url)

        with connect(database_url) as connection:
            state = connection.execute(
                """SELECT run.audit_link_state, link.link_state,
                          legacy.event_id
                     FROM ai_agent_runs run
                     JOIN ai_agent_run_audit_links link
                       ON link.run_id = run.run_id
                      AND link.lease_generation = run.lease_generation
                     LEFT JOIN ai_agent_run_audit_legacy_receipts legacy
                       ON legacy.run_id = run.run_id
                    WHERE run.run_id = %s""",
                (UUID(lease.run_id),),
            ).fetchone()
        assert state == ("LINKED", "LINKED", None)
    finally:
        with connect(database_url) as connection:
            connection.execute(
                "DELETE FROM ai_agent_runs WHERE tenant_id = %s", (int(tenant_id),)
            )


@pytest.mark.integration
def test_postgres_pre_v73_orphan_receipt_does_not_starve_or_link_latest_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _integration_database_url()
    _apply_migrations(database_url)
    tenant_id = str(700_000_000 + uuid4().int % 100_000_000)
    first = _start(audit_id=str(uuid4()), tenant_id=tenant_id)
    store = PostgresRunStore(database_url, _IntegrationEncryption())  # type: ignore[arg-type]
    first_lease = store.begin(first)
    assert first_lease is not None

    try:
        store.fail(first_lease, "ASK_RUNTIME_FAILED")
        retry = replace(
            first,
            run_id=str(uuid4()),
            audit_id=str(uuid4()),
            correlation_id="pre-v73-audit-retry",
        )
        retry_lease = store.begin(retry)
        assert retry_lease is not None
        with connect(database_url) as connection:
            connection.execute(
                """DELETE FROM ai_agent_run_audit_links
                    WHERE run_id = %s AND lease_generation = %s""",
                (UUID(retry_lease.run_id), first_lease.generation),
            )
            connection.execute(
                """UPDATE ai_agent_runs
                      SET created_at = (
                          SELECT cutover_at - INTERVAL '1 second'
                            FROM ai_agent_run_audit_link_cutover
                           WHERE singleton = 1)
                    WHERE run_id = %s""",
                (UUID(retry_lease.run_id),),
            )

        monkeypatch.setenv("DWP_AGENT_DATABASE_URL", database_url)
        orphan_event_id = audit_record_id(first.audit_id or "")
        orphan_event = {
            "eventId": str(orphan_event_id),
            "occurredAt": datetime.now(timezone.utc).isoformat(),
            "tenantId": int(tenant_id),
            "sourceService": "dwp-agent-runtime",
            "targetType": "AGENT_RUN",
            "targetId": retry_lease.run_id,
        }
        assert acknowledge_agent_run_delivery(orphan_event) is True
        assert acknowledge_agent_run_delivery(orphan_event) is True
        assert acknowledge_agent_run_delivery({
            **orphan_event,
            "eventId": str(uuid4()),
            "metadata": {"auditLinkSchemaVersion": 2, "leaseGeneration": 1},
        }) is False
        assert acknowledge_agent_run_delivery({
            **orphan_event,
            "eventId": str(uuid4()),
            "occurredAt": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        }) is False

        with connect(database_url) as connection:
            state = connection.execute(
                """SELECT run.audit_link_state,
                          receipt.reason,
                          current_link.link_state
                     FROM ai_agent_runs run
                     JOIN ai_agent_run_audit_legacy_receipts receipt
                       ON receipt.run_id = run.run_id AND receipt.event_id = %s
                     JOIN ai_agent_run_audit_links current_link
                       ON current_link.run_id = run.run_id
                      AND current_link.lease_generation = run.lease_generation
                    WHERE run.run_id = %s""",
                (orphan_event_id, UUID(retry_lease.run_id)),
            ).fetchone()
        assert state == ("PENDING", "PRE_V73_UNRECOVERABLE_LINK", "PENDING")
    finally:
        with connect(database_url) as connection:
            connection.execute(
                "DELETE FROM ai_agent_runs WHERE tenant_id = %s", (int(tenant_id),)
            )


@pytest.mark.integration
def test_postgres_rolling_upgrade_trigger_captures_old_writer_and_transition_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _integration_database_url()
    _apply_migrations(database_url)
    tenant_id = 700_000_000 + uuid4().int % 100_000_000
    run_id = uuid4()
    audit_id = str(uuid4())
    event_id = audit_record_id(audit_id)

    try:
        with connect(database_url) as connection:
            connection.execute(
                """INSERT INTO ai_agent_runs (
                       run_id, tenant_id, user_id, request_id, query_hash,
                       agent_key, agent_revision, run_state, risk_tier, policy_outcome,
                       locale, correlation_id, lease_expires_at, lease_generation,
                       current_audit_id, audit_record_id, audit_link_state)
                   VALUES (%s, %s, 'old-writer', 'old-writer-request', %s,
                           'DWP_ASSISTANT', 1, 'RUNNING', 'L1', 'ALLOW',
                           'ko-KR', 'old-writer-correlation',
                           CURRENT_TIMESTAMP + INTERVAL '2 minutes', 1,
                           %s, %s, 'PENDING')""",
                (run_id, tenant_id, "a" * 64, audit_id, event_id),
            )
        with connect(database_url) as connection:
            before = connection.execute(
                """SELECT run.state_changed_at, link.event_id, link.link_state
                     FROM ai_agent_runs run
                     JOIN ai_agent_run_audit_links link
                       ON link.run_id = run.run_id
                      AND link.lease_generation = run.lease_generation
                    WHERE run.run_id = %s""",
                (run_id,),
            ).fetchone()
        assert before is not None
        assert before[1:] == (event_id, "PENDING")

        with connect(database_url) as connection:
            connection.execute("SELECT pg_sleep(0.01)")
            connection.execute(
                """UPDATE ai_agent_runs
                      SET run_state = 'FAILED', safe_error_code = 'ASK_RUNTIME_FAILED',
                          completed_at = CURRENT_TIMESTAMP, lease_expires_at = NULL
                    WHERE run_id = %s""",
                (run_id,),
            )
        with connect(database_url) as connection:
            transitioned_at = connection.execute(
                "SELECT state_changed_at FROM ai_agent_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()[0]
        assert transitioned_at > before[0]

        monkeypatch.setenv("DWP_AGENT_DATABASE_URL", database_url)
        assert acknowledge_agent_run_delivery({
            "eventId": str(event_id),
            "occurredAt": datetime.now(timezone.utc).isoformat(),
            "tenantId": tenant_id,
            "sourceService": "dwp-agent-runtime",
            "targetType": "AGENT_RUN",
            "targetId": str(run_id),
        }) is True
    finally:
        with connect(database_url) as connection:
            connection.execute("DELETE FROM ai_agent_runs WHERE run_id = %s", (run_id,))


@pytest.mark.integration
def test_postgres_newer_source_failure_replaces_success_without_constraint_error() -> None:
    database_url = _integration_database_url()
    _apply_migrations(database_url)
    tenant_id = str(700_000_000 + uuid4().int % 100_000_000)
    user_id = "source-recheck-owner"
    start = _start(audit_id=str(uuid4()), tenant_id=tenant_id, user_id=user_id)
    store = PostgresRunStore(database_url, _IntegrationEncryption())  # type: ignore[arg-type]
    lease = store.begin(start)
    assert lease is not None
    observed_at = datetime.now(timezone.utc)

    try:
        store.record_source_health(
            lease,
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=start.request_id,
            observations=(SourceHealthObservation(
                source_type="WORK_ITEM",
                status=RunSourceHealthStatus.SUCCESS,
                observed_at=observed_at,
            ),),
        )
        store.record_source_health(
            lease,
            tenant_id=tenant_id,
            user_id=user_id,
            request_id=start.request_id,
            observations=(SourceHealthObservation(
                source_type="WORK_ITEM",
                status=RunSourceHealthStatus.UNAVAILABLE,
                observed_at=observed_at + timedelta(seconds=1),
            ),),
        )
        with connect(database_url) as connection:
            row = connection.execute(
                """SELECT health_status, last_success_at
                     FROM ai_agent_run_source_health
                    WHERE run_id = %s AND lease_generation = %s
                      AND source_type = 'WORK_ITEM'""",
                (UUID(lease.run_id), lease.generation),
            ).fetchone()
        assert row == ("UNAVAILABLE", observed_at)
    finally:
        with connect(database_url) as connection:
            connection.execute(
                "DELETE FROM ai_agent_runs WHERE tenant_id = %s", (int(tenant_id),)
            )


@pytest.mark.integration
def test_postgres_completion_accepts_governed_attachment_citation() -> None:
    database_url = _integration_database_url()
    _apply_migrations(database_url)
    tenant_id = str(700_000_000 + uuid4().int % 100_000_000)
    user_id = "attachment-citation-owner"
    start = _start(audit_id=str(uuid4()), tenant_id=tenant_id, user_id=user_id)
    store = PostgresRunStore(database_url, _IntegrationEncryption())  # type: ignore[arg-type]
    lease = store.begin(start)
    assert lease is not None
    response = AskResponse.model_validate({
        "runId": lease.run_id,
        "auditId": start.audit_id,
        "requestId": start.request_id,
        "correlationId": start.correlation_id,
        "state": "COMPLETED",
        "answer": "The verified attachment contains the requested evidence [src-01].",
        "confidence": "HIGH",
        "citations": [{
            "sourceId": "src-01",
            "sourceType": "ATTACHMENT",
            "title": "evidence.txt: page 1",
            "sourceSystem": "DWAI_ON_ATTACHMENT",
        }],
        "sourceCount": 1,
        "policy": {
            "outcome": "ALLOW",
            "riskTier": "L1",
            "code": "ALLOW",
            "explanation": "Grounded attachment evidence is available.",
            "modelAllowed": True,
        },
        "modelRoute": {
            "state": "COMPLETED",
            "provider": "OPENAI",
            "model": "gpt-test",
        },
        "agentRegistry": {
            "entryKey": "DWP_ASSISTANT",
            "revision": 1,
            "artifactVersion": "test-v1",
            "riskTier": "LOW",
            "resolution": "ACTIVE",
        },
        "statusCode": "ANSWER_GROUNDED",
        "completedAt": datetime.now(timezone.utc),
    })

    try:
        store.complete(
            response,
            lease=lease,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        with connect(database_url) as connection:
            source_type = connection.execute(
                "SELECT source_type FROM ai_agent_citations WHERE run_id = %s",
                (UUID(lease.run_id),),
            ).fetchone()[0]
        assert source_type == "ATTACHMENT"
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
