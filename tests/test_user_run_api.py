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


SERVICE_TOKEN = "test-gateway-service-token"
RUN_ID = UUID("00000000-0000-4000-8000-000000000101")


class FakeUserRunStore:
    def __init__(self) -> None:
        self.received: tuple[str, str, int, AgentRunState | None] | None = None

    def list(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
        run_state: AgentRunState | None,
    ) -> list[UserAgentRunSummary]:
        self.received = (tenant_id, user_id, limit, run_state)
        return [
            UserAgentRunSummary(
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
        ]


@pytest.fixture(autouse=True)
def configured_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.delenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", raising=False)


async def request(*, permissions: str = "APP.ASK:VIEW") -> httpx.Response:
    headers = {
        "X-DWP-Service-Token": SERVICE_TOKEN,
        "X-DWP-Tenant-ID": "42",
        "X-DWP-User-ID": "user-7",
        "X-DWP-Permissions": permissions,
        "X-Correlation-ID": "run-correlation",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(
            "/v1/runs?state=COMPLETED&limit=250", headers=headers
        )


def test_user_activity_is_scoped_to_verified_identity_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeUserRunStore()
    monkeypatch.setattr(user_run_api_module, "get_user_run_store", lambda: store)

    response = asyncio.run(request())

    assert response.status_code == 200
    assert store.received == ("42", "user-7", 100, AgentRunState.COMPLETED)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["data"][0]["runId"] == str(RUN_ID)
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
