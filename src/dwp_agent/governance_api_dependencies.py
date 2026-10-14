from __future__ import annotations

from typing import Annotated

from fastapi import Header, HTTPException, status

from .security import header_values


def governance_context(
    tenant_id: str,
    user_id: str,
    correlation_id: str,
    permissions: str | None,
    resource: str,
    *allowed: str,
) -> tuple[str, str, str]:
    authorities = set(header_values(permissions))
    if f"{resource}:MANAGE" not in authorities and not any(
        f"{resource}:{permission}" in authorities for permission in allowed
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{resource} permission is required.",
        )
    return tenant_id, user_id, correlation_id


def governance_headers(
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1)],
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
) -> tuple[str, str, str, str | None]:
    return tenant_id, user_id, correlation_id, permissions
