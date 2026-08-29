from __future__ import annotations

import base64
import binascii
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ATTESTATION_PREFIX = "dwpa1"
MAX_ATTESTATION_BYTES = 8 * 1_024
MAX_PAYLOAD_BYTES = 4 * 1_024
MAX_ATTESTATION_LIFETIME_SECONDS = 7 * 24 * 60 * 60
ATTESTATION_CLOCK_SKEW_SECONDS = 60

_EXPECTED_FIELDS = frozenset(
    {
        "v",
        "kid",
        "attestationId",
        "providerCode",
        "model",
        "processingRegion",
        "customerDataTrainingDisabled",
        "providerRetentionDisabled",
        "policySha256",
        "issuedAt",
        "expiresAt",
    }
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MeetingIntelligenceAttestationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedMeetingIntelligenceAttestation:
    attestation_id: UUID
    key_id: str
    policy_sha256: str
    issued_at: int
    expires_at: int


def verify_meeting_intelligence_attestation(
    *,
    compact_attestation: str,
    public_key_base64: str,
    expected_key_id: str,
    expected_provider_code: str,
    expected_model: str,
    expected_processing_region: str,
    expected_policy_sha256: str,
    now: int | None = None,
) -> VerifiedMeetingIntelligenceAttestation:
    if (
        not compact_attestation
        or len(compact_attestation.encode("utf-8")) > MAX_ATTESTATION_BYTES
    ):
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    parts = compact_attestation.split(".")
    if len(parts) != 3 or parts[0] != ATTESTATION_PREFIX:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    encoded_payload, encoded_signature = parts[1], parts[2]
    payload_bytes = _decode_urlsafe(encoded_payload, MAX_PAYLOAD_BYTES)
    signature = _decode_urlsafe(encoded_signature, 64)
    if len(signature) != 64:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    payload = _payload(payload_bytes)
    canonical_payload = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if payload_bytes != canonical_payload:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    public_key = _public_key(public_key_base64)
    try:
        public_key.verify(
            signature,
            f"{ATTESTATION_PREFIX}.{encoded_payload}".encode("ascii"),
        )
    except InvalidSignature as error:
        raise MeetingIntelligenceAttestationError(
            "Policy attestation is invalid."
        ) from error

    _require_string(payload, "kid")
    _require_string(payload, "attestationId")
    _require_string(payload, "providerCode")
    _require_string(payload, "model")
    _require_string(payload, "processingRegion")
    _require_string(payload, "policySha256")
    _require_boolean(payload, "customerDataTrainingDisabled")
    _require_boolean(payload, "providerRetentionDisabled")
    _require_integer(payload, "v")
    _require_integer(payload, "issuedAt")
    _require_integer(payload, "expiresAt")

    if payload["v"] != 1:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    if not _SAFE_IDENTIFIER.fullmatch(expected_key_id):
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    if payload["kid"] != expected_key_id:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    if payload["providerCode"] != expected_provider_code:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    if payload["model"] != expected_model:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    if payload["processingRegion"] != expected_processing_region:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    if payload["customerDataTrainingDisabled"] is not True:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    if payload["providerRetentionDisabled"] is not True:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    if not _SHA256.fullmatch(expected_policy_sha256):
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    if payload["policySha256"] != expected_policy_sha256:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")

    issued_at = payload["issuedAt"]
    expires_at = payload["expiresAt"]
    evaluated_at = int(time.time()) if now is None else now
    if issued_at > evaluated_at + ATTESTATION_CLOCK_SKEW_SECONDS:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    if expires_at <= evaluated_at or expires_at <= issued_at:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    if expires_at - issued_at > MAX_ATTESTATION_LIFETIME_SECONDS:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    try:
        attestation_id = UUID(payload["attestationId"])
    except (ValueError, AttributeError) as error:
        raise MeetingIntelligenceAttestationError(
            "Policy attestation is invalid."
        ) from error
    if str(attestation_id) != payload["attestationId"]:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    return VerifiedMeetingIntelligenceAttestation(
        attestation_id=attestation_id,
        key_id=payload["kid"],
        policy_sha256=payload["policySha256"],
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _payload(payload_bytes: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(
            payload_bytes.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, ValueError, TypeError) as error:
        raise MeetingIntelligenceAttestationError(
            "Policy attestation is invalid."
        ) from error
    if not isinstance(decoded, dict) or frozenset(decoded) != _EXPECTED_FIELDS:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    return decoded


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _decode_urlsafe(value: str, maximum_bytes: int) -> bytes:
    if not value or "=" in value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    if len(value) > ((maximum_bytes + 2) // 3) * 4:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            value + padding, altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError) as error:
        raise MeetingIntelligenceAttestationError(
            "Policy attestation is invalid."
        ) from error
    if len(decoded) > maximum_bytes:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    return decoded


def _public_key(value: str) -> Ed25519PublicKey:
    if not value or len(value) > 1_024:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    try:
        encoded = value.encode("ascii")
        der = base64.b64decode(encoded, validate=True)
        key = serialization.load_der_public_key(der)
    except (
        UnicodeEncodeError,
        binascii.Error,
        ValueError,
        TypeError,
        UnsupportedAlgorithm,
    ) as error:
        raise MeetingIntelligenceAttestationError(
            "Policy attestation is invalid."
        ) from error
    if not isinstance(key, Ed25519PublicKey):
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
    return key


def _require_string(payload: dict[str, Any], field: str) -> None:
    value = payload[field]
    if type(value) is not str or not value or len(value) > 256:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")


def _require_boolean(payload: dict[str, Any], field: str) -> None:
    if type(payload[field]) is not bool:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")


def _require_integer(payload: dict[str, Any], field: str) -> None:
    if type(payload[field]) is not int:
        raise MeetingIntelligenceAttestationError("Policy attestation is invalid.")
