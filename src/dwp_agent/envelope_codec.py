from __future__ import annotations

import base64
import binascii
import struct

from .envelope import EnvelopeCiphertext, KeySlot


class EnvelopeCiphertextCodec:
    PREFIX = "dwp2."
    MAGIC = b"DWP2"
    MAX_ENCODED_CHARACTERS = 24 * 1024 * 1024

    def encode(self, envelope: EnvelopeCiphertext) -> str:
        slot = envelope.key_slot
        fields = (
            _pack_bytes(envelope.content_algorithm.encode("utf-8")),
            _pack_bytes(envelope.aad_profile.encode("utf-8")),
            _pack_bytes(slot.provider.encode("utf-8")),
            _pack_bytes(slot.immutable_key_id.encode("utf-8")),
            _pack_nullable_string(slot.key_version),
            _pack_bytes(slot.wrap_algorithm.encode("utf-8")),
            _pack_bytes(slot.wrapped_dek),
            _pack_bytes(envelope.nonce),
            _pack_bytes(envelope.ciphertext),
            _pack_bytes(envelope.aad_sha256),
        )
        return self.PREFIX + _base64url(self.MAGIC + b"".join(fields))

    def decode(self, encoded: str) -> EnvelopeCiphertext:
        if (
            not encoded.startswith(self.PREFIX)
            or len(encoded) > self.MAX_ENCODED_CHARACTERS
        ):
            raise ValueError("Encrypted value is not a supported DWP envelope.")
        try:
            raw = _decode_base64url(encoded[len(self.PREFIX) :])
            reader = _EnvelopeReader(raw)
            if reader.take(4) != self.MAGIC:
                raise ValueError("Encryption envelope magic is invalid.")
            content_algorithm = reader.read_string(64)
            aad_profile = reader.read_string(64)
            provider = reader.read_string(64)
            immutable_key_id = reader.read_string(1024)
            key_version = reader.read_nullable_string(128)
            wrap_algorithm = reader.read_string(64)
            wrapped_dek = reader.read_bytes(16_384)
            nonce = reader.read_bytes(12)
            ciphertext = reader.read_bytes(16 * 1024 * 1024)
            aad_sha256 = reader.read_bytes(32)
            if not reader.done:
                raise ValueError("Encryption envelope has trailing data.")
            return EnvelopeCiphertext(
                format_version=2,
                content_algorithm=content_algorithm,
                aad_profile=aad_profile,
                key_slot=KeySlot(
                    provider=provider,
                    immutable_key_id=immutable_key_id,
                    key_version=key_version,
                    wrap_algorithm=wrap_algorithm,
                    wrapped_dek=wrapped_dek,
                ),
                nonce=nonce,
                ciphertext=ciphertext,
                aad_sha256=aad_sha256,
            )
        except (UnicodeDecodeError, ValueError, struct.error) as error:
            raise ValueError("Encrypted value is not a valid DWP envelope.") from error


class _EnvelopeReader:
    def __init__(self, value: bytes) -> None:
        self.value = value
        self.offset = 0

    @property
    def done(self) -> bool:
        return self.offset == len(self.value)

    def take(self, length: int) -> bytes:
        if length < 0 or self.offset + length > len(self.value):
            raise ValueError("Encryption envelope is truncated.")
        result = self.value[self.offset : self.offset + length]
        self.offset += length
        return result

    def read_bytes(self, maximum: int) -> bytes:
        length = struct.unpack(">i", self.take(4))[0]
        if length < 0 or length > maximum:
            raise ValueError("Encryption envelope field length is invalid.")
        return self.take(length)

    def read_string(self, maximum: int) -> str:
        return self.read_bytes(maximum).decode("utf-8")

    def read_nullable_string(self, maximum: int) -> str | None:
        length = struct.unpack(">i", self.take(4))[0]
        if length == -1:
            return None
        if length < 0 or length > maximum:
            raise ValueError("Encryption envelope field length is invalid.")
        return self.take(length).decode("utf-8")


def _pack_bytes(value: bytes) -> bytes:
    return struct.pack(">i", len(value)) + value


def _pack_nullable_string(value: str | None) -> bytes:
    return struct.pack(">i", -1) if value is None else _pack_bytes(value.encode("utf-8"))


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    if not value or any(character not in _BASE64URL for character in value):
        raise ValueError("Encryption envelope Base64URL is invalid.")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("Encryption envelope Base64URL is invalid.") from error
    if _base64url(decoded) != value:
        raise ValueError("Encryption envelope Base64URL is not canonical.")
    return decoded


_BASE64URL = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
