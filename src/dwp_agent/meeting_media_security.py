from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from typing import Any, Callable, Protocol
from uuid import UUID

import psycopg


ASSERTION_HEADER = "X-DWP-Meeting-Workload-Assertion"
RECORDING_TOKEN_HEADER = "X-DWP-Meeting-Recording-Token"
TRANSCRIPT_TOKEN_HEADER = "X-DWP-Meeting-Transcript-Token"

_RESOURCE_FIELDS = frozenset(
    {
        "v",
        "kid",
        "method",
        "path",
        "tenantId",
        "meetingId",
        "runId",
        "iat",
        "exp",
        "jti",
        "bodySha256",
    }
)
_SERVICE_FIELDS = frozenset(
    {
        "v",
        "kid",
        "scope",
        "method",
        "path",
        "iat",
        "exp",
        "jti",
        "bodySha256",
    }
)
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


class MeetingMediaPurpose(StrEnum):
    RECORDING = "RECORDING"
    TRANSCRIPT = "TRANSCRIPT"


class MeetingMediaIdentityError(RuntimeError):
    pass


class MeetingMediaReplayStore(Protocol):
    def consume(
        self,
        *,
        purpose: MeetingMediaPurpose,
        scope: str,
        jti: UUID,
        expires_at: datetime,
        tenant_id: int | None,
        meeting_id: UUID | None,
        resource_id: UUID | None,
        body_sha256: str,
    ) -> bool: ...


@dataclass(frozen=True)
class MeetingMediaIdentityConfiguration:
    purpose: MeetingMediaPurpose
    service_token: str = field(repr=False)
    signing_secret: bytes = field(repr=False)
    key_id: str
    maximum_ttl_seconds: int = 60
    clock_skew_seconds: int = 5

    @classmethod
    def from_environment(
        cls, purpose: MeetingMediaPurpose
    ) -> "MeetingMediaIdentityConfiguration":
        prefix = (
            "DWP_MEETING_RECORDING"
            if purpose is MeetingMediaPurpose.RECORDING
            else "DWP_MEETING_TRANSCRIPT"
        )
        token = os.getenv(f"{prefix}_SERVICE_TOKEN", "").strip()
        encoded_secret = os.getenv(f"{prefix}_ASSERTION_SECRET_BASE64", "").strip()
        key_id = os.getenv(f"{prefix}_ASSERTION_KEY_ID", "").strip()
        try:
            secret = base64.b64decode(encoded_secret, validate=True)
        except (ValueError, binascii.Error) as error:
            raise MeetingMediaIdentityError(
                f"{purpose.value.title()} workload identity is not configured."
            ) from error
        if (
            len(token) < 32
            or len(token) > 4_096
            or any(ord(character) < 32 or ord(character) == 127 for character in token)
            or not 32 <= len(secret) <= 128
            or not _KEY_ID.fullmatch(key_id)
        ):
            raise MeetingMediaIdentityError(
                f"{purpose.value.title()} workload identity is not configured."
            )
        return cls(
            purpose=purpose,
            service_token=token,
            signing_secret=secret,
            key_id=key_id,
        )


class MeetingMediaAssertionVerifier:
    def __init__(
        self,
        configuration: MeetingMediaIdentityConfiguration,
        replay_store: MeetingMediaReplayStore,
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.configuration = configuration
        self.replay_store = replay_store
        self.now = now or time.time

    def verify_service(
        self,
        *,
        service_token: str | None,
        assertion: str | None,
        method: str,
        path: str,
        body: bytes,
    ) -> None:
        payload = self._verified_payload(
            service_token=service_token,
            assertion=assertion,
            expected_fields=_SERVICE_FIELDS,
            method=method,
            path=path,
            body=body,
        )
        if payload.get("scope") != "SERVICE":
            raise MeetingMediaIdentityError("Invalid meeting media workload identity.")
        self._consume(payload, scope="SERVICE", body=body)

    def verify_resource(
        self,
        *,
        service_token: str | None,
        assertion: str | None,
        method: str,
        path: str,
        tenant_id: int,
        meeting_id: UUID,
        resource_id: UUID,
        body: bytes,
    ) -> None:
        payload = self._verified_payload(
            service_token=service_token,
            assertion=assertion,
            expected_fields=_RESOURCE_FIELDS,
            method=method,
            path=path,
            body=body,
        )
        try:
            assertion_meeting_id = UUID(_string(payload, "meetingId"))
            assertion_resource_id = UUID(_string(payload, "runId"))
        except (ValueError, AttributeError) as error:
            raise MeetingMediaIdentityError(
                "Invalid meeting media workload identity."
            ) from error
        if (
            type(payload.get("tenantId")) is not int
            or payload["tenantId"] != tenant_id
            or assertion_meeting_id != meeting_id
            or assertion_resource_id != resource_id
        ):
            raise MeetingMediaIdentityError("Invalid meeting media workload identity.")
        self._consume(
            payload,
            scope="RESOURCE",
            body=body,
            tenant_id=tenant_id,
            meeting_id=meeting_id,
            resource_id=resource_id,
        )

    def _verified_payload(
        self,
        *,
        service_token: str | None,
        assertion: str | None,
        expected_fields: frozenset[str],
        method: str,
        path: str,
        body: bytes,
    ) -> dict[str, Any]:
        if service_token is None or not hmac.compare_digest(
            service_token, self.configuration.service_token
        ):
            raise MeetingMediaIdentityError("Invalid meeting media workload identity.")
        signing_input, payload_part, signature_part = _parts(assertion)
        expected_signature = hmac.new(
            self.configuration.signing_secret,
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_decode(signature_part), expected_signature):
            raise MeetingMediaIdentityError("Invalid meeting media workload identity.")
        payload = _payload(payload_part, expected_fields)
        now = int(self.now())
        try:
            issued_at = _integer(payload, "iat")
            expires_at = _integer(payload, "exp")
        except (KeyError, TypeError, ValueError) as error:
            raise MeetingMediaIdentityError(
                "Invalid meeting media workload identity."
            ) from error
        matches = (
            payload.get("v") == 1
            and payload.get("kid") == self.configuration.key_id
            and payload.get("method") == method.upper()
            and payload.get("path") == path
            and payload.get("bodySha256") == hashlib.sha256(body).hexdigest()
            and issued_at <= now + self.configuration.clock_skew_seconds
            and expires_at > now
            and expires_at - issued_at <= self.configuration.maximum_ttl_seconds
            and issued_at >= now - self.configuration.maximum_ttl_seconds
        )
        if not matches:
            raise MeetingMediaIdentityError("Invalid meeting media workload identity.")
        return payload

    def _consume(
        self,
        payload: dict[str, Any],
        *,
        scope: str,
        body: bytes,
        tenant_id: int | None = None,
        meeting_id: UUID | None = None,
        resource_id: UUID | None = None,
    ) -> None:
        try:
            jti = UUID(_string(payload, "jti"))
            expires_at = datetime.fromtimestamp(_integer(payload, "exp"), tz=UTC)
        except (ValueError, AttributeError, OSError) as error:
            raise MeetingMediaIdentityError(
                "Invalid meeting media workload identity."
            ) from error
        if not self.replay_store.consume(
            purpose=self.configuration.purpose,
            scope=scope,
            jti=jti,
            expires_at=expires_at,
            tenant_id=tenant_id,
            meeting_id=meeting_id,
            resource_id=resource_id,
            body_sha256=hashlib.sha256(body).hexdigest(),
        ):
            raise MeetingMediaIdentityError(
                "Meeting media workload assertion was already used."
            )


class PostgresMeetingMediaReplayStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def consume(self, **values: object) -> bool:
        values = {**values, "purpose": str(values["purpose"])}
        try:
            with psycopg.connect(self.database_url, autocommit=False) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM agent_meeting_media_assertion_replay "
                        "WHERE expires_at <= CURRENT_TIMESTAMP"
                    )
                    cursor.execute(
                        """
                        INSERT INTO agent_meeting_media_assertion_replay (
                            purpose, assertion_scope, jti, expires_at, tenant_id,
                            meeting_id, resource_id, body_sha256)
                        VALUES (%(purpose)s, %(scope)s, %(jti)s, %(expires_at)s,
                                %(tenant_id)s, %(meeting_id)s, %(resource_id)s,
                                %(body_sha256)s)
                        ON CONFLICT (purpose, jti) DO NOTHING
                        """,
                        values,
                    )
                    inserted = cursor.rowcount == 1
                connection.commit()
            return inserted
        except psycopg.Error as error:
            raise MeetingMediaIdentityError(
                "Meeting media replay protection is unavailable."
            ) from error


class InMemoryMeetingMediaReplayStore:
    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._lock = threading.Lock()
        self._expires: dict[tuple[MeetingMediaPurpose, UUID], datetime] = {}
        self._now = now or (lambda: datetime.now(UTC))

    def consume(self, **values: object) -> bool:
        purpose = values["purpose"]
        jti = values["jti"]
        expires_at = values["expires_at"]
        if (
            not isinstance(purpose, MeetingMediaPurpose)
            or not isinstance(jti, UUID)
            or not isinstance(expires_at, datetime)
        ):
            return False
        now = self._now()
        key = (purpose, jti)
        with self._lock:
            self._expires = {
                item: expiry for item, expiry in self._expires.items() if expiry > now
            }
            if key in self._expires:
                return False
            self._expires[key] = expires_at
            return True


@lru_cache(maxsize=1)
def meeting_media_replay_store() -> MeetingMediaReplayStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if database_url:
        return PostgresMeetingMediaReplayStore(database_url)
    if os.getenv("DWP_ENVIRONMENT", "local").strip().lower() in {"local", "test"}:
        return InMemoryMeetingMediaReplayStore()
    raise MeetingMediaIdentityError("Meeting media replay protection is unavailable.")


def _parts(assertion: str | None) -> tuple[str, str, str]:
    if assertion is None or assertion.count(".") != 2 or len(assertion) > 4_096:
        raise MeetingMediaIdentityError("Invalid meeting media workload identity.")
    version, payload, signature = assertion.split(".", 2)
    if version != "dwp1" or not payload or not signature:
        raise MeetingMediaIdentityError("Invalid meeting media workload identity.")
    return f"{version}.{payload}", payload, signature


def _payload(value: str, expected_fields: frozenset[str]) -> dict[str, Any]:
    try:
        decoded = json.loads(
            _decode(value),
            object_pairs_hook=_unique_object,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MeetingMediaIdentityError(
            "Invalid meeting media workload identity."
        ) from error
    if not isinstance(decoded, dict) or frozenset(decoded) != expected_fields:
        raise MeetingMediaIdentityError("Invalid meeting media workload identity.")
    return decoded


def _decode(value: str) -> bytes:
    if not value or "=" in value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise MeetingMediaIdentityError("Invalid meeting media workload identity.")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, TypeError, binascii.Error) as error:
        raise MeetingMediaIdentityError(
            "Invalid meeting media workload identity."
        ) from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise ValueError(key)
    return value


def _integer(payload: dict[str, Any], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise ValueError(key)
    return value
