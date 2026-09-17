from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import dwp_agent.user_run_api as user_run_api_module
from dwp_agent.contracts import AskState, PolicyOutcome, RiskTier
from dwp_agent.main import app
from dwp_agent.user_run_contracts import AgentRunState, UserAgentRunSummary
from dwp_agent.user_run_store import UserRunStoreUnavailable


SERVICE_TOKEN = "test-gateway-service-token"
RUN_ID = UUID("00000000-0000-4000-8000-000000000101")


class FakeUserRunStore:
    def __init__(self) -> None:
        self.received: tuple[str, str, int, AgentRunState | None] | None = None
        self.received_page: tuple[
            str,
            str,
            int,
            AgentRunState | None,
            datetime | None,
            datetime | None,
            datetime,
            tuple[datetime, UUID] | None,
        ] | None = None
        self.received_get: tuple[str, str, UUID] | None = None
        self.missing = False
        self.has_more = False

    def list(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
        run_state: AgentRunState | None,
    ) -> list[UserAgentRunSummary]:
        self.received = (tenant_id, user_id, limit, run_state)
        return [self._run()]

    def page(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
        run_state: AgentRunState | None,
        from_at: datetime | None,
        to_at: datetime | None,
        snapshot_at: datetime,
        after: tuple[datetime, UUID] | None,
    ) -> tuple[list[UserAgentRunSummary], bool]:
        self.received_page = (
            tenant_id,
            user_id,
            limit,
            run_state,
            from_at,
            to_at,
            snapshot_at,
            after,
        )
        return [self._run()], self.has_more

    def get(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: UUID,
    ) -> UserAgentRunSummary | None:
        self.received_get = (tenant_id, user_id, run_id)
        return None if self.missing else self._run()

    def _run(self) -> UserAgentRunSummary:
        return UserAgentRunSummary(
            run_id=RUN_ID,
            agent_key="DWP_ASSISTANT",
            agent_revision=2,
            run_state=AgentRunState.COMPLETED,
            answer_state=AskState.COMPLETED,
            risk_tier=RiskTier.L0,
            policy_outcome=PolicyOutcome.ALLOW,
            status_code="READ_ONLY_GROUNDED_ANSWER",
            source_count=3,
            latency_ms=240,
            created_at=datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc),
            completed_at=datetime(2026, 8, 27, 1, 0, 1, tzinfo=timezone.utc),
        )


@pytest.fixture(autouse=True)
def configured_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.delenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", raising=False)


async def request(
    *,
    permissions: str = "APP.ASK:VIEW",
    path: str = "/v1/runs?state=COMPLETED&limit=250",
    user_id: str = "user-7",
) -> httpx.Response:
    headers = {
        "X-DWP-Service-Token": SERVICE_TOKEN,
        "X-DWP-Tenant-ID": "42",
        "X-DWP-User-ID": user_id,
        "X-DWP-Permissions": permissions,
        "X-Correlation-ID": "run-correlation",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path, headers=headers)


def test_user_activity_is_scoped_to_verified_identity_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeUserRunStore()
    monkeypatch.setattr(user_run_api_module, "get_user_run_store", lambda: store)

    response = asyncio.run(request())

    assert response.status_code == 200
    assert store.received_page is not None
    assert store.received_page[:4] == ("42", "user-7", 100, AgentRunState.COMPLETED)
    assert response.headers["Cache-Control"] == "private, no-store, max-age=0"
    assert response.json()["data"][0]["runId"] == str(RUN_ID)
    assert response.json()["data"][0]["activityTitle"] == "DWAI·ON Agent execution"
    assert response.json()["data"][0]["attempt"] == 1
    assert response.json()["data"][0]["measurementStatus"] == "NOT_AVAILABLE"
    assert response.json()["data"][0]["auditEvidence"]["status"] == "NOT_AVAILABLE"
    assert response.json()["hasMore"] is False
    assert response.json()["nextCursor"] is None
    assert response.json()["snapshotAt"] is not None
    assert "query" not in response.text.lower()
    assert "ciphertext" not in response.text.lower()


def test_user_activity_requires_dwaion_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        user_run_api_module, "get_user_run_store", lambda: FakeUserRunStore()
    )

    response = asyncio.run(request(permissions="APP.WORK:VIEW"))

    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "private, no-store, max-age=0"


def test_user_run_detail_is_independent_and_owner_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeUserRunStore()
    monkeypatch.setattr(user_run_api_module, "get_user_run_store", lambda: store)

    response = asyncio.run(request(path=f"/v1/runs/{RUN_ID}"))

    assert response.status_code == 200
    assert store.received_get == ("42", "user-7", RUN_ID)
    assert response.headers["Cache-Control"] == "private, no-store, max-age=0"
    assert response.json()["data"]["runId"] == str(RUN_ID)
    assert "query" not in response.text.lower()
    assert "ciphertext" not in response.text.lower()


def test_user_run_detail_hides_missing_or_foreign_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeUserRunStore()
    store.missing = True
    monkeypatch.setattr(user_run_api_module, "get_user_run_store", lambda: store)

    response = asyncio.run(request(path=f"/v1/runs/{RUN_ID}"))

    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "private, no-store, max-age=0"
    assert store.received_get == ("42", "user-7", RUN_ID)


def test_user_run_detail_requires_dwaion_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeUserRunStore()
    monkeypatch.setattr(user_run_api_module, "get_user_run_store", lambda: store)

    response = asyncio.run(request(
        permissions="APP.ACTIVITY:VIEW",
        path=f"/v1/runs/{RUN_ID}",
    ))

    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "private, no-store, max-age=0"
    assert store.received_get is None


def test_user_run_store_unavailable_responses_are_private_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableUserRunStore(FakeUserRunStore):
        def page(
            self,
            *,
            tenant_id: str,
            user_id: str,
            limit: int,
            run_state: AgentRunState | None,
            from_at: datetime | None,
            to_at: datetime | None,
            snapshot_at: datetime,
            after: tuple[datetime, UUID] | None,
        ) -> tuple[list[UserAgentRunSummary], bool]:
            raise UserRunStoreUnavailable("Run source unavailable.")

        def get(
            self,
            *,
            tenant_id: str,
            user_id: str,
            run_id: UUID,
        ) -> UserAgentRunSummary | None:
            raise UserRunStoreUnavailable("Run source unavailable.")

    monkeypatch.setattr(
        user_run_api_module,
        "get_user_run_store",
        lambda: UnavailableUserRunStore(),
    )

    for path in ("/v1/runs", f"/v1/runs/{RUN_ID}"):
        response = asyncio.run(request(path=path))
        assert response.status_code == 503
        assert response.headers["Cache-Control"] == "private, no-store, max-age=0"


def test_user_run_validation_and_unknown_route_are_private_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        user_run_api_module, "get_user_run_store", lambda: FakeUserRunStore()
    )

    invalid_state = asyncio.run(request(path="/v1/runs?state=INVALID"))
    unknown_route = asyncio.run(request(path="/v1/runs/not/a/route"))

    assert invalid_state.status_code == 422
    assert invalid_state.headers["Cache-Control"] == "private, no-store, max-age=0"
    assert unknown_route.status_code == 404
    assert unknown_route.headers["Cache-Control"] == "private, no-store, max-age=0"


def test_user_activity_applies_time_range_and_owner_bound_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeUserRunStore()
    store.has_more = True
    monkeypatch.setattr(user_run_api_module, "get_user_run_store", lambda: store)
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 9, 1, tzinfo=timezone.utc)
    path = (
        "/v1/runs?limit=25&from=2026-08-01T09%3A00%3A00%2B09%3A00"
        "&to=2026-09-01T09%3A00%3A00%2B09%3A00"
    )

    first = asyncio.run(request(path=path))

    assert first.status_code == 200
    assert store.received_page is not None
    assert store.received_page[:6] == ("42", "user-7", 25, None, start, end)
    cursor = first.json()["nextCursor"]
    assert first.json()["hasMore"] is True
    assert isinstance(cursor, str)

    store.has_more = False
    second = asyncio.run(request(path=f"{path}&cursor={cursor}"))

    assert second.status_code == 200
    assert store.received_page is not None
    assert store.received_page[7] == (
        datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc),
        RUN_ID,
    )
    assert second.json()["hasMore"] is False
    assert second.json()["nextCursor"] is None

    wrong_scope = asyncio.run(
        request(path=f"/v1/runs?limit=25&from=2026-08-02T00%3A00%3A00Z&cursor={cursor}")
    )
    assert wrong_scope.status_code == 400
    wrong_state = asyncio.run(request(path=f"{path}&state=COMPLETED&cursor={cursor}"))
    assert wrong_state.status_code == 400
    wrong_owner = asyncio.run(request(path=f"{path}&cursor={cursor}", user_id="user-8"))
    assert wrong_owner.status_code == 400


@pytest.mark.parametrize(
    "path",
    (
        "/v1/runs?from=2026-08-01T00:00:00",
        "/v1/runs?from=2026-09-01T00:00:00Z&to=2026-08-01T00:00:00Z",
    ),
)
def test_user_activity_rejects_unsafe_page_ranges(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    store = FakeUserRunStore()
    monkeypatch.setattr(user_run_api_module, "get_user_run_store", lambda: store)

    response = asyncio.run(request(path=path))

    assert response.status_code == 422
    assert store.received_page is None
