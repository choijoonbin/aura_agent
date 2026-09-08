from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from psycopg import connect

import dwp_agent.activity_api as api
import dwp_agent.run_store as run_store_module
from dwp_agent.activity_cursor import InvalidActivityCursor, decode_cursor, encode_cursor
from dwp_agent.activity_store import (
    ActivityFilters, ActivityStoreUnavailable, InMemoryAgentActivityStore,
    PostgresAgentActivityStore, get_agent_activity_store,
)
from dwp_agent.contracts import AskResponse
from dwp_agent.main import app
from dwp_agent.run_store import InMemoryRunStore, PostgresRunStore, RunStart, RunStoreUnavailable, _apply_migrations
from dwp_agent.user_run_store import InMemoryUserRunStore, get_user_run_store


def start(*, tenant: str = "42", user: str = "7", run_id: str | None = None) -> RunStart:
    return RunStart(run_id=run_id or str(uuid4()), tenant_id=tenant, user_id=user,
                    request_id=str(uuid4()), query_hash="a" * 64,
                    agent_key="DWP_ASSISTANT", agent_revision=1, risk_tier="L0",
                    policy_outcome="ALLOW", locale="en", correlation_id="private-correlation-not-exported")


def response_for(run: RunStart, run_id: str) -> AskResponse:
    return AskResponse.model_validate({
        "runId": run_id, "auditId": str(uuid4()), "requestId": run.request_id,
        "correlationId": run.correlation_id, "state": "ABSTAINED", "sourceCount": 0,
        "policy": {"outcome": "ALLOW", "riskTier": "L0", "code": "NO_SCOPED_EVIDENCE",
                   "explanation": "No evidence.", "modelAllowed": False},
        "modelRoute": {"state": "NOT_INVOKED"},
        "agentRegistry": {"entryKey": "DWP_ASSISTANT", "revision": 1,
                          "artifactVersion": "test-v1", "riskTier": "LOW", "resolution": "ACTIVE"},
        "statusCode": "NO_SCOPED_EVIDENCE", "completedAt": datetime.now(timezone.utc),
    })


class TestEncryption:
    def encrypt_bytes(self, *_args, **_kwargs) -> str:
        return "dwp2.activity-test-envelope"


@pytest.fixture(params=["memory", "postgres"])
def source(request):
    if request.param == "memory":
        runs = InMemoryRunStore()
        yield runs, InMemoryAgentActivityStore(runs), "42", None
        return
    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    if not urlparse(database_url).path.removeprefix("/").endswith(("_test", "_integration", "_verify")):
        pytest.fail("Activity source tests require a dedicated test database.")
    _apply_migrations(database_url)
    tenant = str(900_000_000 + uuid4().int % 90_000_000)
    runs = PostgresRunStore(database_url, TestEncryption())
    try:
        yield runs, PostgresAgentActivityStore(database_url), tenant, database_url
    finally:
        # This tenant was generated exclusively for this test in a guarded test DB.
        with connect(database_url) as connection:
            connection.execute("DELETE FROM ai_agent_runs WHERE tenant_id = %s", (int(tenant),))


def detail(source, tenant, run_id):
    result = source.detail(tenant_id=tenant, user_id="7", run_id=UUID(run_id))
    assert result is not None
    return result


def test_real_lifecycle_idempotency_fenced_retry_and_late_delivery(source):
    runs, activity, tenant, _ = source
    run = start(tenant=tenant)
    first = runs.begin(run)
    assert first is not None
    assert runs.begin(run) is None
    first_snapshot = detail(activity, tenant, first.run_id)
    assert first_snapshot.execution_version(datetime.now(timezone.utc)) == 2
    runs.fail(first, "FAILED_SAFE_CODE")
    assert detail(activity, tenant, first.run_id).run_state == "FAILED"
    assert detail(activity, tenant, first.run_id).execution_version(datetime.now(timezone.utc)) == 3
    second = runs.begin(replace(run, run_id=str(uuid4())))
    assert second is not None and second.run_id == first.run_id and second.generation == 2
    runs.fail(first, "LATE_OLD_FAILURE")
    with pytest.raises(RunStoreUnavailable):
        runs.complete(response_for(run, first.run_id), lease=first, tenant_id=tenant, user_id="7")
    assert detail(activity, tenant, first.run_id).execution_version(datetime.now(timezone.utc)) == 4
    runs.complete(response_for(run, second.run_id), lease=second, tenant_id=tenant, user_id="7")
    runs.fail(second, "LATE_FAILURE_AFTER_COMPLETION")
    latest = detail(activity, tenant, first.run_id)
    assert latest.run_state == "COMPLETED"
    assert latest.execution_version(datetime.now(timezone.utc)) == 5
    assert latest.created_at == first_snapshot.created_at
    assert runs.begin(run) is None
    rows, has_more = activity.page(tenant_id=tenant, user_id="7", filters=ActivityFilters(),
                                   snapshot_at=datetime.now(timezone.utc), now=datetime.now(timezone.utc), after=None, limit=100)
    assert [row.run_id for row in rows] == [UUID(first.run_id)] and not has_more


def test_lease_expiration_is_unknown_not_forever_running(source):
    runs, activity, tenant, database_url = source
    run = start(tenant=tenant)
    lease = runs.begin(run)
    assert lease is not None
    now = datetime.now(timezone.utc)
    if database_url:
        with connect(database_url) as connection:
            connection.execute("UPDATE ai_agent_runs SET lease_expires_at = %s WHERE run_id = %s", (now - timedelta(seconds=1), UUID(lease.run_id)))
    else:
        runs._clock = lambda: now + timedelta(minutes=3)
        now += timedelta(minutes=3)
    row = detail(activity, tenant, lease.run_id)
    assert row.run_state == "RUNNING"  # Reads never change the source ledger.
    assert row.activity_state(now) == "UNKNOWN"
    assert row.execution_version(now) == 2  # Clock expiry must not mint a source version.
    counts = activity.counts(tenant_id=tenant, user_id="7", filters=ActivityFilters(), now=now)
    assert counts == {"UNKNOWN": 1}
    with pytest.raises(RunStoreUnavailable):
        runs.complete(response_for(run, lease.run_id), lease=lease, tenant_id=tenant, user_id="7")
    retry = runs.begin(replace(run, run_id=str(uuid4())))
    assert retry is not None and retry.generation == 2
    assert detail(activity, tenant, lease.run_id).activity_state(now) == "RUNNING"


def test_full_summary_is_not_limited_by_recent_page_and_owner_isolation(source):
    runs, activity, tenant, database_url = source
    old = start(tenant=tenant)
    runs.begin(old)
    for _ in range(103):
        run = start(tenant=tenant)
        lease = runs.begin(run)
        assert lease is not None
        runs.complete(response_for(run, lease.run_id), lease=lease, tenant_id=tenant, user_id="7")
    other_user = start(tenant=tenant, user="other-user")
    runs.begin(other_user)
    now = datetime.now(timezone.utc)
    rows, more = activity.page(tenant_id=tenant, user_id="7", filters=ActivityFilters(),
                               snapshot_at=now, now=now, after=None, limit=100)
    assert len(rows) == 100 and more and all(str(row.run_id) != old.run_id for row in rows)
    assert activity.counts(tenant_id=tenant, user_id="7", filters=ActivityFilters(), now=now) == {"RUNNING": 1, "COMPLETED": 103}
    assert activity.detail(tenant_id=tenant, user_id="other-user", run_id=UUID(old.run_id)) is None
    assert activity.detail(tenant_id="1", user_id="7", run_id=UUID(old.run_id)) is None
    tail, more = activity.page(tenant_id=tenant, user_id="7", filters=ActivityFilters(),
                               snapshot_at=now, now=now, after=(rows[-1].created_at, rows[-1].run_id), limit=100)
    assert len(tail) == 4 and not more and str(tail[-1].run_id) == old.run_id
    assert not set(row.run_id for row in rows) & set(row.run_id for row in tail)
    for filters in (ActivityFilters(actor="PERSON"), ActivityFilters(source="WORKSPACE"), ActivityFilters(object_type="WORK_ITEM")):
        assert activity.counts(tenant_id=tenant, user_id="7", filters=filters, now=now) == {}
    assert activity.counts(tenant_id=tenant, user_id="7", filters=ActivityFilters(query=old.run_id), now=now) == {"RUNNING": 1}


def test_source_provider_tracks_executor_on_memory_database_transition(source, monkeypatch):
    runs, activity, tenant, database_url = source
    run = start(tenant=tenant)
    runs.begin(run)
    monkeypatch.setattr(run_store_module, "_STORE", runs)
    # A configuration edit alone must not fork the execution ledger and read source.
    monkeypatch.setenv("DWP_AGENT_DATABASE_URL", "" if database_url else "postgresql://unused/not_a_source")
    selected = get_agent_activity_store()
    assert type(selected) is type(activity)
    assert selected.detail(tenant_id=tenant, user_id="7", run_id=UUID(run.run_id)) is not None
    legacy_rows = get_user_run_store().list(tenant_id=tenant, user_id="7", limit=50, run_state=None)
    assert [row.run_id for row in legacy_rows] == [UUID(run.run_id)]
    assert get_user_run_store().get(
        tenant_id=tenant, user_id="7", run_id=UUID(run.run_id)
    ) is not None
    assert get_user_run_store().get(
        tenant_id=tenant, user_id="other-user", run_id=UUID(run.run_id)
    ) is None
    assert get_user_run_store().get(
        tenant_id="1", user_id="7", run_id=UUID(run.run_id)
    ) is None
    # Simulate the controlled executor replacement used by a process restart.
    replacement = InMemoryRunStore()
    monkeypatch.setattr(run_store_module, "_STORE", replacement)
    selected = get_agent_activity_store()
    assert isinstance(selected, InMemoryAgentActivityStore)
    assert selected.detail(tenant_id=tenant, user_id="7", run_id=UUID(run.run_id)) is None
    assert selected.counts(tenant_id=tenant, user_id="7", filters=ActivityFilters(), now=datetime.now(timezone.utc)) == {}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", "test-activity-service-token")
    monkeypatch.delenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V4_ENABLED", raising=False)
    monkeypatch.delenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V5_ENABLED", raising=False)
    runs = InMemoryRunStore()
    source = InMemoryAgentActivityStore(runs)
    monkeypatch.setattr(api, "get_agent_activity_store", lambda: source)
    return runs


def request(path: str, *, headers: dict | None = None):
    async def fetch():
        identity = {"X-DWP-Service-Token": "test-activity-service-token", "X-DWP-Tenant-ID": "42",
                    "X-DWP-User-ID": "7", "X-DWP-Permissions": "APP.ACTIVITY:VIEW,APP.ASK:VIEW",
                    "X-DWP-Identity-Plane": "TENANT", "X-Correlation-ID": "activity-test"}
        identity.update(headers or {})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path, headers=identity)
    return asyncio.run(fetch())


def test_api_pagination_signed_resume_cursor_watermark_and_privacy(configured):
    fixed = datetime.now(timezone.utc) - timedelta(seconds=1)
    configured._clock = lambda: fixed
    run_ids = sorted([str(uuid4()) for _ in range(3)], reverse=True)
    for run_id in run_ids:
        configured.begin(start(run_id=run_id))
    first = request("/v1/activity/events?limit=2")
    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "private, no-store, max-age=0"
    page = first.json()["data"]
    assert [event["id"] for event in page["events"]] == run_ids[:2]
    assert page["hasMore"] and page["startCursor"] and page["nextCursor"]
    assert all(event["resumeCursor"] and event["auditStatus"] == "NOT_LINKED" and event["auditId"] is None for event in page["events"])
    assert all(event["workStatus"] is None for event in page["events"])
    assert page["coverage"]["semantics"] == "CURRENT_EXECUTION_SNAPSHOTS"
    assert "private-correlation" not in first.text and "ciphertext" not in first.text and "queryHash" not in first.text
    configured._clock = lambda: datetime.now(timezone.utc)
    new_run = start()
    configured.begin(new_run)
    replay = request(f"/v1/activity/events?limit=2&cursor={page['startCursor']}").json()["data"]
    assert [event["id"] for event in replay["events"]] == run_ids[:2]
    tail = request(f"/v1/activity/events?cursor={page['nextCursor']}").json()["data"]
    assert [event["id"] for event in tail["events"]] == run_ids[2:] and not tail["hasMore"]
    one_consumed = request(f"/v1/activity/events?cursor={page['events'][0]['resumeCursor']}").json()["data"]
    assert [event["id"] for event in one_consumed["events"]] == run_ids[1:]
    invalid_cursor = request(
        f"/v1/activity/events?state=FAILED&cursor={page['nextCursor']}"
    )
    assert invalid_cursor.status_code == 400
    assert invalid_cursor.headers["Cache-Control"] == "private, no-store, max-age=0"
    assert request(f"/v1/activity/events?cursor={page['nextCursor']}", headers={"X-DWP-User-ID": "8"}).status_code == 400
    assert request(f"/v1/activity/events?cursor={page['nextCursor'][:-1]}!").status_code == 400
    assert request(f"/v1/activity/events/{run_ids[0]}").status_code == 200
    unavailable_detail = request(
        f"/v1/activity/events/{run_ids[0]}", headers={"X-DWP-User-ID": "8"}
    )
    assert unavailable_detail.status_code == 404
    assert unavailable_detail.headers["Cache-Control"] == "private, no-store, max-age=0"


@pytest.mark.parametrize("headers,status", [
    ({"X-DWP-Permissions": "APP.ASK:VIEW"}, 403),
    ({"X-DWP-Permissions": "APP.ACTIVITY:VIEW"}, 403),
    ({"X-DWP-Identity-Plane": "PROVIDER"}, 403),
    ({"X-DWP-Roles": "PROVIDER_ADMIN"}, 403),
    ({"X-DWP-Support-Session-ID": "support"}, 403),
    ({"X-DWP-Active-Access-Mode": "SUPPORT"}, 403),
    ({"X-DWP-Service-Token": "invalid"}, 401),
    ({"X-DWP-Rollout-State": "110"}, 503),
    ({"X-DWP-Rollout-State": "111"}, 503),
])
def test_all_source_endpoints_recheck_identity_and_both_permissions(configured, headers, status):
    for path in ("/v1/activity/events", f"/v1/activity/events/{uuid4()}", "/v1/activity/executions/summary"):
        assert request(path, headers=headers).status_code == status


def test_activity_projects_measured_progress_and_central_audit_link(configured):
    audit_id = str(uuid4())
    run = replace(start(), audit_id=audit_id)
    lease = configured.begin(run)
    assert lease is not None

    event = request(f"/v1/activity/events/{lease.run_id}").json()["data"]

    assert event["progress"] == 0
    assert event["attempt"] == 1
    assert event["auditId"] == audit_id
    assert event["auditRecordId"]
    assert event["auditStatus"] == "PENDING"
    assert event["dataProvenance"] == "LIVE"
    assert "private-correlation" not in str(event)


def test_signed_identity_required_and_v5_default_fail_closed(configured, monkeypatch):
    monkeypatch.setenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", "test-identity-secret")
    assert request("/v1/activity/events").status_code == 401
    monkeypatch.delenv("DWP_AGENT_IDENTITY_SIGNING_SECRET")
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V5_ENABLED", "true")
    assert request("/v1/activity/events").status_code == 503


def test_api_unavailable_not_empty_and_validates_time_filters(configured, monkeypatch):
    def unavailable():
        raise ActivityStoreUnavailable("Source unavailable.")
    monkeypatch.setattr(api, "get_agent_activity_store", unavailable)
    unavailable_events = request("/v1/activity/events")
    assert unavailable_events.status_code == 503
    assert unavailable_events.headers["Cache-Control"] == "private, no-store, max-age=0"
    assert request("/v1/activity/executions/summary").status_code == 503
    assert request("/v1/activity/events?from=2026-09-04T00:00:00").status_code == 422
    assert request("/v1/activity/events?limit=101").status_code == 422


def test_factory_and_legacy_user_list_read_same_memory_execution_source(configured, monkeypatch):
    monkeypatch.setattr(run_store_module, "_STORE", configured)
    # A changed env does not silently switch away from the initialized executor.
    monkeypatch.setenv("DWP_AGENT_DATABASE_URL", "postgresql://not-used/other")
    run = start()
    configured.begin(run)
    assert isinstance(get_agent_activity_store(), InMemoryAgentActivityStore)
    legacy = get_user_run_store()
    assert isinstance(legacy, InMemoryUserRunStore)
    assert legacy.list(tenant_id="42", user_id="7", limit=10, run_state=None)[0].run_id == UUID(run.run_id)
    assert legacy.list(tenant_id="42", user_id="8", limit=10, run_state=None) == []
    assert legacy.get(tenant_id="42", user_id="7", run_id=UUID(run.run_id)) is not None
    assert legacy.get(tenant_id="42", user_id="8", run_id=UUID(run.run_id)) is None


def test_cursor_requires_real_secret_and_expires(configured, monkeypatch):
    now = datetime.now(timezone.utc)
    cursor = encode_cursor(tenant_id="42", user_id="7", filters=ActivityFilters(), snapshot_at=now, after=None)
    with pytest.raises(InvalidActivityCursor):
        decode_cursor(cursor, tenant_id="42", user_id="7", filters=ActivityFilters(), now=now + timedelta(hours=2))
    monkeypatch.delenv("DWP_AGENT_SERVICE_TOKEN")
    with pytest.raises(ActivityStoreUnavailable):
        encode_cursor(tenant_id="42", user_id="7", filters=ActivityFilters(), snapshot_at=now, after=None)
