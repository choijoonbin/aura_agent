from __future__ import annotations

import base64
import binascii
import hashlib
import os
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto import PayloadCipherKeyring
from .key_provider import (
    KeyProvider,
    VersionedKeyMaterial,
    load_versioned_key_material,
    normalized_environment,
)


class EnvelopeEncryptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeyContext:
    environment: str
    service: str
    purpose: str
    tenant_id: int
    resource_type: str
    resource_id: str
    field: str

    AAD_PROFILE = "dwp-aad-v1"
    FORMAT_VERSION = 2

    def __post_init__(self) -> None:
        if self.environment not in {"local", "dev", "qa", "prod"}:
            raise ValueError("Encryption environment is not canonical.")
        _required(self.service, "service", 80)
        _required(self.purpose, "purpose", 80)
        if self.tenant_id < 0:
            raise ValueError("Encryption tenant ID cannot be negative.")
        _required(self.resource_type, "resource type", 80)
        _required(self.resource_id, "resource ID", 256)
        _required(self.field, "field", 80)

    @classmethod
    def payload(
        cls,
        *,
        tenant_id: str | int,
        resource_type: str,
        resource_id: str,
        field: str,
    ) -> KeyContext:
        return cls(
            environment=normalized_environment(),
            service="dwp-agent",
            purpose="payload",
            tenant_id=int(tenant_id),
            resource_type=resource_type,
            resource_id=resource_id,
            field=field,
        )

    def canonical_aad(self) -> bytes:
        values = (
            self.environment,
            self.service,
            self.purpose,
            str(self.tenant_id),
            self.resource_type,
            self.resource_id,
            self.field,
            str(self.FORMAT_VERSION),
        )
        encoded = ".".join(_base64url(value.encode("utf-8")) for value in values)
        return f"{self.AAD_PROFILE}.{encoded}".encode("ascii")


@dataclass(frozen=True)
class KeySlot:
    provider: str
    immutable_key_id: str
    key_version: str | None
    wrap_algorithm: str
    wrapped_dek: bytes

    def __post_init__(self) -> None:
        _required(self.provider, "provider", 64)
        _required(self.immutable_key_id, "immutable key ID", 1024)
        if self.key_version is not None:
            _required(self.key_version, "key version", 128)
        _required(self.wrap_algorithm, "wrap algorithm", 64)
        if not 16 <= len(self.wrapped_dek) <= 16_384:
            raise ValueError("Wrapped data key length is invalid.")


@dataclass(frozen=True)
class EnvelopeCiphertext:
    format_version: int
    content_algorithm: str
    aad_profile: str
    key_slot: KeySlot
    nonce: bytes
    ciphertext: bytes
    aad_sha256: bytes

    def __post_init__(self) -> None:
        if self.format_version != 2:
            raise ValueError("Unsupported encryption envelope version.")
        if self.content_algorithm != "A256GCM":
            raise ValueError("Unsupported content encryption algorithm.")
        if self.aad_profile != KeyContext.AAD_PROFILE:
            raise ValueError("Unsupported encryption AAD profile.")
        if len(self.nonce) != 12:
            raise ValueError("Envelope nonce must contain 12 bytes.")
        if not 16 <= len(self.ciphertext) <= 16 * 1024 * 1024:
            raise ValueError("Envelope ciphertext length is invalid.")
        if len(self.aad_sha256) != 32:
            raise ValueError("Envelope AAD digest must contain 32 bytes.")


class EnvelopeKeyProvider(Protocol):
    provider_id: str

    def wrap_dek(self, plaintext_dek: bytes, context: KeyContext) -> KeySlot:
        ...

    def unwrap_dek(self, key_slot: KeySlot, context: KeyContext) -> bytes:
        ...


class MaterialEnvelopeKeyProvider:
    WRAP_ALGORITHM = "A256GCMKW"

    def __init__(self, material: VersionedKeyMaterial, immutable_key_id: str) -> None:
        self.provider_id = material.provider_id
        self.immutable_key_id = _required(
            immutable_key_id, "immutable key ID", 1024
        )
        self.active_version = _required(material.active_version, "active version", 128)
        keys = dict(material.previous_keys)
        keys[self.active_version] = material.active_key
        self._keys = {version: _decode_key(value) for version, value in keys.items()}

    def wrap_dek(self, plaintext_dek: bytes, context: KeyContext) -> KeySlot:
        if len(plaintext_dek) != 32:
            raise EnvelopeEncryptionError("Data encryption key must contain 32 bytes.")
        nonce = os.urandom(12)
        wrapped = nonce + AESGCM(self._keys[self.active_version]).encrypt(
            nonce, plaintext_dek, _wrap_aad(context)
        )
        return KeySlot(
            provider=self.provider_id,
            immutable_key_id=self.immutable_key_id,
            key_version=self.active_version,
            wrap_algorithm=self.WRAP_ALGORITHM,
            wrapped_dek=wrapped,
        )

    def unwrap_dek(self, key_slot: KeySlot, context: KeyContext) -> bytes:
        if (
            key_slot.provider != self.provider_id
            or key_slot.immutable_key_id != self.immutable_key_id
            or key_slot.wrap_algorithm != self.WRAP_ALGORITHM
        ):
            raise EnvelopeEncryptionError(
                "Wrapped data key is outside the configured allowlist."
            )
        key = self._keys.get(key_slot.key_version or "")
        if key is None:
            raise EnvelopeEncryptionError(
                "The required wrapping key version is unavailable."
            )
        if len(key_slot.wrapped_dek) != 60:
            raise EnvelopeEncryptionError("Wrapped data key length is invalid.")
        try:
            plaintext = AESGCM(key).decrypt(
                key_slot.wrapped_dek[:12],
                key_slot.wrapped_dek[12:],
                _wrap_aad(context),
            )
        except InvalidTag as error:
            raise EnvelopeEncryptionError("Data-key unwrapping failed.") from error
        if len(plaintext) != 32:
            raise EnvelopeEncryptionError("Unwrapped data key length is invalid.")
        return plaintext


class EnvelopeEncryptionService:
    def __init__(self, key_provider: EnvelopeKeyProvider) -> None:
        self.key_provider = key_provider

    def encrypt(self, plaintext: bytes, context: KeyContext) -> EnvelopeCiphertext:
        aad = context.canonical_aad()
        dek = bytearray(os.urandom(32))
        nonce = os.urandom(12)
        try:
            ciphertext = AESGCM(bytes(dek)).encrypt(nonce, plaintext, aad)
            return EnvelopeCiphertext(
                format_version=2,
                content_algorithm="A256GCM",
                aad_profile=KeyContext.AAD_PROFILE,
                key_slot=self.key_provider.wrap_dek(bytes(dek), context),
                nonce=nonce,
                ciphertext=ciphertext,
                aad_sha256=hashlib.sha256(aad).digest(),
            )
        finally:
            dek[:] = b"\x00" * len(dek)

    def decrypt(self, envelope: EnvelopeCiphertext, context: KeyContext) -> bytes:
        aad = context.canonical_aad()
        if not _constant_time_equal(envelope.aad_sha256, hashlib.sha256(aad).digest()):
            raise EnvelopeEncryptionError(
                "Envelope context does not match the trusted resource."
            )
        dek = bytearray(self.key_provider.unwrap_dek(envelope.key_slot, context))
        try:
            return AESGCM(bytes(dek)).decrypt(
                envelope.nonce, envelope.ciphertext, aad
            )
        except InvalidTag as error:
            raise EnvelopeEncryptionError("Envelope content decryption failed.") from error
        finally:
            dek[:] = b"\x00" * len(dek)

    def rewrap(
        self,
        envelope: EnvelopeCiphertext,
        context: KeyContext,
        target_provider: EnvelopeKeyProvider,
    ) -> EnvelopeCiphertext:
        aad = context.canonical_aad()
        if not _constant_time_equal(envelope.aad_sha256, hashlib.sha256(aad).digest()):
            raise EnvelopeEncryptionError(
                "Envelope context does not match the trusted resource."
            )
        dek = bytearray(self.key_provider.unwrap_dek(envelope.key_slot, context))
        try:
            return EnvelopeCiphertext(
                format_version=envelope.format_version,
                content_algorithm=envelope.content_algorithm,
                aad_profile=envelope.aad_profile,
                key_slot=target_provider.wrap_dek(bytes(dek), context),
                nonce=envelope.nonce,
                ciphertext=envelope.ciphertext,
                aad_sha256=envelope.aad_sha256,
            )
        finally:
            dek[:] = b"\x00" * len(dek)


class PayloadEncryption:
    def __init__(
        self,
        envelope_service: EnvelopeEncryptionService,
        legacy_keyring: PayloadCipherKeyring,
    ) -> None:
        from .envelope_codec import EnvelopeCiphertextCodec

        self.envelope_service = envelope_service
        self.legacy_keyring = legacy_keyring
        self.codec = EnvelopeCiphertextCodec()

    def encrypt_bytes(self, payload: bytes, context: KeyContext) -> str:
        return self.codec.encode(self.envelope_service.encrypt(payload, context))

    def decrypt_bytes(
        self,
        *,
        envelope: str | None,
        context: KeyContext,
        legacy_version: str | None,
        legacy_nonce: bytes | None,
        legacy_ciphertext: bytes | None,
        legacy_aad: bytes,
    ) -> bytes:
        if envelope is not None:
            return self.envelope_service.decrypt(self.codec.decode(envelope), context)
        if legacy_version is None or legacy_nonce is None or legacy_ciphertext is None:
            raise EnvelopeEncryptionError("Encrypted payload metadata is incomplete.")
        return self.legacy_keyring.decrypt_bytes(
            legacy_version, legacy_nonce, legacy_ciphertext, legacy_aad
        )


def load_payload_encryption(provider: KeyProvider | None = None) -> PayloadEncryption:
    material = load_versioned_key_material(provider)
    legacy = PayloadCipherKeyring(
        active_version=material.active_version,
        active_key=material.active_key,
        previous_keys=dict(material.previous_keys),
    )
    reference = os.getenv("DWP_AGENT_KEY_REFERENCE", "").strip()
    if not reference:
        if material.provider_id.startswith("local-"):
            reference = "local://dwp-agent/payload"
        else:
            raise EnvelopeEncryptionError(
                "Managed envelope encryption requires an immutable key reference."
            )
    wrapping_provider = MaterialEnvelopeKeyProvider(material, reference)
    return PayloadEncryption(EnvelopeEncryptionService(wrapping_provider), legacy)


def _wrap_aad(context: KeyContext) -> bytes:
    return b"dwp-key-wrap-v1\n" + context.canonical_aad()


def _required(value: str, field: str, maximum: int) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"Encryption {field} is invalid.")
    return value


def _decode_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise EnvelopeEncryptionError("Wrapping key is not valid Base64.") from error
    if len(key) != 32:
        raise EnvelopeEncryptionError("Wrapping key must contain 32 bytes.")
    return key


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _constant_time_equal(left: bytes, right: bytes) -> bool:
    import hmac

    return hmac.compare_digest(left, right)
