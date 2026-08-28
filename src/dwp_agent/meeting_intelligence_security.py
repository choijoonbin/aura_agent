from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Callable, Protocol
from uuid import UUID

import psycopg


ASSERTION_HEADER = "X-DWP-Meeting-Workload-Assertion"
TOKEN_HEADER = "X-DWP-Meeting-Intelligence-Token"
ASSERTION_FIELDS = frozenset(
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


class MeetingWorkloadIdentityError(RuntimeError):
    pass


class MeetingAssertionReplayStore(Protocol):
    def consume(
        self,
        *,
        jti: UUID,
        expires_at: datetime,
        tenant_id: int,
        meeting_id: UUID,
        run_id: UUID,
        body_sha256: str,
    ) -> bool: ...


@dataclass(frozen=True)
class MeetingWorkloadIdentityConfiguration:
    service_token: str
    signing_secret: bytes
    key_id: str
    maximum_ttl_seconds: int = 60
    clock_skew_seconds: int = 5

    @classmethod
    def from_environment(cls) -> "MeetingWorkloadIdentityConfiguration":
        token = os.getenv("DWP_MEETING_INTELLIGENCE_SERVICE_TOKEN", "").strip()
        secret_base64 = os.getenv(
            "DWP_MEETING_INTELLIGENCE_ASSERTION_SECRET_BASE64", ""
        ).strip()
        key_id = os.getenv(
            "DWP_MEETING_INTELLIGENCE_ASSERTION_KEY_ID", "meeting-workload-v1"
        ).strip()
        try:
            secret = base64.b64decode(secret_base64, validate=True)
        except (ValueError, binascii.Error) as error:
            raise MeetingWorkloadIdentityError(
                "Meeting intelligence workload identity is not configured."
            ) from error
        if len(token) < 32 or not 32 <= len(secret) <= 128:
            raise MeetingWorkloadIdentityError(
                "Meeting intelligence workload identity is not configured."
            )
        if not key_id or len(key_id) > 80 or not all(
            character.isalnum() or character in "._-" for character in key_id
        ):
            raise MeetingWorkloadIdentityError(
                "Meeting intelligence workload identity is not configured."
            )
        return cls(service_token=token, signing_secret=secret, key_id=key_id)


class MeetingWorkloadAssertionVerifier:
    def __init__(
        self,
        configuration: MeetingWorkloadIdentityConfiguration,
        replay_store: MeetingAssertionReplayStore,
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.configuration = configuration
        self.replay_store = replay_store
        self.now = now or time.time

    def verify(
        self,
        *,
        service_token: str | None,
        assertion: str | None,
        method: str,
        path: str,
        tenant_id: int,
        meeting_id: UUID,
        run_id: UUID,
        body: bytes,
    ) -> None:
        if service_token is None or not hmac.compare_digest(
            service_token, self.configuration.service_token
        ):
            raise MeetingWorkloadIdentityError("Invalid meeting workload identity.")
        signing_input, payload_part, signature_part = _parts(assertion)
        expected_signature = hmac.new(
            self.configuration.signing_secret,
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_decode(signature_part), expected_signature):
            raise MeetingWorkloadIdentityError("Invalid meeting workload identity.")
        payload = _payload(payload_part)
        now = int(self.now())
        expected_body_hash = hashlib.sha256(body).hexdigest()
        try:
            assertion_meeting_id = UUID(payload["meetingId"])
            assertion_run_id = UUID(payload["runId"])
            jti = UUID(payload["jti"])
            issued_at = int(payload["iat"])
            expires_at = int(payload["exp"])
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise MeetingWorkloadIdentityError("Invalid meeting workload identity.") from error
        matches = (
            payload.get("v") == 1
            and payload.get("kid") == self.configuration.key_id
            and payload.get("method") == method.upper()
            and payload.get("path") == path
            and payload.get("tenantId") == tenant_id
            and assertion_meeting_id == meeting_id
            and assertion_run_id == run_id
            and payload.get("bodySha256") == expected_body_hash
            and issued_at <= now + self.configuration.clock_skew_seconds
            and expires_at > now
            and expires_at - issued_at <= self.configuration.maximum_ttl_seconds
            and issued_at >= now - self.configuration.maximum_ttl_seconds
        )
        if not matches:
            raise MeetingWorkloadIdentityError("Invalid meeting workload identity.")
        consumed = self.replay_store.consume(
            jti=jti,
            expires_at=datetime.fromtimestamp(expires_at, tz=UTC),
            tenant_id=tenant_id,
            meeting_id=meeting_id,
            run_id=run_id,
            body_sha256=expected_body_hash,
        )
        if not consumed:
            raise MeetingWorkloadIdentityError("Meeting workload assertion was already used.")


class PostgresMeetingAssertionReplayStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def consume(self, **values: object) -> bool:
        try:
            with psycopg.connect(self.database_url, autocommit=False) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM agent_meeting_assertion_replay WHERE expires_at <= CURRENT_TIMESTAMP"
                    )
                    cursor.execute(
                        """
                        INSERT INTO agent_meeting_assertion_replay (
                            jti, expires_at, tenant_id, meeting_id, run_id, body_sha256)
                        VALUES (%(jti)s, %(expires_at)s, %(tenant_id)s,
                                %(meeting_id)s, %(run_id)s, %(body_sha256)s)
                        ON CONFLICT (jti) DO NOTHING
                        """,
                        values,
                    )
                    inserted = cursor.rowcount == 1
                connection.commit()
            return inserted
        except psycopg.Error as error:
            raise MeetingWorkloadIdentityError(
                "Meeting workload replay protection is unavailable."
            ) from error


class InMemoryMeetingAssertionReplayStore:
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
                key: expiry for key, expiry in self._expires.items() if expiry > now
            }
            if jti in self._expires:
                return False
            self._expires[jti] = expires_at
            return True


@lru_cache(maxsize=1)
def meeting_assertion_replay_store() -> MeetingAssertionReplayStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if database_url:
        return PostgresMeetingAssertionReplayStore(database_url)
    environment = os.getenv("DWP_ENVIRONMENT", "local").strip().lower()
    if environment in {"local", "test"}:
        return InMemoryMeetingAssertionReplayStore()
    raise MeetingWorkloadIdentityError(
        "Meeting workload replay protection is unavailable."
    )


def _parts(assertion: str | None) -> tuple[str, str, str]:
    if assertion is None or assertion.count(".") != 2 or len(assertion) > 4_096:
        raise MeetingWorkloadIdentityError("Invalid meeting workload identity.")
    version, payload, signature = assertion.split(".", 2)
    if version != "dwp1" or not payload or not signature:
        raise MeetingWorkloadIdentityError("Invalid meeting workload identity.")
    return f"{version}.{payload}", payload, signature


def _payload(value: str) -> dict[str, object]:
    try:
        payload = json.loads(_decode(value))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MeetingWorkloadIdentityError("Invalid meeting workload identity.") from error
    if not isinstance(payload, dict) or set(payload) != ASSERTION_FIELDS:
        raise MeetingWorkloadIdentityError("Invalid meeting workload identity.")
    return payload


def _decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError, binascii.Error) as error:
        raise MeetingWorkloadIdentityError("Invalid meeting workload identity.") from error
