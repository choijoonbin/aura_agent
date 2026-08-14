from __future__ import annotations

import os
from hmac import compare_digest
from typing import Annotated

from fastapi import Header, HTTPException, status


SERVICE_TOKEN_HEADER = "X-DWP-Service-Token"


def require_gateway_service(
    service_token: Annotated[
        str | None,
        Header(alias=SERVICE_TOKEN_HEADER, include_in_schema=False),
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
