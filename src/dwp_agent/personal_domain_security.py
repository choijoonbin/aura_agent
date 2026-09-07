from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from .governed_domain_core import tenant_number
from .policy import AskIdentity
from .security import require_gateway_service, verified_ask_identity


@dataclass(frozen=True)
class PersonalDomainIdentity:
    tenant_id: int
    user_id: str
    correlation_id: str
    auth_session_id: str
    roles: frozenset[str]
    permissions: frozenset[str]

    def require(self, *permissions: str) -> None:
        missing = set(permissions) - self.permissions
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Required DWAI-ON personal-domain access is not present.",
            )


def require_personal_domain_identity(
    request: Request,
    identity: Annotated[AskIdentity, Depends(verified_ask_identity)],
    auth_session_id: Annotated[
        str, Header(alias="X-DWP-Auth-Session-ID", min_length=1, max_length=160)
    ],
) -> PersonalDomainIdentity:
    _single(request, "X-DWP-Tenant-ID", identity.tenant_id)
    _single(request, "X-DWP-User-ID", identity.user_id)
    _single(request, "X-Correlation-ID", identity.correlation_id)
    _single(request, "X-DWP-Auth-Session-ID", auth_session_id)
    if _single(request, "X-DWP-Identity-Plane", None) != "TENANT":
        _deny()
    if _single(request, "X-DWP-Access-Mode", None) != "NORMAL":
        _deny()
    if request.headers.getlist("X-DWP-Support-Session-ID"):
        _deny()
    roles = frozenset(role.strip().upper() for role in identity.roles)
    if any(role.startswith("PROVIDER_") for role in roles):
        _deny()
    _canonical(auth_session_id, 160)
    _canonical(identity.user_id, 160)
    _canonical(identity.correlation_id, 160)
    return PersonalDomainIdentity(
        tenant_id=tenant_number(identity.tenant_id),
        user_id=identity.user_id,
        correlation_id=identity.correlation_id,
        auth_session_id=auth_session_id,
        roles=roles,
        permissions=frozenset(value.strip().upper() for value in identity.permissions),
    )


personal_domain_dependencies = [Depends(require_gateway_service)]


def _single(request: Request, name: str, expected: str | None) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        _deny()
    value = values[0]
    _canonical(value, 256)
    if expected is not None and value != expected:
        _deny()
    return value


def _canonical(value: str, maximum: int) -> None:
    if (
        not value
        or value != value.strip()
        or len(value) > maximum
        or "," in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _deny()


def _deny() -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Personal DWAI-ON data is unavailable in this identity context.",
    )
