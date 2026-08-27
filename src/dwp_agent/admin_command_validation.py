from __future__ import annotations

from datetime import datetime, timezone
from re import fullmatch
from typing import Any


class AdminCommandValidationError(ValueError):
    """Raised when an administration command does not match the allow-list."""


def validate_admin_command_semantics(
    command_key: str,
    parameters: dict[str, Any],
) -> None:
    _validate_future_instant(parameters)
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
            if not isinstance(item["resourceId"], int) or isinstance(
                item["resourceId"], bool
            ):
                raise AdminCommandValidationError(
                    "permission resourceId must be an integer."
                )
            if item["effect"] not in {"ALLOW", "DENY"}:
                raise AdminCommandValidationError(
                    "permission effect must be ALLOW or DENY."
                )
            if (
                not isinstance(item["permissionCode"], str)
                or fullmatch(r"[A-Z][A-Z0-9_.-]{0,49}", item["permissionCode"])
                is None
            ):
                raise AdminCommandValidationError(
                    "permissionCode must match the Auth permission contract."
                )
    if command_key == "NAVIGATION.ORDER.UPDATE":
        for item in parameters["items"]:
            if set(item) != {
                "navigationItemId",
                "parentNavigationItemId",
                "sortOrder",
                "version",
            }:
                raise AdminCommandValidationError(
                    "Each navigation order item must match the versioned reorder contract."
                )
            integer_fields = ("navigationItemId", "sortOrder", "version")
            if any(
                not isinstance(item[field], int) or isinstance(item[field], bool)
                for field in integer_fields
            ):
                raise AdminCommandValidationError(
                    "Navigation order numeric fields must be integers."
                )
            if item["sortOrder"] < 0 or item["version"] < 0:
                raise AdminCommandValidationError(
                    "Navigation order sortOrder and version must be non-negative."
                )
            parent = item["parentNavigationItemId"]
            if parent is not None and (
                not isinstance(parent, int) or isinstance(parent, bool)
            ):
                raise AdminCommandValidationError(
                    "parentNavigationItemId must be integer or null."
                )


def _validate_future_instant(parameters: dict[str, Any]) -> None:
    valid_to = parameters.get("validTo")
    if not valid_to:
        return
    try:
        expires_at = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))
    except ValueError as error:
        raise AdminCommandValidationError(
            "validTo must be a valid UTC instant."
        ) from error
    if expires_at <= datetime.now(timezone.utc):
        raise AdminCommandValidationError("validTo must be a future UTC instant.")
