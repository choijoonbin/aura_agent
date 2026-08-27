import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from dwp_agent.admin_authority import (
    APP_GOVERNANCE_AUTHORITY,
    required_admin_preflight_authorities,
)
from dwp_agent.admin_commands import (
    AdminCommandValidationError,
    resolve_admin_command,
)
from dwp_agent.main import app


SERVICE_TOKEN = "test-gateway-service-token"


@pytest.fixture(autouse=True)
def configured_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("DWP_AGENT_REGISTRY_MODE", "optional")
    monkeypatch.delenv("SERVICE_PLATFORM_URL", raising=False)
    monkeypatch.delenv("DWP_PLATFORM_RUNTIME_SERVICE_TOKEN", raising=False)


async def post_preview(
    payload: dict,
    *,
    tenant_id: str = "tenant-1",
    include_command_permission: bool = True,
    identity_plane: str | None = None,
    roles: str | None = None,
    resource_roles: str | None = None,
    include_command_authority: bool = True,
    additional_permissions: tuple[str, ...] = (),
):
    definition = None
    admin_change = payload.get("adminChange")
    if isinstance(admin_change, dict):
        try:
            definition = resolve_admin_command(
                admin_change.get("commandKey", ""),
                admin_change.get("targetType", ""),
                admin_change.get("parameters", {}),
            )
        except AdminCommandValidationError:
            pass
    resolved_plane = identity_plane or (definition.identity_plane if definition else "TENANT")
    permissions = ["APP.ASK:VIEW"] if resolved_plane == "TENANT" else []
    if (
        include_command_permission
        and definition is not None
        and definition.authority_kind == "TENANT_PERMISSION"
    ):
        permissions.append(definition.required_permission)
    permissions.extend(additional_permissions)
    automatic_role = None
    automatic_resource_role = None
    if (
        include_command_authority
        and definition is not None
        and definition.authority_kind == APP_GOVERNANCE_AUTHORITY
    ):
        candidate = required_admin_preflight_authorities(
            definition,
            admin_change.get("parameters", {}),
        )[0]
        if candidate.startswith("TENANT_ROLE:"):
            automatic_role = candidate.removeprefix("TENANT_ROLE:")
        if candidate.startswith("RESOURCE_ROLE:"):
            automatic_resource_role = candidate.removeprefix("RESOURCE_ROLE:")
    resolved_roles = roles or automatic_role or (
        sorted(definition.required_roles)[0]
        if definition is not None and definition.required_roles
        else "TENANT_ADMIN"
    )
    resolved_resource_roles = (
        resource_roles if resource_roles is not None else automatic_resource_role or ""
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/v1/plans/preview",
            json=payload,
            headers={
                "X-DWP-Service-Token": SERVICE_TOKEN,
                "X-DWP-User-ID": "admin-7",
                "X-DWP-Tenant-ID": tenant_id,
                "X-DWP-Roles": resolved_roles,
                "X-Correlation-ID": "correlation-admin-change",
                "X-DWP-Permissions": ",".join(permissions),
                "X-DWP-Resource-Roles": resolved_resource_roles,
                "X-DWP-Identity-Plane": resolved_plane,
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
                "justification": "Operations staff require governed help desk access.",
            },
            "justification": "Operations staff require governed help desk access.",
        },
    }


def app_responsibility_payload(
    command_key: str,
    *,
    responsibility_code: str,
    scope_key: str = "RS_MAIL",
) -> dict:
    if command_key == "ACCESS.APP_RESPONSIBILITY.REQUEST":
        target_type = "APP_RESOURCE_SET"
        parameters = {
            "principalType": "USER",
            "principalRef": "42",
            "responsibilityCode": responsibility_code,
            "resourceSetId": "4d36e968-e325-4cf7-9423-2286d83cae7a",
            "scopeResourceSetKey": scope_key,
            "justification": "Grant a governed application responsibility.",
        }
    elif command_key == "ACCESS.APP_RESPONSIBILITY.DECIDE":
        target_type = "APP_ADMIN_ASSIGNMENT"
        parameters = {
            "decision": "APPROVED",
            "reason": "Approved after independent governance review.",
            "scopeResourceSetKey": scope_key,
            "targetResponsibilityCode": responsibility_code,
            "version": 1,
        }
    else:
        target_type = "APP_ADMIN_ASSIGNMENT"
        parameters = {
            "reason": "Responsibility is no longer operationally required.",
            "scopeResourceSetKey": scope_key,
            "targetResponsibilityCode": responsibility_code,
            "version": 2,
        }
    payload = admin_change_payload()
    payload["adminChange"] = {
        "commandKey": command_key,
        "targetType": target_type,
        "targetId": "4d36e968-e325-4cf7-9423-2286d83cae7a",
        "expectedVersion": parameters.get("version", 0),
        "parameters": parameters,
        "justification": "Preview an application responsibility governance action.",
    }
    return payload


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
        "catalogRevision": 3,
        "targetService": "auth",
        "httpMethod": "POST",
        "endpointTemplate": "/auth/admin/access/governance/group-role-assignments",
        "requiredPermission": "ADMIN.IDENTITY_DIRECTORY:MANAGE",
        "authorityKind": "TENANT_PERMISSION",
        "identityPlane": "TENANT",
        "requiredRoles": [],
        "requiredAuthorities": [
            "TENANT_PERMISSION:ADMIN.IDENTITY_DIRECTORY:MANAGE"
        ],
        "bodyParameters": [
            "assignmentType",
            "groupId",
            "justification",
            "roleId",
            "scopeRef",
            "scopeType",
            "validFrom",
            "validTo",
        ],
        "queryParameters": [],
        "headerParameters": {},
        "contextParameters": [],
        "finalAuthorityService": "auth",
    }
    assert [step["tool"] for step in plan["steps"]] == [
        "policy.check",
        "admin.command.resolve",
        "auth.preview",
        "workflow.human-approval",
    ]


def test_admin_change_requires_the_registered_command_permission() -> None:
    response = asyncio.run(
        post_preview(admin_change_payload(), include_command_permission=False)
    )

    assert response.status_code == 403


def test_app_responsibility_request_accepts_exact_owner_or_catalog_not_coarse_manage() -> None:
    payload = app_responsibility_payload(
        "ACCESS.APP_RESPONSIBILITY.REQUEST",
        responsibility_code="APP_ACCESS_MANAGER",
    )

    exact_owner = asyncio.run(
        post_preview(
            payload,
            roles="WORKSPACE_MEMBER",
            resource_roles="APP_OWNER@RS_MAIL",
            include_command_authority=False,
        )
    )
    catalog = asyncio.run(
        post_preview(
            payload,
            roles="APP_CATALOG_ADMIN",
            resource_roles="",
            include_command_authority=False,
        )
    )
    wrong_scope = asyncio.run(
        post_preview(
            payload,
            roles="WORKSPACE_MEMBER",
            resource_roles="APP_OWNER@RS_CALENDAR",
            include_command_authority=False,
        )
    )
    coarse_manage_only = asyncio.run(
        post_preview(
            payload,
            roles="TENANT_ADMIN",
            resource_roles="",
            include_command_authority=False,
            additional_permissions=("ADMIN.APP_GOVERNANCE:MANAGE",),
        )
    )

    assert exact_owner.status_code == 200
    assert catalog.status_code == 200
    assert wrong_scope.status_code == 403
    assert coarse_manage_only.status_code == 403
    assert "RS_MAIL" not in wrong_scope.text
    resolution = exact_owner.json()["data"]["adminCommand"]
    assert resolution["catalogRevision"] == 4
    assert resolution["requiredPermission"] is None
    assert resolution["authorityKind"] == "APP_GOVERNANCE_CAPABILITY"
    assert resolution["requiredAuthorities"] == [
        "TENANT_ROLE:APP_CATALOG_ADMIN",
        "RESOURCE_ROLE:APP_OWNER@RS_MAIL",
    ]
    assert resolution["contextParameters"] == ["scopeResourceSetKey"]
    assert "scopeResourceSetKey" not in resolution["bodyParameters"]
    assert resolution["finalAuthorityService"] == "auth"
    assert "remains the final authority" in exact_owner.json()["data"]["steps"][1][
        "description"
    ]


def test_app_preflight_hash_binds_the_gateway_resource_role_evidence() -> None:
    payload = app_responsibility_payload(
        "ACCESS.APP_RESPONSIBILITY.REQUEST",
        responsibility_code="APP_ACCESS_MANAGER",
    )

    owner = asyncio.run(
        post_preview(
            payload,
            roles="WORKSPACE_MEMBER",
            resource_roles="APP_OWNER@RS_MAIL",
            include_command_authority=False,
        )
    )
    catalog = asyncio.run(
        post_preview(
            payload,
            roles="APP_CATALOG_ADMIN",
            resource_roles="",
            include_command_authority=False,
        )
    )

    assert owner.status_code == 200
    assert catalog.status_code == 200
    assert owner.json()["data"]["planHash"] != catalog.json()["data"]["planHash"]


def test_app_owner_request_remains_catalog_admin_only() -> None:
    payload = app_responsibility_payload(
        "ACCESS.APP_RESPONSIBILITY.REQUEST",
        responsibility_code="APP_OWNER",
    )

    scoped_owner = asyncio.run(
        post_preview(
            payload,
            roles="WORKSPACE_MEMBER",
            resource_roles="APP_OWNER@RS_MAIL",
            include_command_authority=False,
        )
    )
    catalog = asyncio.run(
        post_preview(
            payload,
            roles="APP_CATALOG_ADMIN",
            resource_roles="",
            include_command_authority=False,
        )
    )

    assert scoped_owner.status_code == 403
    assert catalog.status_code == 200
    assert catalog.json()["data"]["adminCommand"]["requiredAuthorities"] == [
        "TENANT_ROLE:APP_CATALOG_ADMIN"
    ]


def test_app_responsibility_decision_matches_target_duty_and_exact_scope() -> None:
    manager_payload = app_responsibility_payload(
        "ACCESS.APP_RESPONSIBILITY.DECIDE",
        responsibility_code="APP_ACCESS_MANAGER",
    )
    approver_payload = app_responsibility_payload(
        "ACCESS.APP_RESPONSIBILITY.DECIDE",
        responsibility_code="APP_ACCESS_APPROVER",
    )
    owner_payload = app_responsibility_payload(
        "ACCESS.APP_RESPONSIBILITY.DECIDE",
        responsibility_code="APP_OWNER",
    )

    exact_approver = asyncio.run(
        post_preview(
            manager_payload,
            roles="WORKSPACE_MEMBER",
            resource_roles="APP_ACCESS_APPROVER@RS_MAIL",
            include_command_authority=False,
        )
    )
    wrong_scope = asyncio.run(
        post_preview(
            manager_payload,
            roles="WORKSPACE_MEMBER",
            resource_roles="APP_ACCESS_APPROVER@RS_CALENDAR",
            include_command_authority=False,
        )
    )
    catalog_for_non_bootstrap = asyncio.run(
        post_preview(
            manager_payload,
            roles="APP_CATALOG_ADMIN",
            resource_roles="",
            include_command_authority=False,
        )
    )
    catalog_bootstrap_candidate = asyncio.run(
        post_preview(
            approver_payload,
            roles="APP_CATALOG_ADMIN",
            resource_roles="",
            include_command_authority=False,
        )
    )
    owner_catalog_decision = asyncio.run(
        post_preview(
            owner_payload,
            roles="APP_CATALOG_ADMIN",
            resource_roles="",
            include_command_authority=False,
        )
    )

    assert exact_approver.status_code == 200
    assert wrong_scope.status_code == 403
    assert catalog_for_non_bootstrap.status_code == 403
    assert catalog_bootstrap_candidate.status_code == 200
    assert owner_catalog_decision.status_code == 200
    assert catalog_bootstrap_candidate.json()["data"]["adminCommand"][
        "requiredAuthorities"
    ] == [
        "TENANT_ROLE:APP_CATALOG_ADMIN",
        "RESOURCE_ROLE:APP_ACCESS_APPROVER@RS_MAIL",
    ]


def test_app_responsibility_revoke_requires_manager_scope_except_owner() -> None:
    approver_payload = app_responsibility_payload(
        "ACCESS.APP_RESPONSIBILITY.REVOKE",
        responsibility_code="APP_ACCESS_APPROVER",
    )
    owner_payload = app_responsibility_payload(
        "ACCESS.APP_RESPONSIBILITY.REVOKE",
        responsibility_code="APP_OWNER",
    )

    exact_manager = asyncio.run(
        post_preview(
            approver_payload,
            roles="WORKSPACE_MEMBER",
            resource_roles="APP_ACCESS_MANAGER@RS_MAIL",
            include_command_authority=False,
        )
    )
    approver_cannot_revoke = asyncio.run(
        post_preview(
            approver_payload,
            roles="WORKSPACE_MEMBER",
            resource_roles="APP_ACCESS_APPROVER@RS_MAIL",
            include_command_authority=False,
        )
    )
    catalog_cannot_revoke_non_owner = asyncio.run(
        post_preview(
            approver_payload,
            roles="APP_CATALOG_ADMIN",
            resource_roles="",
            include_command_authority=False,
        )
    )
    owner_catalog_revoke = asyncio.run(
        post_preview(
            owner_payload,
            roles="APP_CATALOG_ADMIN",
            resource_roles="",
            include_command_authority=False,
        )
    )

    assert exact_manager.status_code == 200
    assert approver_cannot_revoke.status_code == 403
    assert catalog_cannot_revoke_non_owner.status_code == 403
    assert owner_catalog_revoke.status_code == 200


def test_provider_command_requires_provider_plane_and_coarse_role() -> None:
    payload = admin_change_payload()
    payload["adminChange"] = {
        "commandKey": "PROVIDER.TENANT.ONBOARD.PREVIEW",
        "targetType": "TENANT_DRAFT",
        "targetId": "seoul-telecom",
        "expectedVersion": 0,
        "parameters": {
            "idempotencyKey": "onboard-seoul-telecom-1",
            "organizationKey": "skax",
            "organizationName": "SK AX",
            "tenantKey": "seoul-telecom",
            "displayName": "Seoul Telecom",
            "environmentKey": "production",
            "serviceTier": "REGULATED",
            "dataRegion": "ap-northeast-2",
            "isolationModel": "BRIDGE",
            "defaultLocale": "ko-KR",
            "timeZone": "Asia/Seoul",
            "initialAdminDisplayName": "Hyunwoo Park",
            "initialAdminEmail": "hyunwoo.park@sk.com",
            "entitlementKeys": ["people.core"],
            "justification": "Prepare the regulated tenant onboarding plan.",
        },
        "justification": "Preview a governed Provider tenant onboarding operation.",
    }

    allowed = asyncio.run(
        post_preview(payload, identity_plane="PROVIDER", roles="PROVIDER_TENANT_PROVISIONER")
    )
    wrong_plane = asyncio.run(
        post_preview(payload, identity_plane="TENANT", roles="PROVIDER_TENANT_PROVISIONER")
    )
    wrong_role = asyncio.run(
        post_preview(payload, identity_plane="PROVIDER", roles="PROVIDER_SUPPORT_OPERATOR")
    )

    assert allowed.status_code == 200
    assert allowed.json()["data"]["adminCommand"] == {
        "commandKey": "PROVIDER.TENANT.ONBOARD.PREVIEW",
        "catalogRevision": 3,
        "targetService": "provider",
        "httpMethod": "POST",
        "endpointTemplate": "/v1/admin/onboarding-plans",
        "requiredPermission": "TENANT_WRITE",
        "authorityKind": "PROVIDER_ROLE",
        "identityPlane": "PROVIDER",
        "requiredRoles": ["PROVIDER_ADMIN", "PROVIDER_TENANT_PROVISIONER"],
        "requiredAuthorities": [
            "TENANT_ROLE:PROVIDER_ADMIN",
            "TENANT_ROLE:PROVIDER_TENANT_PROVISIONER",
        ],
        "bodyParameters": [
            "customerReference",
            "dataRegion",
            "defaultLocale",
            "displayName",
            "entitlementKeys",
            "environmentKey",
            "initialAdminDisplayName",
            "initialAdminEmail",
            "isolationModel",
            "justification",
            "legalName",
            "organizationKey",
            "organizationName",
            "primaryDomain",
            "serviceTier",
            "tenantKey",
            "timeZone",
        ],
        "queryParameters": [],
        "headerParameters": {"idempotencyKey": "Idempotency-Key"},
        "contextParameters": [],
        "finalAuthorityService": "provider",
    }
    assert wrong_plane.status_code == 403
    assert wrong_role.status_code == 403


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
                "assignmentType": "ACTIVE",
                "scopeType": "RESOURCE",
                "scopeRef": "app:people",
                "justification": "People operations require this scoped role assignment.",
            },
            "auth",
        ),
        ("ACCESS.GROUP_ROLE.REVOKE", "GROUP_ROLE_ASSIGNMENT", {"version": 2}, "auth"),
        (
            "ACCESS.ROLE.PERMISSION.REPLACE",
            "ROLE",
            {
                "version": 3,
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
        (
            "ACCESS.APP_RESPONSIBILITY.REQUEST",
            "APP_RESOURCE_SET",
            {
                "principalType": "USER",
                "principalRef": "42",
                "responsibilityCode": "APP_ACCESS_APPROVER",
                "resourceSetId": "4d36e968-e325-4cf7-9423-2286d83cae7a",
                "scopeResourceSetKey": "RS_MAIL",
                "validTo": "2099-12-31T00:00:00Z",
                "justification": "Approves access for the mail application.",
            },
            "auth",
        ),
        (
            "ACCESS.APP_RESPONSIBILITY.DECIDE",
            "APP_ADMIN_ASSIGNMENT",
            {
                "decision": "APPROVED",
                "reason": "Approved by an independent application approver.",
                "scopeResourceSetKey": "RS_MAIL",
                "targetResponsibilityCode": "APP_ACCESS_MANAGER",
                "version": 1,
            },
            "auth",
        ),
        (
            "ACCESS.APP_RESPONSIBILITY.REVOKE",
            "APP_ADMIN_ASSIGNMENT",
            {
                "reason": "Responsibility is no longer required.",
                "scopeResourceSetKey": "RS_MAIL",
                "targetResponsibilityCode": "APP_ACCESS_APPROVER",
                "version": 2,
            },
            "auth",
        ),
        ("NAVIGATION.ITEM.PUBLISH", "NAVIGATION_ITEM", {"version": 6}, "platform"),
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
        (
            "WORKFORCE.HRIS.CONNECTOR.CHECK",
            "HRIS_CONNECTOR",
            {"idempotencyKey": "connector-check-1042"},
            "people",
        ),
        ("SCIM.CONNECTOR.ROTATE", "SCIM_CONNECTOR", {}, "auth"),
        (
            "PROVIDER.TENANT.ONBOARD.PREVIEW",
            "TENANT_DRAFT",
            {
                "idempotencyKey": "onboard-seoul-telecom-2",
                "organizationKey": "skax",
                "organizationName": "SK AX",
                "tenantKey": "seoul-telecom",
                "displayName": "Seoul Telecom",
                "environmentKey": "production",
                "serviceTier": "REGULATED",
                "dataRegion": "ap-northeast-2",
                "isolationModel": "BRIDGE",
                "defaultLocale": "ko-KR",
                "timeZone": "Asia/Seoul",
                "initialAdminDisplayName": "Hyunwoo Park",
                "initialAdminEmail": "hyunwoo.park@sk.com",
                "entitlementKeys": ["people.core", "agent.governed"],
                "justification": "Preview the regulated production tenant onboarding plan.",
            },
            "provider",
        ),
        (
            "PROVIDER.TENANT.ENTITLEMENT.REPLACE",
            "TENANT",
            {
                "entitlementKeys": ["people.core"],
                "justification": "Replace the tenant entitlement set after approval.",
                "version": 4,
            },
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


@pytest.mark.parametrize(
    ("command_key", "target_type", "parameters"),
    [
        (
            "ACCESS.GROUP_ROLE.ASSIGN",
            "GROUP",
            {
                "groupId": 10,
                "roleId": 20,
                "assignmentType": "ELIGIBLE",
                "scopeType": "TENANT",
                "justification": "Legacy eligible assignments are not executable.",
            },
        ),
        (
            "ACCESS.GROUP_ROLE.ASSIGN",
            "GROUP",
            {
                "groupId": 10,
                "roleId": 20,
                "assignmentType": "ACTIVE",
                "scopeType": "TENANT",
            },
        ),
        ("ACCESS.GROUP_ROLE.REVOKE", "GROUP_ROLE_ASSIGNMENT", {}),
        (
            "ACCESS.ROLE.PERMISSION.REPLACE",
            "ROLE",
            {
                "permissions": [
                    {"resourceId": 12, "permissionCode": "READ", "effect": "ALLOW"}
                ]
            },
        ),
        (
            "ACCESS.APP_RESPONSIBILITY.REQUEST",
            "APP_RESOURCE_SET",
            {
                "principalType": "USER",
                "principalRef": "42",
                "responsibilityCode": "APP_CONFIG_ADMIN",
                "resourceSetId": "4d36e968-e325-4cf7-9423-2286d83cae7a",
                "scopeResourceSetKey": "RS_MAIL",
                "justification": "Legacy capability duties cannot use this responsibility path.",
            },
        ),
        (
            "ACCESS.APP_RESPONSIBILITY.REQUEST",
            "APP_RESOURCE_SET",
            {
                "principalType": "USER",
                "principalRef": "42",
                "responsibilityCode": "APP_ACCESS_MANAGER",
                "resourceSetId": "4d36e968-e325-4cf7-9423-2286d83cae7a",
                "justification": "Missing exact-scope context must fail closed.",
            },
        ),
        (
            "ACCESS.APP_RESPONSIBILITY.DECIDE",
            "APP_ADMIN_ASSIGNMENT",
            {
                "decision": "APPROVED",
                "reason": "Missing target duty context must fail closed.",
                "scopeResourceSetKey": "RS_MAIL",
                "version": 1,
            },
        ),
    ],
)
def test_admin_catalog_rejects_inputs_the_owning_api_cannot_execute(
    command_key: str,
    target_type: str,
    parameters: dict,
) -> None:
    with pytest.raises(AdminCommandValidationError):
        resolve_admin_command(command_key, target_type, parameters)


def test_admin_catalog_preserves_non_body_transport_bindings() -> None:
    revoke = resolve_admin_command(
        "ACCESS.GROUP_ROLE.REVOKE",
        "GROUP_ROLE_ASSIGNMENT",
        {"version": 2},
    )
    hris = resolve_admin_command(
        "WORKFORCE.HRIS.CONNECTOR.CHECK",
        "HRIS_CONNECTOR",
        {"idempotencyKey": "connector-check-1042"},
    )
    onboarding = resolve_admin_command(
        "PROVIDER.TENANT.ONBOARD.PREVIEW",
        "TENANT_DRAFT",
        {
            "idempotencyKey": "onboard-seoul-telecom-3",
            "organizationKey": "skax",
            "organizationName": "SK AX",
            "tenantKey": "seoul-telecom",
            "displayName": "Seoul Telecom",
            "environmentKey": "production",
            "serviceTier": "REGULATED",
            "dataRegion": "ap-northeast-2",
            "isolationModel": "BRIDGE",
            "defaultLocale": "ko-KR",
            "timeZone": "Asia/Seoul",
            "initialAdminDisplayName": "Hyunwoo Park",
            "initialAdminEmail": "hyunwoo.park@sk.com",
            "entitlementKeys": ["people.core"],
            "justification": "Preview the regulated production tenant onboarding plan.",
        },
    )
    app_request = resolve_admin_command(
        "ACCESS.APP_RESPONSIBILITY.REQUEST",
        "APP_RESOURCE_SET",
        {
            "principalType": "USER",
            "principalRef": "42",
            "responsibilityCode": "APP_ACCESS_MANAGER",
            "resourceSetId": "4d36e968-e325-4cf7-9423-2286d83cae7a",
            "scopeResourceSetKey": "RS_MAIL",
            "justification": "Grant a governed application responsibility.",
        },
    )

    assert revoke.http_method == "PATCH"
    assert revoke.query_parameters == frozenset({"version"})
    assert revoke.resolved_body_parameters() == frozenset()
    assert hris.header_parameters == {"idempotencyKey": "Idempotency-Key"}
    assert hris.resolved_body_parameters() == frozenset()
    assert onboarding.header_parameters == {"idempotencyKey": "Idempotency-Key"}
    assert "idempotencyKey" not in onboarding.resolved_body_parameters()
    assert app_request.context_parameters == frozenset({"scopeResourceSetKey"})
    assert "scopeResourceSetKey" not in app_request.resolved_body_parameters()
