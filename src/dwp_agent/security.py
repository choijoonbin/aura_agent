from __future__ import annotations

import os
from hmac import compare_digest
from typing import Annotated

from fastapi import Header, HTTPException, Request, status

from .delegated_identity import (
    ASSERTION_HEADER,
    DelegatedIdentityError,
    verify_delegated_identity,
)
from .policy import AskIdentity


SERVICE_TOKEN_HEADER = "X-DWP-Service-Token"


def require_gateway_service(
    request: Request,
    service_token: Annotated[
        str | None,
        Header(alias=SERVICE_TOKEN_HEADER, include_in_schema=False),
    ] = None,
    delegated_identity: Annotated[
        str | None,
        Header(alias=ASSERTION_HEADER, include_in_schema=False),
    ] = None,
) -> None:
    expected = os.getenv("DWP_AGENT_SERVICE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent service identity is not configured.",
        )
    if service_token is None or not compare_digest(service_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Agent service identity.",
        )
    signing_secret = os.getenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", "").strip()
    if not signing_secret:
        return
    if delegated_identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Signed delegated identity is required.",
        )
    try:
        verify_delegated_identity(
            assertion=delegated_identity,
            secret=signing_secret,
            method=request.method,
            path=request.url.path,
            headers=request.headers,
            key_id=os.getenv("DWP_AGENT_IDENTITY_KEY_ID", "gateway-agent-v1").strip(),
        )
    except DelegatedIdentityError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid delegated identity assertion.",
        ) from error


def verified_ask_identity(
    user_id: Annotated[str, Header(alias="X-DWP-User-ID", min_length=1)],
    tenant_id: Annotated[str, Header(alias="X-DWP-Tenant-ID", min_length=1)],
    correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=1)],
    roles: Annotated[str | None, Header(alias="X-DWP-Roles")] = None,
    permissions: Annotated[str | None, Header(alias="X-DWP-Permissions")] = None,
    person_public_id: Annotated[
        str | None, Header(alias="X-DWP-Person-Public-ID", max_length=36)
    ] = None,
    display_name_b64: Annotated[
        str | None, Header(alias="X-DWP-Display-Name-B64", max_length=400)
    ] = None,
) -> AskIdentity:
    return AskIdentity(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=header_values(roles),
        permissions=header_values(permissions),
        correlation_id=correlation_id,
        person_public_id=person_public_id,
        display_name_b64=display_name_b64,
    )


def header_values(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(
        sorted({item.strip().upper() for item in value.split(",") if item.strip()})
    )
