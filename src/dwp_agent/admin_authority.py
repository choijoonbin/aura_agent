from __future__ import annotations

from typing import Any, Protocol


APP_GOVERNANCE_AUTHORITY = "APP_GOVERNANCE_CAPABILITY"
RESOURCE_SET_KEY_PATTERN = r"[A-Z][A-Z0-9_.-]{2,254}"
_APP_CATALOG_ADMIN = "APP_CATALOG_ADMIN"
_APP_OWNER = "APP_OWNER"
_APP_ACCESS_APPROVER = "APP_ACCESS_APPROVER"
_APP_ACCESS_MANAGER = "APP_ACCESS_MANAGER"


class AdminAuthorityDefinition(Protocol):
    command_key: str
    authority_kind: str
    required_permission: str | None
    required_roles: frozenset[str]
    identity_plane: str


class AdminPreflightDenied(RuntimeError):
    """Raised for a privacy-safe Agent preflight denial."""


def required_admin_preflight_authorities(
    definition: AdminAuthorityDefinition,
    parameters: dict[str, Any],
) -> tuple[str, ...]:
    """Return the Gateway evidence accepted only for Agent preflight."""
    if definition.authority_kind == "TENANT_PERMISSION":
        if definition.required_permission is None:
            raise RuntimeError(
                f"Command '{definition.command_key}' has no tenant permission."
            )
        return (f"TENANT_PERMISSION:{definition.required_permission}",)
    if definition.authority_kind == "PROVIDER_ROLE":
        return tuple(
            f"TENANT_ROLE:{role}" for role in sorted(definition.required_roles)
        )
    if definition.authority_kind != APP_GOVERNANCE_AUTHORITY:
        raise RuntimeError(
            f"Command '{definition.command_key}' has an unsupported authority kind."
        )

    scope_key = parameters["scopeResourceSetKey"]

    def scoped(responsibility: str) -> str:
        return f"RESOURCE_ROLE:{responsibility}@{scope_key}"

    catalog = f"TENANT_ROLE:{_APP_CATALOG_ADMIN}"
    if definition.command_key == "ACCESS.APP_RESPONSIBILITY.REQUEST":
        if parameters["responsibilityCode"] == _APP_OWNER:
            return (catalog,)
        return (catalog, scoped(_APP_OWNER))

    target_responsibility = parameters["targetResponsibilityCode"]
    if definition.command_key == "ACCESS.APP_RESPONSIBILITY.DECIDE":
        if target_responsibility == _APP_OWNER:
            return (catalog,)
        if target_responsibility == _APP_ACCESS_APPROVER:
            # Auth alone decides whether the independent first-approver
            # bootstrap exception is still valid for the authoritative target.
            return (catalog, scoped(_APP_ACCESS_APPROVER))
        return (scoped(_APP_ACCESS_APPROVER),)

    if definition.command_key == "ACCESS.APP_RESPONSIBILITY.REVOKE":
        if target_responsibility == _APP_OWNER:
            return (catalog,)
        return (scoped(_APP_ACCESS_MANAGER),)
    raise RuntimeError(
        f"Command '{definition.command_key}' has no app-governance preflight contract."
    )


def admin_preflight_allows(
    definition: AdminAuthorityDefinition,
    parameters: dict[str, Any],
    *,
    permissions: set[str],
    roles: set[str],
    resource_roles: set[str],
) -> bool:
    candidates = required_admin_preflight_authorities(definition, parameters)
    evidence = {f"TENANT_PERMISSION:{permission}" for permission in permissions}
    evidence.update(f"TENANT_ROLE:{role}" for role in roles)
    evidence.update(f"RESOURCE_ROLE:{resource_role}" for resource_role in resource_roles)
    return any(candidate in evidence for candidate in candidates)


def require_admin_preflight(
    definition: AdminAuthorityDefinition,
    parameters: dict[str, Any],
    *,
    identity_plane: str,
    permissions: set[str],
    roles: set[str],
    resource_roles: set[str],
) -> None:
    if identity_plane != definition.identity_plane:
        raise AdminPreflightDenied(
            "The administration command identity plane is not permitted."
        )
    if definition.authority_kind == "TENANT_PERMISSION" and (
        "APP.ASK:VIEW" not in permissions
        or definition.required_permission is None
        or definition.required_permission not in permissions
    ):
        raise AdminPreflightDenied(
            "The administration command permission is required."
        )
    if definition.authority_kind == APP_GOVERNANCE_AUTHORITY and (
        "APP.ASK:VIEW" not in permissions
        or not admin_preflight_allows(
            definition,
            parameters,
            permissions=permissions,
            roles=roles,
            resource_roles=resource_roles,
        )
    ):
        raise AdminPreflightDenied(
            "An application-governance authority candidate is required."
        )
    if definition.authority_kind == "PROVIDER_ROLE" and not (
        roles & definition.required_roles
    ):
        raise AdminPreflightDenied("The Provider administration role is required.")
