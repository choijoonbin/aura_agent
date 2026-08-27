from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping
from uuid import UUID


ASSERTION_HEADER = "X-DWP-Delegated-Identity"
_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_OPTIONAL_CLAIMS = {
    "pid": "X-DWP-Person-Public-ID",
    "dn": "X-DWP-Display-Name-B64",
    "sid": "X-DWP-Auth-Session-ID",
    "ip": "X-DWP-Identity-Plane",
}


class DelegatedIdentityError(RuntimeError):
    pass


def verify_delegated_identity(
    *,
    assertion: str,
    secret: str,
    method: str,
    path: str,
    headers: Mapping[str, str],
    now: int | None = None,
    key_id: str = "gateway-agent-v1",
) -> None:
    encoded_header, encoded_payload, encoded_signature = _segments(assertion)
    signed = f"{encoded_header}.{encoded_payload}".encode("ascii")
    expected = _encode(hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest())
    if not hmac.compare_digest(encoded_signature, expected):
        raise DelegatedIdentityError("Delegated identity assertion is invalid.")

    protected = _json_segment(encoded_header)
    claims = _json_segment(encoded_payload)
    if protected != {"alg": "HS256", "kid": key_id, "typ": "dwp-identity+jwt"}:
        raise DelegatedIdentityError("Delegated identity assertion is invalid.")

    current = int(time.time()) if now is None else now
    issued_at = _integer_claim(claims, "iat")
    not_before = _integer_claim(claims, "nbf")
    expires_at = _integer_claim(claims, "exp")
    if not_before > current + 5 or issued_at > current + 5 or expires_at < current:
        raise DelegatedIdentityError("Delegated identity assertion is expired or not active.")
    if expires_at <= issued_at or expires_at - issued_at > 30:
        raise DelegatedIdentityError("Delegated identity assertion lifetime is invalid.")
    try:
        UUID(str(claims["jti"]))
    except (KeyError, TypeError, ValueError) as error:
        raise DelegatedIdentityError("Delegated identity assertion is invalid.") from error

    expected_claims = {
        "iss": "dwp-gateway",
        "aud": "dwp-agent",
        "sub": headers.get("X-DWP-User-ID"),
        "tid": headers.get("X-DWP-Tenant-ID"),
        "cid": headers.get("X-Correlation-ID"),
        "htm": method.upper(),
        "htu": path,
        "roles": list(_header_values(headers.get("X-DWP-Roles"))),
        "permissions": list(_header_values(headers.get("X-DWP-Permissions"))),
    }
    resource_roles_header = headers.get("X-DWP-Resource-Roles")
    if resource_roles_header is not None or "resourceRoles" in claims:
        expected_claims["resourceRoles"] = list(_header_values(resource_roles_header))
    for claim, expected_value in expected_claims.items():
        if claims.get(claim) != expected_value:
            raise DelegatedIdentityError("Delegated identity assertion does not match the request.")
    for claim, header in _OPTIONAL_CLAIMS.items():
        if claims.get(claim) != headers.get(header):
            raise DelegatedIdentityError("Delegated identity assertion does not match the request.")


def _segments(assertion: str) -> tuple[str, str, str]:
    parts = assertion.split(".")
    if len(parts) != 3 or any(not _SEGMENT.fullmatch(part) for part in parts):
        raise DelegatedIdentityError("Delegated identity assertion is invalid.")
    return parts[0], parts[1], parts[2]


def _json_segment(segment: str) -> dict[str, object]:
    try:
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        value = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise DelegatedIdentityError("Delegated identity assertion is invalid.") from error
    if not isinstance(value, dict):
        raise DelegatedIdentityError("Delegated identity assertion is invalid.")
    return value


def _integer_claim(claims: Mapping[str, object], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DelegatedIdentityError("Delegated identity assertion is invalid.")
    return value


def _header_values(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(sorted({item.strip().upper() for item in value.split(",") if item.strip()}))


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
