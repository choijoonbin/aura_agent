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
            "targetId": "84",
            "expectedVersion": expected_version,
            "parameters": {
                "groupId": 84,
                "roleId": 12,
                "assignmentType": "ACTIVE",
                "scopeType": "TENANT",
                "validFrom": "2026-08-10T00:00:00Z",
            },
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
    assert plan["adminCommand"] == {
        "commandKey": "ACCESS.GROUP_ROLE.ASSIGN",
        "catalogRevision": 1,
        "targetService": "auth",
        "httpMethod": "POST",
        "endpointTemplate": "/auth/admin/access/governance/group-role-assignments",
        "requiredPermission": "access:group-role:assign",
    }
    assert [step["tool"] for step in plan["steps"]] == [
        "policy.check",
        "admin.command.resolve",
        "auth.preview",
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
            "commandKey": "ACCESS.UNREGISTERED.COMMAND",
            "targetType": "GROUP",
            "targetId": "operations",
            "expectedVersion": 1,
            "justification": "Unregistered commands must fail closed.",
        },
        {
            "commandKey": "ACCESS.GROUP_ROLE.ASSIGN",
            "targetType": "GROUP",
            "targetId": "operations",
            "expectedVersion": 1,
            "parameters": {
                "groupId": 84,
                "roleId": 12,
                "assignmentType": "ACTIVE",
                "scopeType": "TENANT",
                "rawSql": "update roles set code = 'ADMIN'",
            },
            "justification": "Unknown fields must fail closed.",
        },
    ],
)
def test_admin_change_rejects_untyped_or_unknown_commands(admin_change: dict) -> None:
    response = asyncio.run(
        post_preview({**admin_change_payload(), "adminChange": admin_change})
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("command_key", "target_type", "parameters", "target_service"),
    [
        (
            "ACCESS.GROUP_ROLE.ASSIGN",
            "GROUP",
            {
                "groupId": 10,
                "roleId": 20,
                "assignmentType": "ELIGIBLE",
                "scopeType": "RESOURCE",
                "scopeRef": "app:people",
            },
            "auth",
        ),
        ("ACCESS.GROUP_ROLE.REVOKE", "GROUP_ROLE_ASSIGNMENT", {}, "auth"),
        (
            "ACCESS.ROLE.PERMISSION.REPLACE",
            "ROLE",
            {
                "permissions": [
                    {
                        "resourceId": 12,
                        "permissionCode": "READ",
                        "effect": "ALLOW",
                    }
                ]
            },
            "auth",
        ),
        ("NAVIGATION.ITEM.PUBLISH", "NAVIGATION_ITEM", {}, "platform"),
        (
            "NAVIGATION.ORDER.UPDATE",
            "NAVIGATION_TREE",
            {
                "items": [
                    {
                        "navigationItemId": 7,
                        "parentNavigationItemId": None,
                        "sortOrder": 10,
                        "version": 3,
                    }
                ]
            },
            "platform",
        ),
        (
            "WORKFORCE.HRIS.SYNC.PREVIEW",
            "HRIS_MAPPING_PROFILE",
            {"idempotencyKey": "sample-sync-1042"},
            "people",
        ),
        ("WORKFORCE.HRIS.CONNECTOR.CHECK", "HRIS_CONNECTOR", {}, "people"),
        ("SCIM.CONNECTOR.ROTATE", "SCIM_CONNECTOR", {}, "auth"),
        (
            "PROVIDER.TENANT.ONBOARD.PREVIEW",
            "TENANT_DRAFT",
            {
                "tenantKey": "seoul-telecom",
                "displayName": "Seoul Telecom",
                "serviceTier": "REGULATED",
                "dataRegion": "ap-northeast-2",
                "isolationModel": "BRIDGE",
                "entitlementKeys": ["people.core", "agent.governed"],
            },
            "provider",
        ),
        (
            "PROVIDER.TENANT.ENTITLEMENT.REPLACE",
            "TENANT",
            {"entitlementKeys": ["people.core"]},
            "provider",
        ),
    ],
)
def test_registered_admin_command_catalog_resolves(
    command_key: str,
    target_type: str,
    parameters: dict,
    target_service: str,
) -> None:
    payload = admin_change_payload()
    payload["adminChange"] = {
        "commandKey": command_key,
        "targetType": target_type,
        "targetId": "4d36e968-e325-4cf7-9423-2286d83cae7a",
        "expectedVersion": 2,
        "parameters": parameters,
        "justification": "A governed administration preview is required.",
    }

    response = asyncio.run(post_preview(payload))

    assert response.status_code == 200, response.text
    plan = response.json()["data"]
    assert plan["adminCommand"]["commandKey"] == command_key
    assert plan["adminCommand"]["targetService"] == target_service
    assert plan["mutationAllowed"] is False


def test_group_role_assignment_requires_scope_reference_and_valid_period() -> None:
    payload = admin_change_payload()
    payload["adminChange"]["parameters"].update(
        {
            "scopeType": "ORG_UNIT",
            "validFrom": "2026-09-01T00:00:00Z",
            "validTo": "2026-08-01T00:00:00Z",
        }
    )

    response = asyncio.run(post_preview(payload))

    assert response.status_code == 422
