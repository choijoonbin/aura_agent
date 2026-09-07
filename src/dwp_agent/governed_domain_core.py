from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Mapping

from psycopg import Connection

from .canonical_json import canonical_json_bytes
from .envelope import KeyContext, PayloadEncryption, load_payload_encryption
from .key_provider import load_versioned_key_material


class GovernedDomainConflict(RuntimeError):
    pass


class GovernedDomainNotFound(RuntimeError):
    pass


class GovernedDomainUnavailable(RuntimeError):
    pass


class GovernedDomainForbidden(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandProof:
    session_fingerprint: str
    request_fingerprint: str


class GovernedFingerprints:
    """Purpose-separated keyed fingerprints backed by the configured Agent key."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("Domain fingerprint key is too short.")
        self._key = hmac.new(
            key,
            b"dwp-agent-governed-domains-v1",
            hashlib.sha256,
        ).digest()

    @classmethod
    def load(cls) -> "GovernedFingerprints":
        material = load_versioned_key_material()
        return cls(base64.b64decode(material.active_key, validate=True))

    @classmethod
    def ephemeral(cls, key: bytes = b"dwp-domain-test-key-material-32!!") -> "GovernedFingerprints":
        return cls(key)

    def value(
        self,
        *,
        tenant_id: int,
        purpose: str,
        payload: Mapping[str, object],
    ) -> str:
        framed = canonical_json_bytes(
            {
                "profile": "dwp-domain-hmac-v1",
                "tenantId": tenant_id,
                "purpose": purpose,
                "payload": dict(payload),
            }
        )
        return hmac.new(self._key, framed, hashlib.sha256).hexdigest()

    def command(
        self,
        *,
        tenant_id: int,
        session_id: str,
        purpose: str,
        payload: Mapping[str, object],
    ) -> CommandProof:
        return CommandProof(
            session_fingerprint=self.value(
                tenant_id=tenant_id,
                purpose="auth-session",
                payload={"sessionId": session_id},
            ),
            request_fingerprint=self.value(
                tenant_id=tenant_id,
                purpose=purpose,
                payload=payload,
            ),
        )

    @staticmethod
    def matches(left: str, right: str) -> bool:
        return hmac.compare_digest(left, right)


class GovernedPayloadCodec:
    def __init__(self, encryption: PayloadEncryption | None = None) -> None:
        self._encryption = encryption or load_payload_encryption()

    def encrypt_json(
        self,
        payload: Mapping[str, object],
        *,
        tenant_id: int,
        resource_type: str,
        resource_id: str,
        field: str,
    ) -> str:
        return self._encryption.encrypt_bytes(
            canonical_json_bytes(payload),
            KeyContext.payload(
                tenant_id=tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                field=field,
            ),
        )

    def decrypt_json(
        self,
        envelope: str,
        *,
        tenant_id: int,
        resource_type: str,
        resource_id: str,
        field: str,
    ) -> dict[str, object]:
        raw = self._encryption.decrypt_bytes(
            envelope=envelope,
            context=KeyContext.payload(
                tenant_id=tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                field=field,
            ),
            legacy_version=None,
            legacy_nonce=None,
            legacy_ciphertext=None,
            legacy_aad=b"",
        )
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise GovernedDomainUnavailable("Encrypted domain payload is invalid.")
        return parsed


def tenant_number(value: str | int) -> int:
    try:
        tenant_id = int(value)
    except (TypeError, ValueError) as error:
        raise GovernedDomainForbidden("Tenant identity is not canonical.") from error
    if tenant_id <= 0:
        raise GovernedDomainForbidden("Tenant identity is not canonical.")
    return tenant_id


def advisory_lock(connection: Connection[object], *parts: object) -> None:
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).digest()
    lock_id = int.from_bytes(digest[:8], byteorder="big", signed=True)
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))


def require_command_replay(
    stored_session: str,
    stored_request: str,
    proof: CommandProof,
) -> None:
    if not (
        GovernedFingerprints.matches(stored_session, proof.session_fingerprint)
        and GovernedFingerprints.matches(stored_request, proof.request_fingerprint)
    ):
        raise GovernedDomainConflict("The command ID is already bound to another request.")


def retention_deadline(now: datetime, retention_days: int) -> datetime:
    return now.astimezone(UTC) + timedelta(days=retention_days)


def iso_value(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None
