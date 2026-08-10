from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from re import fullmatch
from typing import Any


class AdminCommandValidationError(ValueError):
    """Raised when an administration command does not match the allow-list."""


@dataclass(frozen=True)
class ParameterRule:
    json_types: tuple[type, ...]
    required: bool = False
    pattern: str | None = None
    values: frozenset[str] | None = None
    min_items: int = 0
    max_items: int = 100
    item_type: type | None = None

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
            if not value.strip():
                raise AdminCommandValidationError(f"Parameter '{key}' must not be blank.")
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


@dataclass(frozen=True)
class AdminCommandDefinition:
    command_key: str
    target_types: frozenset[str]
    target_service: str
    http_method: str
    endpoint_template: str
    required_permission: str
    parameters: dict[str, ParameterRule]
    catalog_revision: int = 1

    def validate(self, target_type: str, parameters: dict[str, Any]) -> None:
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
        _validate_semantics(self.command_key, parameters)


IDENTIFIER = r"[A-Za-z][A-Za-z0-9:._/-]{0,254}"
ISO_DATE_TIME = r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)?"
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"


def _string(*, required: bool = False, pattern: str | None = None) -> ParameterRule:
    return ParameterRule((str,), required=required, pattern=pattern)


def _enum(*values: str, required: bool = False) -> ParameterRule:
    return ParameterRule((str,), required=required, values=frozenset(values))


def _list(item_type: type, *, required: bool = False, min_items: int = 0) -> ParameterRule:
    return ParameterRule(
        (list,),
        required=required,
        min_items=min_items,
        item_type=item_type,
    )


ADMIN_COMMAND_CATALOG: dict[str, AdminCommandDefinition] = {
    "ACCESS.GROUP_ROLE.ASSIGN": AdminCommandDefinition(
        command_key="ACCESS.GROUP_ROLE.ASSIGN",
        target_types=frozenset({"GROUP"}),
        target_service="auth",
        http_method="POST",
        endpoint_template="/auth/admin/access/governance/group-role-assignments",
        required_permission="access:group-role:assign",
        parameters={
            "groupId": ParameterRule((int,), required=True),
            "roleId": ParameterRule((int,), required=True),
            "assignmentType": _enum("ACTIVE", "ELIGIBLE", required=True),
            "scopeType": _enum("TENANT", "ORG_UNIT", "RESOURCE", required=True),
            "scopeRef": _string(pattern=IDENTIFIER),
            "validFrom": _string(pattern=ISO_DATE_TIME),
            "validTo": _string(pattern=ISO_DATE_TIME),
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
        required_permission="access:group-role:revoke",
        parameters={},
    ),
    "ACCESS.ROLE.PERMISSION.REPLACE": AdminCommandDefinition(
        command_key="ACCESS.ROLE.PERMISSION.REPLACE",
        target_types=frozenset({"ROLE"}),
        target_service="auth",
        http_method="PUT",
        endpoint_template="/auth/admin/access/governance/roles/{targetId}/permissions",
        required_permission="access:role-permission:replace",
        parameters={"permissions": _list(dict, required=True)},
    ),
    "NAVIGATION.ITEM.PUBLISH": AdminCommandDefinition(
        command_key="NAVIGATION.ITEM.PUBLISH",
        target_types=frozenset({"NAVIGATION_ITEM"}),
        target_service="platform",
        http_method="POST",
        endpoint_template="/v1/admin/navigation/{targetId}/activate",
        required_permission="navigation:item:publish",
        parameters={},
    ),
    "NAVIGATION.ORDER.UPDATE": AdminCommandDefinition(
        command_key="NAVIGATION.ORDER.UPDATE",
        target_types=frozenset({"NAVIGATION_TREE"}),
        target_service="platform",
        http_method="PUT",
        endpoint_template="/v1/admin/navigation/order",
        required_permission="navigation:order:update",
        parameters={"items": _list(dict, required=True, min_items=1)},
    ),
    "PEOPLE.HRIS.SYNC.PREVIEW": AdminCommandDefinition(
        command_key="PEOPLE.HRIS.SYNC.PREVIEW",
        target_types=frozenset({"HRIS_MAPPING_PROFILE"}),
        target_service="people",
        http_method="POST",
        endpoint_template="/v1/admin/integrations/hris/sample-import",
        required_permission="people:hris-sync:preview",
        parameters={"idempotencyKey": _string(required=True, pattern=IDENTIFIER)},
    ),
    "PEOPLE.HRIS.CONNECTOR.CHECK": AdminCommandDefinition(
        command_key="PEOPLE.HRIS.CONNECTOR.CHECK",
        target_types=frozenset({"HRIS_CONNECTOR"}),
        target_service="people",
        http_method="POST",
        endpoint_template=(
            "/v1/admin/integrations/hris/connectors/{targetId}/configuration-check"
        ),
        required_permission="people:hris-connector:check",
        parameters={},
    ),
    "SCIM.CONNECTOR.ROTATE": AdminCommandDefinition(
        command_key="SCIM.CONNECTOR.ROTATE",
        target_types=frozenset({"SCIM_CONNECTOR"}),
        target_service="auth",
        http_method="POST",
        endpoint_template=(
            "/auth/admin/provisioning/scim/connectors/{targetId}/rotate-secret"
        ),
        required_permission="provisioning:scim-secret:rotate",
        parameters={},
    ),
    "PROVIDER.TENANT.ONBOARD.PREVIEW": AdminCommandDefinition(
        command_key="PROVIDER.TENANT.ONBOARD.PREVIEW",
        target_types=frozenset({"TENANT_DRAFT"}),
        target_service="provider",
        http_method="POST",
        endpoint_template="/v1/admin/onboarding-plans",
        required_permission="provider:tenant-onboarding:preview",
        parameters={
            "tenantKey": _string(required=True, pattern=r"[a-z][a-z0-9-]{1,79}"),
            "displayName": _string(required=True),
            "serviceTier": _enum("STANDARD", "ENTERPRISE", "REGULATED", required=True),
            "dataRegion": _string(required=True, pattern=r"[a-z0-9-]{2,40}"),
            "isolationModel": _enum("POOL", "BRIDGE", "SILO", required=True),
            "entitlementKeys": _list(str, required=True),
        },
    ),
    "PROVIDER.TENANT.ENTITLEMENT.REPLACE": AdminCommandDefinition(
        command_key="PROVIDER.TENANT.ENTITLEMENT.REPLACE",
        target_types=frozenset({"TENANT"}),
        target_service="provider",
        http_method="PUT",
        endpoint_template="/v1/admin/tenants/{targetId}/entitlements",
        required_permission="provider:tenant-entitlement:replace",
        parameters={"entitlementKeys": _list(str, required=True)},
    ),
}


def _validate_semantics(command_key: str, parameters: dict[str, Any]) -> None:
    if command_key == "ACCESS.GROUP_ROLE.ASSIGN":
        if parameters["scopeType"] != "TENANT" and not parameters.get("scopeRef"):
            raise AdminCommandValidationError(
                "scopeRef is required for ORG_UNIT and RESOURCE assignments."
            )
        valid_from = parameters.get("validFrom")
        valid_to = parameters.get("validTo")
        if valid_from and valid_to:
            start = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
            end = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))
            if end <= start:
                raise AdminCommandValidationError("validTo must be later than validFrom.")
    if command_key == "ACCESS.ROLE.PERMISSION.REPLACE":
        for item in parameters["permissions"]:
            if set(item) != {"resourceId", "permissionCode", "effect"}:
                raise AdminCommandValidationError(
                    "Each permission requires resourceId, permissionCode, and effect only."
                )
            if not isinstance(item["resourceId"], int) or isinstance(item["resourceId"], bool):
                raise AdminCommandValidationError("permission resourceId must be an integer.")
            if item["effect"] not in {"ALLOW", "DENY"}:
                raise AdminCommandValidationError("permission effect must be ALLOW or DENY.")
            if not isinstance(item["permissionCode"], str) or not item["permissionCode"].strip():
                raise AdminCommandValidationError("permissionCode must be a non-blank string.")
    if command_key == "NAVIGATION.ORDER.UPDATE":
        for item in parameters["items"]:
            if set(item) != {"navigationItemId", "parentNavigationItemId", "sortOrder", "version"}:
                raise AdminCommandValidationError(
                    "Each navigation order item must match the versioned reorder contract."
                )
            integer_fields = ("navigationItemId", "sortOrder", "version")
            if any(
                not isinstance(item[field], int) or isinstance(item[field], bool)
                for field in integer_fields
            ):
                raise AdminCommandValidationError("Navigation order numeric fields must be integers.")
            parent = item["parentNavigationItemId"]
            if parent is not None and (not isinstance(parent, int) or isinstance(parent, bool)):
                raise AdminCommandValidationError("parentNavigationItemId must be integer or null.")


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
