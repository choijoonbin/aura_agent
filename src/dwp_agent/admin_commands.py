from __future__ import annotations

from dataclasses import dataclass, field
from re import fullmatch
from typing import Any

from .admin_authority import (
    APP_GOVERNANCE_AUTHORITY,
    RESOURCE_SET_KEY_PATTERN,
)
from .admin_command_validation import (
    AdminCommandValidationError,
    validate_admin_command_semantics,
)


@dataclass(frozen=True)
class ParameterRule:
    json_types: tuple[type, ...]
    required: bool = False
    pattern: str | None = None
    values: frozenset[str] | None = None
    min_items: int = 0
    max_items: int = 100
    item_type: type | None = None
    item_pattern: str | None = None
    min_value: int | None = None
    min_length: int = 1
    max_length: int = 4_000

    def validate(self, key: str, value: Any) -> None:
        expected_types = self.json_types
        if int in expected_types and isinstance(value, bool):
            raise AdminCommandValidationError(f"Parameter '{key}' must not be boolean.")
        if not isinstance(value, expected_types):
            names = ", ".join(item.__name__ for item in expected_types)
            raise AdminCommandValidationError(
                f"Parameter '{key}' must be one of: {names}."
            )
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise AdminCommandValidationError(f"Parameter '{key}' must not be blank.")
            if not self.min_length <= len(normalized) <= self.max_length:
                raise AdminCommandValidationError(
                    f"Parameter '{key}' must contain {self.min_length}..{self.max_length} characters."
                )
            if self.pattern and fullmatch(self.pattern, value) is None:
                raise AdminCommandValidationError(f"Parameter '{key}' has an invalid format.")
            if self.values and value not in self.values:
                raise AdminCommandValidationError(f"Parameter '{key}' has an unsupported value.")
        if isinstance(value, list):
            if not self.min_items <= len(value) <= self.max_items:
                raise AdminCommandValidationError(
                    f"Parameter '{key}' must contain {self.min_items}..{self.max_items} items."
                )
            if self.item_type is not None and any(
                not isinstance(item, self.item_type) or isinstance(item, bool)
                for item in value
            ):
                raise AdminCommandValidationError(
                    f"Parameter '{key}' contains an invalid item type."
                )
            if self.item_pattern and any(
                not isinstance(item, str) or fullmatch(self.item_pattern, item) is None
                for item in value
            ):
                raise AdminCommandValidationError(
                    f"Parameter '{key}' contains an invalid item value."
                )
        if isinstance(value, int) and self.min_value is not None and value < self.min_value:
            raise AdminCommandValidationError(
                f"Parameter '{key}' must be at least {self.min_value}."
            )


@dataclass(frozen=True)
class AdminCommandDefinition:
    command_key: str
    target_types: frozenset[str]
    target_service: str
    http_method: str
    endpoint_template: str
    required_permission: str | None
    parameters: dict[str, ParameterRule]
    body_parameters: frozenset[str] | None = None
    query_parameters: frozenset[str] = frozenset()
    header_parameters: dict[str, str] = field(default_factory=dict)
    context_parameters: frozenset[str] = frozenset()
    authority_kind: str = "TENANT_PERMISSION"
    identity_plane: str = "TENANT"
    required_roles: frozenset[str] = frozenset()
    catalog_revision: int = 3

    def validate(self, target_type: str, parameters: dict[str, Any]) -> None:
        self._validate_transport_contract()
        if target_type not in self.target_types:
            supported = ", ".join(sorted(self.target_types))
            raise AdminCommandValidationError(
                f"Command '{self.command_key}' requires target type: {supported}."
            )
        unknown = sorted(set(parameters) - set(self.parameters))
        if unknown:
            raise AdminCommandValidationError(
                f"Command '{self.command_key}' has unsupported parameters: {', '.join(unknown)}."
            )
        missing = sorted(
            key
            for key, rule in self.parameters.items()
            if rule.required and key not in parameters
        )
        if missing:
            raise AdminCommandValidationError(
                f"Command '{self.command_key}' is missing parameters: {', '.join(missing)}."
            )
        for key, value in parameters.items():
            self.parameters[key].validate(key, value)
        validate_admin_command_semantics(self.command_key, parameters)

    def resolved_body_parameters(self) -> frozenset[str]:
        if self.body_parameters is not None:
            return self.body_parameters
        return frozenset(self.parameters) - self.query_parameters - frozenset(
            self.header_parameters
        ) - self.context_parameters

    def _validate_transport_contract(self) -> None:
        body = self.resolved_body_parameters()
        query = self.query_parameters
        headers = frozenset(self.header_parameters)
        context = self.context_parameters
        if (
            body & query
            or body & headers
            or body & context
            or query & headers
            or query & context
            or headers & context
        ):
            raise RuntimeError(f"Command '{self.command_key}' has overlapping parameter bindings.")
        if body | query | headers | context != frozenset(self.parameters):
            raise RuntimeError(f"Command '{self.command_key}' has incomplete parameter bindings.")
        if any(not name.strip() for name in self.header_parameters.values()):
            raise RuntimeError(f"Command '{self.command_key}' has an invalid header binding.")


IDENTIFIER = r"[A-Za-z][A-Za-z0-9:._/-]{0,254}"
ISO_DATE_TIME = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z"
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
PRINCIPAL_REF = r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,254}"


def _string(
    *,
    required: bool = False,
    pattern: str | None = None,
    min_length: int = 1,
    max_length: int = 4_000,
) -> ParameterRule:
    return ParameterRule(
        (str,),
        required=required,
        pattern=pattern,
        min_length=min_length,
        max_length=max_length,
    )


def _enum(*values: str, required: bool = False) -> ParameterRule:
    return ParameterRule((str,), required=required, values=frozenset(values))


def _list(
    item_type: type,
    *,
    required: bool = False,
    min_items: int = 0,
    max_items: int = 100,
    item_pattern: str | None = None,
) -> ParameterRule:
    return ParameterRule(
        (list,),
        required=required,
        min_items=min_items,
        max_items=max_items,
        item_type=item_type,
        item_pattern=item_pattern,
    )


def _version() -> ParameterRule:
    return ParameterRule((int,), required=True, min_value=0)


ADMIN_COMMAND_CATALOG: dict[str, AdminCommandDefinition] = {
    "ACCESS.GROUP_ROLE.ASSIGN": AdminCommandDefinition(
        command_key="ACCESS.GROUP_ROLE.ASSIGN",
        target_types=frozenset({"GROUP"}),
        target_service="auth",
        http_method="POST",
        endpoint_template="/auth/admin/access/governance/group-role-assignments",
        required_permission="ADMIN.IDENTITY_DIRECTORY:MANAGE",
        parameters={
            "groupId": ParameterRule((int,), required=True),
            "roleId": ParameterRule((int,), required=True),
            "assignmentType": _enum("ACTIVE", required=True),
            "scopeType": _enum("TENANT", "ORG_UNIT", "RESOURCE", required=True),
            "scopeRef": _string(pattern=IDENTIFIER),
            "validFrom": _string(pattern=ISO_DATE_TIME),
            "validTo": _string(pattern=ISO_DATE_TIME),
            "justification": _string(required=True, min_length=10, max_length=500),
        },
    ),
    "ACCESS.GROUP_ROLE.REVOKE": AdminCommandDefinition(
        command_key="ACCESS.GROUP_ROLE.REVOKE",
        target_types=frozenset({"GROUP_ROLE_ASSIGNMENT"}),
        target_service="auth",
        http_method="PATCH",
        endpoint_template=(
            "/auth/admin/access/governance/group-role-assignments/{targetId}/revoke"
        ),
        required_permission="ADMIN.IDENTITY_DIRECTORY:MANAGE",
        parameters={"version": _version()},
        query_parameters=frozenset({"version"}),
    ),
    "ACCESS.ROLE.PERMISSION.REPLACE": AdminCommandDefinition(
        command_key="ACCESS.ROLE.PERMISSION.REPLACE",
        target_types=frozenset({"ROLE"}),
        target_service="auth",
        http_method="PUT",
        endpoint_template="/auth/admin/access/governance/roles/{targetId}/permissions",
        required_permission="ADMIN.IDENTITY_DIRECTORY:MANAGE",
        parameters={
            "version": _version(),
            "permissions": _list(dict, required=True, max_items=500),
        },
    ),
    "ACCESS.APP_RESPONSIBILITY.REQUEST": AdminCommandDefinition(
        command_key="ACCESS.APP_RESPONSIBILITY.REQUEST",
        target_types=frozenset({"APP_RESOURCE_SET"}),
        target_service="auth",
        http_method="POST",
        endpoint_template="/auth/admin/access/app-governance/assignments",
        required_permission=None,
        authority_kind=APP_GOVERNANCE_AUTHORITY,
        catalog_revision=4,
        parameters={
            "principalType": _enum("USER", "GROUP", required=True),
            "principalRef": _string(required=True, pattern=PRINCIPAL_REF),
            "responsibilityCode": _enum(
                "APP_OWNER",
                "APP_ACCESS_MANAGER",
                "APP_ACCESS_APPROVER",
                "APP_ACCESS_REVIEWER",
                required=True,
            ),
            "resourceSetId": _string(required=True, pattern=UUID),
            "scopeResourceSetKey": _string(
                required=True, pattern=RESOURCE_SET_KEY_PATTERN
            ),
            "validTo": _string(pattern=ISO_DATE_TIME),
            "justification": _string(required=True, min_length=10, max_length=1_000),
        },
        context_parameters=frozenset({"scopeResourceSetKey"}),
    ),
    "ACCESS.APP_RESPONSIBILITY.DECIDE": AdminCommandDefinition(
        command_key="ACCESS.APP_RESPONSIBILITY.DECIDE",
        target_types=frozenset({"APP_ADMIN_ASSIGNMENT"}),
        target_service="auth",
        http_method="POST",
        endpoint_template=(
            "/auth/admin/access/app-governance/assignments/{targetId}/decision"
        ),
        required_permission=None,
        authority_kind=APP_GOVERNANCE_AUTHORITY,
        catalog_revision=4,
        parameters={
            "decision": _enum("APPROVED", "DENIED", required=True),
            "reason": _string(required=True, min_length=10, max_length=1_000),
            "scopeResourceSetKey": _string(
                required=True, pattern=RESOURCE_SET_KEY_PATTERN
            ),
            "targetResponsibilityCode": _enum(
                "APP_OWNER",
                "APP_ACCESS_MANAGER",
                "APP_ACCESS_APPROVER",
                "APP_ACCESS_REVIEWER",
                required=True,
            ),
            "version": _version(),
        },
        context_parameters=frozenset(
            {"scopeResourceSetKey", "targetResponsibilityCode"}
        ),
    ),
    "ACCESS.APP_RESPONSIBILITY.REVOKE": AdminCommandDefinition(
        command_key="ACCESS.APP_RESPONSIBILITY.REVOKE",
        target_types=frozenset({"APP_ADMIN_ASSIGNMENT"}),
        target_service="auth",
        http_method="PATCH",
        endpoint_template=(
            "/auth/admin/access/app-governance/assignments/{targetId}/revoke"
        ),
        required_permission=None,
        authority_kind=APP_GOVERNANCE_AUTHORITY,
        catalog_revision=4,
        parameters={
            "reason": _string(required=True, min_length=10, max_length=1_000),
            "scopeResourceSetKey": _string(
                required=True, pattern=RESOURCE_SET_KEY_PATTERN
            ),
            "targetResponsibilityCode": _enum(
                "APP_OWNER",
                "APP_ACCESS_MANAGER",
                "APP_ACCESS_APPROVER",
                "APP_ACCESS_REVIEWER",
                required=True,
            ),
            "version": _version(),
        },
        context_parameters=frozenset(
            {"scopeResourceSetKey", "targetResponsibilityCode"}
        ),
    ),
    "NAVIGATION.ITEM.PUBLISH": AdminCommandDefinition(
        command_key="NAVIGATION.ITEM.PUBLISH",
        target_types=frozenset({"NAVIGATION_ITEM"}),
        target_service="platform",
        http_method="POST",
        endpoint_template="/v1/admin/navigation/{targetId}/activate",
        required_permission="ADMIN.NAVIGATION:MANAGE",
        parameters={"version": _version()},
    ),
    "NAVIGATION.ORDER.UPDATE": AdminCommandDefinition(
        command_key="NAVIGATION.ORDER.UPDATE",
        target_types=frozenset({"NAVIGATION_TREE"}),
        target_service="platform",
        http_method="PUT",
        endpoint_template="/v1/admin/navigation/order",
        required_permission="ADMIN.NAVIGATION:MANAGE",
        parameters={"items": _list(dict, required=True, min_items=1)},
    ),
    "WORKFORCE.HRIS.SYNC.PREVIEW": AdminCommandDefinition(
        command_key="WORKFORCE.HRIS.SYNC.PREVIEW",
        target_types=frozenset({"HRIS_MAPPING_PROFILE"}),
        target_service="people",
        http_method="POST",
        endpoint_template="/v1/workforce/data-operations/hris/sample-import",
        required_permission="ACTION.WORKFORCE_DATA_OPERATIONS:MANAGE",
        parameters={"idempotencyKey": _string(required=True, pattern=IDENTIFIER)},
        header_parameters={"idempotencyKey": "Idempotency-Key"},
    ),
    "WORKFORCE.HRIS.CONNECTOR.CHECK": AdminCommandDefinition(
        command_key="WORKFORCE.HRIS.CONNECTOR.CHECK",
        target_types=frozenset({"HRIS_CONNECTOR"}),
        target_service="people",
        http_method="POST",
        endpoint_template=(
            "/v1/workforce/data-operations/hris/connectors/{targetId}/configuration-check"
        ),
        required_permission="ACTION.WORKFORCE_DATA_OPERATIONS:MANAGE",
        parameters={"idempotencyKey": _string(required=True, pattern=IDENTIFIER)},
        header_parameters={"idempotencyKey": "Idempotency-Key"},
    ),
    "SCIM.CONNECTOR.ROTATE": AdminCommandDefinition(
        command_key="SCIM.CONNECTOR.ROTATE",
        target_types=frozenset({"SCIM_CONNECTOR"}),
        target_service="auth",
        http_method="POST",
        endpoint_template=(
            "/auth/admin/provisioning/scim/connectors/{targetId}/rotate-secret"
        ),
        required_permission="ADMIN.IDENTITY_PROVISIONING:MANAGE",
        parameters={},
    ),
    "PROVIDER.TENANT.ONBOARD.PREVIEW": AdminCommandDefinition(
        command_key="PROVIDER.TENANT.ONBOARD.PREVIEW",
        target_types=frozenset({"TENANT_DRAFT"}),
        target_service="provider",
        http_method="POST",
        endpoint_template="/v1/admin/onboarding-plans",
        required_permission="TENANT_WRITE",
        authority_kind="PROVIDER_ROLE",
        identity_plane="PROVIDER",
        required_roles=frozenset({"PROVIDER_ADMIN", "PROVIDER_TENANT_PROVISIONER"}),
        parameters={
            "idempotencyKey": _string(required=True, pattern=IDENTIFIER),
            "organizationKey": _string(required=True, pattern=r"[a-z][a-z0-9-]{1,79}"),
            "organizationName": _string(required=True, max_length=240),
            "legalName": _string(max_length=320),
            "customerReference": _string(max_length=120),
            "tenantKey": _string(required=True, pattern=r"[a-z][a-z0-9-]{1,79}"),
            "displayName": _string(required=True, max_length=240),
            "environmentKey": _string(
                required=True, pattern=r"[a-z][a-z0-9-]{1,31}"
            ),
            "serviceTier": _enum("STANDARD", "ENTERPRISE", "REGULATED", required=True),
            "dataRegion": _string(required=True, pattern=r"[a-z0-9-]{2,40}"),
            "isolationModel": _enum("POOL", "BRIDGE", "SILO", required=True),
            "defaultLocale": _string(
                required=True, pattern=r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*"
            ),
            "timeZone": _string(required=True, max_length=80),
            "primaryDomain": _string(
                pattern=r"(?i)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
            ),
            "initialAdminDisplayName": _string(required=True, max_length=200),
            "initialAdminEmail": _string(
                required=True,
                pattern=r"[^@\s]+@[^@\s]+\.[^@\s]+",
                max_length=255,
            ),
            "entitlementKeys": _list(
                str,
                required=True,
                min_items=1,
                item_pattern=r"[a-z][a-z0-9.-]{1,119}",
            ),
            "justification": _string(required=True, max_length=1_000),
        },
        header_parameters={"idempotencyKey": "Idempotency-Key"},
    ),
    "PROVIDER.TENANT.ENTITLEMENT.REPLACE": AdminCommandDefinition(
        command_key="PROVIDER.TENANT.ENTITLEMENT.REPLACE",
        target_types=frozenset({"TENANT"}),
        target_service="provider",
        http_method="PUT",
        endpoint_template="/v1/admin/tenants/{targetId}/entitlements",
        required_permission="ENTITLEMENT_WRITE",
        authority_kind="PROVIDER_ROLE",
        identity_plane="PROVIDER",
        required_roles=frozenset({"PROVIDER_ADMIN", "PROVIDER_ENTITLEMENT_ADMIN"}),
        parameters={
            "entitlementKeys": _list(
                str,
                required=True,
                min_items=1,
                item_pattern=r"[a-z][a-z0-9.-]{1,119}",
            ),
            "justification": _string(required=True, max_length=1_000),
            "version": _version(),
        },
    ),
}


def resolve_admin_command(
    command_key: str,
    target_type: str,
    parameters: dict[str, Any],
) -> AdminCommandDefinition:
    definition = ADMIN_COMMAND_CATALOG.get(command_key)
    if definition is None:
        raise AdminCommandValidationError(
            f"Administration command '{command_key}' is not registered."
        )
    definition.validate(target_type, parameters)
    return definition
