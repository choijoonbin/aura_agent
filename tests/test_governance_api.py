import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import dwp_agent.governance_api as governance_api_module
from dwp_agent.contracts import CitationSourceType
from dwp_agent.governance_contracts import (
    ConnectionState,
    DataClassification,
    DataSourcePolicy,
    GovernanceAuditEvent,
    GovernanceAuditPage,
    SourceAccessMode,
    UpdateDataSourcePolicyRequest,
)
from dwp_agent.main import app


SERVICE_TOKEN = "test-gateway-service-token"


@pytest.fixture(autouse=True)
def configured_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)


class FakeGovernanceStore:
    def __init__(self) -> None:
        self.policy = DataSourcePolicy(
            source_key=CitationSourceType.WORK_ITEM,
            display_name="Work",
            description="Verified work items",
            provider_type="DWP_PLATFORM",
            classification=DataClassification.INTERNAL,
            access_mode=SourceAccessMode.SOURCE_PERMISSIONS,
            enabled=True,
            connection_state=ConnectionState.CONNECTED,
            policy_version=1,
            updated_at=datetime.now(timezone.utc),
        )

    def source_policies(self, *, tenant_id: str, actor_user_id: str):
        assert (tenant_id, actor_user_id) == ("1", "7")
        return [self.policy]

    def update_source_policy(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        source_key: CitationSourceType,
        request: UpdateDataSourcePolicyRequest,
    ):
        assert (tenant_id, actor_user_id, correlation_id) == ("1", "7", "corr-1")
        assert source_key == CitationSourceType.WORK_ITEM
        self.policy = self.policy.model_copy(
            update={
                "classification": request.classification,
                "access_mode": request.access_mode,
                "enabled": request.enabled,
                "policy_version": 2,
            }
        )
        return self.policy

    def audit_events(
        self,
        *,
        tenant_id: str,
        category: str | None,
        query: str | None,
        page: int,
        size: int,
    ):
        assert tenant_id == "1"
        event = GovernanceAuditEvent(
            event_id=uuid4(),
            category=category or "SOURCE",
            event_type="source-policy.updated",
            target_type="DATA_SOURCE",
            target_key="WORK_ITEM",
            actor_user_id="7",
            correlation_id="corr-1",
            change_reason="Align source handling with tenant policy.",
            created_at=datetime.now(timezone.utc),
        )
        return GovernanceAuditPage(
            content=[event], page=page, size=size, total_elements=1, total_pages=1
        )

    def audit_csv(self, *, tenant_id: str, category: str | None, query: str | None):
        assert tenant_id == "1"
        return "eventId,category\r\n1,SOURCE\r\n", False


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


def test_source_governance_requires_granular_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeGovernanceStore()
    monkeypatch.setattr(governance_api_module, "get_governance_store", lambda: store)

    legacy_scope = asyncio.run(
        request("GET", "/v1/admin/sources", permissions="ADMIN.DWAION:VIEW")
    )
    allowed = asyncio.run(
        request("GET", "/v1/admin/sources", permissions="ADMIN.DWAION_SOURCES:VIEW")
    )
    updated = asyncio.run(
        request(
            "PATCH",
            "/v1/admin/sources/WORK_ITEM",
            permissions="ADMIN.DWAION_SOURCES:UPDATE",
            json={
                "enabled": True,
                "accessMode": "SOURCE_PERMISSIONS",
                "classification": "CONFIDENTIAL",
                "expectedVersion": 1,
                "changeReason": "Align source handling with tenant policy.",
            },
        )
    )

    assert legacy_scope.status_code == 403
    assert allowed.status_code == 200
    assert updated.status_code == 200
    assert updated.json()["data"]["policyVersion"] == 2


def test_audit_export_requires_export_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeGovernanceStore()
    monkeypatch.setattr(governance_api_module, "get_governance_store", lambda: store)

    denied = asyncio.run(
        request("GET", "/v1/admin/audit/export", permissions="ADMIN.DWAION_AUDIT:VIEW")
    )
    allowed = asyncio.run(
        request("GET", "/v1/admin/audit/export", permissions="ADMIN.DWAION_AUDIT:EXPORT")
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.headers["x-dwp-export-limit"] == "10000"
    assert allowed.headers["x-dwp-export-truncated"] == "false"
    assert allowed.text.startswith("eventId,category")
