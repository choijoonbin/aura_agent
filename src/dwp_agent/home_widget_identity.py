from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import lru_cache
from typing import Callable, Protocol
from uuid import UUID

import psycopg


ASSERTION_HEADER = "X-DWP-Home-Assertion"
DEFAULT_KEY_ID = "platform-dwaion-home-v1"
_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


class HomeDelegatedIdentityError(RuntimeError):
    pass


class HomeIdentityReplayUnavailable(HomeDelegatedIdentityError):
    pass


class HomeIdentityReplayStore(Protocol):
    def consume(
        self,
        *,
        jti: UUID,
        tenant_id: int,
        user_id: str,
        body_sha256: str,
        expires_at: datetime,
    ) -> bool: ...


def verify_home_delegated_identity(
    *,
    assertion: str,
    secret: str,
    method: str,
    path: str,
    body: bytes,
    headers: Mapping[str, str],
    replay_store: HomeIdentityReplayStore,
    now: int | None = None,
    key_id: str = DEFAULT_KEY_ID,
) -> None:
    signed_value, encoded_payload, encoded_signature = _segments(assertion)
    signed = signed_value.encode("ascii")
    expected_signature = _encode(
        hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
    )
    if not hmac.compare_digest(encoded_signature, expected_signature):
        raise HomeDelegatedIdentityError("Home delegated identity is invalid.")

    claims = _json_segment(encoded_payload)
    current = int(time.time()) if now is None else now
    issued_at = _integer_claim(claims, "iat")
    not_before = _integer_claim(claims, "nbf")
    expires_at = _integer_claim(claims, "exp")
    if not_before > current + 5 or issued_at > current + 5 or expires_at <= current:
        raise HomeDelegatedIdentityError(
            "Home delegated identity is expired or not active."
        )
    if expires_at <= issued_at or expires_at - issued_at > 30:
        raise HomeDelegatedIdentityError("Home delegated identity lifetime is invalid.")
    try:
        jti = UUID(str(claims["jti"]))
    except (KeyError, TypeError, ValueError) as error:
        raise HomeDelegatedIdentityError("Home delegated identity is invalid.") from error

    expected = {
        "v": 1,
        "kid": key_id,
        "iss": "dwp-platform-server",
        "aud": "dwp-agent-home",
        "sub": headers.get("X-DWP-User-ID"),
        "tid": headers.get("X-DWP-Tenant-ID"),
        "pid": headers.get("X-DWP-Person-Public-ID"),
        "cid": headers.get("X-Correlation-ID"),
        "ip": headers.get("X-DWP-Identity-Plane"),
        "htm": method.upper(),
        "htu": path,
        "permissions": _values(headers.get("X-DWP-Permissions")),
        "roles": _values(headers.get("X-DWP-Roles")),
        "groups": _values(headers.get("X-DWP-Group-Refs"), uppercase=False),
        "authorityRevision": headers.get("X-DWP-Current-Decision-Revision"),
        "authorityRevalidateAt": headers.get("X-DWP-Current-Revalidate-At"),
        "deadlineAt": headers.get("X-DWP-Home-Deadline-At"),
        "bodySha256": hashlib.sha256(body).hexdigest(),
    }
    for name, expected_value in expected.items():
        if claims.get(name) != expected_value:
            raise HomeDelegatedIdentityError(
                "Home delegated identity does not match the request."
            )
    allowed = {*expected, "iat", "nbf", "exp", "jti"}
    if set(claims) != allowed:
        raise HomeDelegatedIdentityError("Home delegated identity is invalid.")
    try:
        deadline = datetime.fromisoformat(str(claims["deadlineAt"]).replace("Z", "+00:00"))
        revalidate = datetime.fromisoformat(
            str(claims["authorityRevalidateAt"]).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise HomeDelegatedIdentityError("Home delegated identity is invalid.") from error
    if expires_at > math.ceil(deadline.timestamp()) or expires_at > math.ceil(
        revalidate.timestamp()
    ):
        raise HomeDelegatedIdentityError(
            "Home delegated identity outlives its authority or request deadline."
        )
    try:
        tenant_id = int(str(claims["tid"]))
    except (TypeError, ValueError) as error:
        raise HomeDelegatedIdentityError("Home delegated identity is invalid.") from error
    if not replay_store.consume(
        jti=jti,
        tenant_id=tenant_id,
        user_id=str(claims["sub"]),
        body_sha256=str(claims["bodySha256"]),
        expires_at=datetime.fromtimestamp(expires_at, tz=UTC),
    ):
        raise HomeDelegatedIdentityError(
            "Home delegated identity assertion was already used."
        )


class PostgresHomeIdentityReplayStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def consume(self, **values: object) -> bool:
        try:
            with psycopg.connect(self.database_url, autocommit=False) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """DELETE FROM agent_home_identity_assertion_replay
                              WHERE jti IN (
                                  SELECT jti
                                    FROM agent_home_identity_assertion_replay
                                   WHERE expires_at <= CURRENT_TIMESTAMP
                                   ORDER BY expires_at
                                   LIMIT 1000
                              )"""
                    )
                    cursor.execute(
                        """
                        INSERT INTO agent_home_identity_assertion_replay (
                            jti, tenant_id, user_id, body_sha256, expires_at)
                        VALUES (%(jti)s, %(tenant_id)s, %(user_id)s,
                                %(body_sha256)s, %(expires_at)s)
                        ON CONFLICT (jti) DO NOTHING
                        """,
                        values,
                    )
                    inserted = cursor.rowcount == 1
                connection.commit()
            return inserted
        except psycopg.Error as error:
            raise HomeIdentityReplayUnavailable(
                "Home delegated identity replay protection is unavailable."
            ) from error


class InMemoryHomeIdentityReplayStore:
    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._lock = threading.Lock()
        self._expires: dict[UUID, datetime] = {}
        self._now = now or (lambda: datetime.now(UTC))

    def consume(self, **values: object) -> bool:
        jti = values["jti"]
        expires_at = values["expires_at"]
        if not isinstance(jti, UUID) or not isinstance(expires_at, datetime):
            return False
        now = self._now()
        with self._lock:
            self._expires = {
                item: expiry for item, expiry in self._expires.items() if expiry > now
            }
            if jti in self._expires:
                return False
            self._expires[jti] = expires_at
            return True


@lru_cache(maxsize=1)
def home_identity_replay_store() -> HomeIdentityReplayStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if database_url:
        return PostgresHomeIdentityReplayStore(database_url)
    if os.getenv("DWP_ENVIRONMENT", "local").strip().lower() in {"local", "test"}:
        return InMemoryHomeIdentityReplayStore()
    raise HomeIdentityReplayUnavailable(
        "Home delegated identity replay protection is unavailable."
    )


def _values(value: str | None, *, uppercase: bool = True) -> list[str]:
    if not value:
        return []
    values = {
        item.strip().upper() if uppercase else item.strip()
        for item in value.split(",")
        if item.strip()
    }
    return sorted(values)


def _segments(assertion: str) -> tuple[str, str, str]:
    parts = assertion.split(".")
    if len(parts) != 3 or any(not _SEGMENT.fullmatch(part) for part in parts):
        raise HomeDelegatedIdentityError("Home delegated identity is invalid.")
    if parts[0] != "dwp1":
        raise HomeDelegatedIdentityError("Home delegated identity is invalid.")
    return f"{parts[0]}.{parts[1]}", parts[1], parts[2]


def _json_segment(segment: str) -> dict[str, object]:
    try:
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        value = json.loads(decoded, object_pairs_hook=_unique_object)
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise HomeDelegatedIdentityError("Home delegated identity is invalid.") from error
    if not isinstance(value, dict):
        raise HomeDelegatedIdentityError("Home delegated identity is invalid.")
    return value


def _integer_claim(claims: Mapping[str, object], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HomeDelegatedIdentityError("Home delegated identity is invalid.")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
