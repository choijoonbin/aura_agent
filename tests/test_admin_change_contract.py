import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from main import app


SERVICE_TOKEN = "test-gateway-service-token"


@pytest.fixture(autouse=True)
def configured_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("DWP_AGENT_REGISTRY_MODE", "optional")
    monkeypatch.delenv("SERVICE_PLATFORM_URL", raising=False)
    monkeypatch.delenv("DWP_PLATFORM_RUNTIME_SERVICE_TOKEN", raising=False)


async def post_preview(payload: dict, *, tenant_id: str = "tenant-1"):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/v1/plans/preview",
            json=payload,
            headers={
                "X-DWP-Service-Token": SERVICE_TOKEN,
                "X-DWP-User-ID": "admin-7",
                "X-DWP-Tenant-ID": tenant_id,
                "X-DWP-Roles": "TENANT_ADMIN",
                "X-Correlation-ID": "correlation-admin-change",
            },
        )


def admin_change_payload(expected_version: int = 4) -> dict:
    return {
        "requestId": "request-admin-1042",
        "intent": "Assign the help desk role to the operations group",
        "action": "tenant role assignment",
        "target": "access/group-role-assignment",
        "sourceReferences": ["access-policy-v3"],
        "adminChange": {
            "commandKey": "ACCESS.GROUP_ROLE.ASSIGN",
            "targetType": "GROUP",
            "targetId": "operations",
            "expectedVersion": expected_version,
            "parameters": {"roleCode": "HELP_DESK", "effectiveFrom": "2026-08-10"},
            "justification": "Operations staff require governed help desk access.",
        },
    }


def test_admin_change_is_high_risk_preview_only() -> None:
    response = asyncio.run(post_preview(admin_change_payload()))

    assert response.status_code == 200
    plan = response.json()["data"]
    assert plan["riskTier"] == "L3"
    assert plan["approvalRequired"] is True
    assert plan["mutationAllowed"] is False
    assert "ACCESS.GROUP_ROLE.ASSIGN" in plan["summary"]
    assert [step["tool"] for step in plan["steps"]] == [
        "policy.check",
        "admin.command.validate",
        "tool.preview",
        "workflow.human-approval",
    ]


def test_admin_change_hash_binds_tenant_and_expected_version() -> None:
    current = asyncio.run(post_preview(admin_change_payload(expected_version=4)))
    changed_version = asyncio.run(post_preview(admin_change_payload(expected_version=5)))
    changed_tenant = asyncio.run(
        post_preview(admin_change_payload(expected_version=4), tenant_id="tenant-2")
    )

    hashes = {
        current.json()["data"]["planHash"],
        changed_version.json()["data"]["planHash"],
        changed_tenant.json()["data"]["planHash"],
    }
    assert len(hashes) == 3


@pytest.mark.parametrize(
    "admin_change",
    [
        {
            "commandKey": "assign role",
            "targetType": "GROUP",
            "targetId": "operations",
            "expectedVersion": 1,
            "justification": "Invalid free-form command key.",
        },
        {
            "commandKey": "ACCESS.GROUP_ROLE.ASSIGN",
            "targetType": "GROUP",
            "targetId": "operations",
            "expectedVersion": 1,
            "justification": "Unknown fields must fail closed.",
            "rawSql": "update roles set code = 'ADMIN'",
        },
    ],
)
def test_admin_change_rejects_untyped_or_unknown_commands(admin_change: dict) -> None:
    response = asyncio.run(
        post_preview({**admin_change_payload(), "adminChange": admin_change})
    )

    assert response.status_code == 422
