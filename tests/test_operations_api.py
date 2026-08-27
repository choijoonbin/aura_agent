import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import dwp_agent.operations_api as operations_api_module
from dwp_agent.operations_contracts import (
    BootstrapRetentionPolicyRequest,
    DwaionOperationsOverview,
    RetentionPolicy,
    UpdateRetentionPolicyRequest,
)
from dwp_agent.main import app


SERVICE_TOKEN = "test-gateway-service-token"


@pytest.fixture(autouse=True)
def configured_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)


class FakeOperationsStore:
    def __init__(self) -> None:
        self.policy = RetentionPolicy(
            retention_days=90,
            legal_hold=False,
            policy_version=1,
            updated_at=datetime.now(timezone.utc),
        )

    def overview(self, *, tenant_id: str, period_days: int = 30):
        assert tenant_id == "1"
        return DwaionOperationsOverview(
            period_days=period_days,
            run_count=12,
            completed_run_count=10,
            failed_run_count=2,
            allowed_run_count=9,
            handed_off_run_count=2,
            denied_run_count=1,
            grounded_answer_count=8,
            abstained_answer_count=1,
            configuration_required_count=1,
            average_latency_ms=320,
            total_tokens=2400,
            active_user_count=4,
            conversation_count=6,
            feedback_up_count=3,
            feedback_down_count=1,
            retention=self.policy,
            generated_at=datetime.now(timezone.utc),
        )

    def retention_policy(self, *, tenant_id: str):
        assert tenant_id == "1"
        return self.policy

    def bootstrap_retention_policy(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        request: BootstrapRetentionPolicyRequest,
    ):
        assert (tenant_id, actor_user_id, correlation_id) == ("1", "7", "corr-1")
        self.policy = RetentionPolicy(
            retention_days=request.retention_days,
            legal_hold=request.legal_hold,
            policy_version=1,
            updated_at=datetime.now(timezone.utc),
        )
        return self.policy

    def update_retention_policy(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        request: UpdateRetentionPolicyRequest,
    ):
        assert (tenant_id, actor_user_id, correlation_id) == ("1", "7", "corr-1")
        self.policy = self.policy.model_copy(
            update={
                "retention_days": request.retention_days or self.policy.retention_days,
                "legal_hold": (
                    self.policy.legal_hold
                    if request.legal_hold is None
                    else request.legal_hold
                ),
                "policy_version": self.policy.policy_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return self.policy


async def request(
    method: str,
    path: str,
    *,
    permissions: str,
    json: dict | None = None,
) -> httpx.Response:
    headers = {
        "X-DWP-Service-Token": SERVICE_TOKEN,
        "X-DWP-User-ID": "7",
        "X-DWP-Tenant-ID": "1",
        "X-Correlation-ID": "corr-1",
        "X-DWP-Permissions": permissions,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, json=json)


def test_operations_overview_requires_dedicated_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeOperationsStore()
    monkeypatch.setattr(operations_api_module, "get_operations_store", lambda: store)

    denied = asyncio.run(
        request("GET", "/v1/admin/overview", permissions="APP.ASK:VIEW")
    )
    allowed = asyncio.run(
        request(
            "GET",
            "/v1/admin/overview",
            permissions="ADMIN.DWAION_OPERATIONS:VIEW",
        )
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["data"]["runCount"] == 12
    assert "messages" not in allowed.text.lower()


def test_retention_update_uses_versioned_policy_and_manage_for_legal_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeOperationsStore()
    monkeypatch.setattr(operations_api_module, "get_operations_store", lambda: store)

    updated = asyncio.run(
        request(
            "PATCH",
            "/v1/admin/retention",
            permissions="ADMIN.DWAION_RETENTION:UPDATE",
            json={
                "retentionDays": 180,
                "expectedVersion": 1,
                "changeReason": "Align retention with the company record schedule.",
            },
        )
    )
    denied_hold = asyncio.run(
        request(
            "PATCH",
            "/v1/admin/retention",
            permissions="ADMIN.DWAION_RETENTION:UPDATE",
            json={
                "legalHold": True,
                "expectedVersion": 2,
                "changeReason": "Preserve records for the active legal investigation.",
            },
        )
    )
    allowed_hold = asyncio.run(
        request(
            "PATCH",
            "/v1/admin/retention",
            permissions="ADMIN.DWAION_RETENTION:MANAGE",
            json={
                "legalHold": True,
                "expectedVersion": 2,
                "changeReason": "Preserve records for the active legal investigation.",
            },
        )
    )

    assert updated.status_code == 200
    assert updated.json()["data"]["retentionDays"] == 180
    assert denied_hold.status_code == 403
    assert allowed_hold.status_code == 200
    assert allowed_hold.json()["data"]["legalHold"] is True


def test_retention_bootstrap_requires_manage_and_explicit_command_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeOperationsStore()
    monkeypatch.setattr(operations_api_module, "get_operations_store", lambda: store)
    command = {
        "idempotencyKey": "00000000-0000-4000-8000-000000000001",
        "expectedExistingCount": 0,
        "retentionDays": 365,
        "legalHold": False,
        "changeReason": "Initialize the tenant records retention policy.",
    }

    denied = asyncio.run(
        request(
            "POST",
            "/v1/admin/retention/bootstrap",
            permissions="ADMIN.DWAION_RETENTION:UPDATE",
            json=command,
        )
    )
    allowed = asyncio.run(
        request(
            "POST",
            "/v1/admin/retention/bootstrap",
            permissions="ADMIN.DWAION_RETENTION:MANAGE",
            json=command,
        )
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["data"]["retentionDays"] == 365
