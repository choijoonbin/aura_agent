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


ATTESTATION_PREFIX = "dwpma1"
MAX_ATTESTATION_BYTES = 12 * 1_024
MAX_PAYLOAD_BYTES = 8 * 1_024
MAX_ATTESTATION_LIFETIME_SECONDS = 7 * 24 * 60 * 60
ATTESTATION_CLOCK_SKEW_SECONDS = 60

_EXPECTED_FIELDS = frozenset(
    {
        "v",
        "kid",
        "attestationId",
        "purpose",
        "originHost",
        "providerCode",
        "storageProviderCode",
        "processingRegion",
        "egressAvailable",
        "storageAvailable",
        "speechToTextAvailable",
        "deletionAvailable",
        "cryptoShredAvailable",
        "orphanCleanupAvailable",
        "maximumOrphanTtlSeconds",
        "customerManagedStorage",
        "providerRetentionDisabled",
        "policySha256",
        "issuedAt",
        "expiresAt",
    }
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MeetingMediaAttestationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedMeetingMediaAttestation:
    attestation_id: UUID
    purpose: str
    origin_host: str
    provider_code: str
    storage_provider_code: str
    processing_region: str
    egress_available: bool
    storage_available: bool
    speech_to_text_available: bool
    deletion_available: bool
    crypto_shred_available: bool
    orphan_cleanup_available: bool
    maximum_orphan_ttl_seconds: int
    customer_managed_storage: bool
    provider_retention_disabled: bool

    def ready(self) -> bool:
        common = (
            self.storage_available
            and self.deletion_available
            and self.crypto_shred_available
            and self.orphan_cleanup_available
            and 30 <= self.maximum_orphan_ttl_seconds <= 3_600
            and self.customer_managed_storage
            and self.provider_retention_disabled
        )
        if self.purpose == "RECORDING":
            return common and self.egress_available
        return common and self.speech_to_text_available


def verify_meeting_media_attestation(
    *,
    compact_attestation: str,
    public_key_base64: str,
    expected_key_id: str,
    expected_purpose: str,
    expected_origin_host: str,
    expected_provider_code: str,
    expected_storage_provider_code: str,
    expected_processing_region: str,
    expected_policy_sha256: str,
    now: int | None = None,
) -> VerifiedMeetingMediaAttestation:
    if (
        not compact_attestation
        or len(compact_attestation.encode("utf-8")) > MAX_ATTESTATION_BYTES
    ):
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")
    parts = compact_attestation.split(".")
    if len(parts) != 3 or parts[0] != ATTESTATION_PREFIX:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")
    encoded_payload, encoded_signature = parts[1], parts[2]
    payload_bytes = _decode_urlsafe(encoded_payload, MAX_PAYLOAD_BYTES)
    signature = _decode_urlsafe(encoded_signature, 64)
    if len(signature) != 64:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")
    payload = _payload(payload_bytes)
    canonical_payload = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if payload_bytes != canonical_payload:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")
    try:
        _public_key(public_key_base64).verify(
            signature,
            f"{ATTESTATION_PREFIX}.{encoded_payload}".encode("ascii"),
        )
    except InvalidSignature as error:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.") from error

    for field in (
        "kid",
        "attestationId",
        "purpose",
        "originHost",
        "providerCode",
        "storageProviderCode",
        "processingRegion",
        "policySha256",
    ):
        _require_string(payload, field)
    for field in (
        "egressAvailable",
        "storageAvailable",
        "speechToTextAvailable",
        "deletionAvailable",
        "cryptoShredAvailable",
        "orphanCleanupAvailable",
        "customerManagedStorage",
        "providerRetentionDisabled",
    ):
        _require_boolean(payload, field)
    for field in ("v", "maximumOrphanTtlSeconds", "issuedAt", "expiresAt"):
        _require_integer(payload, field)
    matches = (
        payload["v"] == 1
        and payload["kid"] == expected_key_id
        and payload["purpose"] == expected_purpose
        and payload["originHost"] == expected_origin_host
        and payload["providerCode"] == expected_provider_code
        and payload["storageProviderCode"] == expected_storage_provider_code
        and payload["processingRegion"] == expected_processing_region
        and payload["policySha256"] == expected_policy_sha256
        and _SAFE_IDENTIFIER.fullmatch(expected_key_id)
        and _SAFE_IDENTIFIER.fullmatch(expected_provider_code)
        and _SAFE_IDENTIFIER.fullmatch(expected_storage_provider_code)
        and _SHA256.fullmatch(expected_policy_sha256)
    )
    if not matches:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")
    issued_at = payload["issuedAt"]
    expires_at = payload["expiresAt"]
    evaluated_at = int(time.time()) if now is None else now
    if (
        issued_at > evaluated_at + ATTESTATION_CLOCK_SKEW_SECONDS
        or expires_at <= evaluated_at
        or expires_at <= issued_at
        or expires_at - issued_at > MAX_ATTESTATION_LIFETIME_SECONDS
    ):
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")
    try:
        attestation_id = UUID(payload["attestationId"])
    except (ValueError, AttributeError) as error:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.") from error
    if str(attestation_id) != payload["attestationId"]:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")
    verified = VerifiedMeetingMediaAttestation(
        attestation_id=attestation_id,
        purpose=payload["purpose"],
        origin_host=payload["originHost"],
        provider_code=payload["providerCode"],
        storage_provider_code=payload["storageProviderCode"],
        processing_region=payload["processingRegion"],
        egress_available=payload["egressAvailable"],
        storage_available=payload["storageAvailable"],
        speech_to_text_available=payload["speechToTextAvailable"],
        deletion_available=payload["deletionAvailable"],
        crypto_shred_available=payload["cryptoShredAvailable"],
        orphan_cleanup_available=payload["orphanCleanupAvailable"],
        maximum_orphan_ttl_seconds=payload["maximumOrphanTtlSeconds"],
        customer_managed_storage=payload["customerManagedStorage"],
        provider_retention_disabled=payload["providerRetentionDisabled"],
    )
    if not verified.ready():
        raise MeetingMediaAttestationError("Media broker attestation is not ready.")
    return verified


def _payload(payload_bytes: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(
            payload_bytes.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (UnicodeDecodeError, ValueError, TypeError) as error:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.") from error
    if not isinstance(decoded, dict) or frozenset(decoded) != _EXPECTED_FIELDS:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")
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
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError) as error:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.") from error
    if len(decoded) > maximum_bytes:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")
    return decoded


def _public_key(value: str) -> Ed25519PublicKey:
    if not value or len(value) > 1_024:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")
    try:
        der = base64.b64decode(value.encode("ascii"), validate=True)
        key = serialization.load_der_public_key(der)
    except (
        UnicodeEncodeError,
        binascii.Error,
        ValueError,
        TypeError,
        UnsupportedAlgorithm,
    ) as error:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.") from error
    if not isinstance(key, Ed25519PublicKey):
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")
    return key


def _require_string(payload: dict[str, Any], field: str) -> None:
    value = payload[field]
    if type(value) is not str or not value or len(value) > 256:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")


def _require_boolean(payload: dict[str, Any], field: str) -> None:
    if type(payload[field]) is not bool:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")


def _require_integer(payload: dict[str, Any], field: str) -> None:
    if type(payload[field]) is not int:
        raise MeetingMediaAttestationError("Media broker attestation is invalid.")
