from __future__ import annotations

import base64
import binascii
import json
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .contracts import AskResponse

_KEY_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class DataKeyConfigurationError(RuntimeError):
    pass


class PayloadCipher:
    def __init__(self, encoded_key: str) -> None:
        try:
            key = base64.b64decode(encoded_key, validate=True)
        except (ValueError, binascii.Error) as error:
            raise DataKeyConfigurationError("Agent data key is not valid base64.") from error
        if len(key) != 32:
            raise DataKeyConfigurationError("Agent data key must contain 32 bytes.")
        self._cipher = AESGCM(key)

    def encrypt_bytes(self, payload: bytes, aad: bytes) -> tuple[bytes, bytes]:
        nonce = os.urandom(12)
        return nonce, self._cipher.encrypt(nonce, payload, aad)

    def decrypt_bytes(self, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
        return self._cipher.decrypt(nonce, ciphertext, aad)


class PayloadCipherKeyring:
    def __init__(
        self,
        *,
        active_version: str,
        active_key: str,
        previous_keys: dict[str, str] | None = None,
    ) -> None:
        if not _KEY_VERSION_PATTERN.fullmatch(active_version):
            raise DataKeyConfigurationError("Agent data key version is invalid.")
        keys = dict(previous_keys or {})
        if any(not _KEY_VERSION_PATTERN.fullmatch(version) for version in keys):
            raise DataKeyConfigurationError("Agent previous data key version is invalid.")
        keys[active_version] = active_key
        self._ciphers = {version: PayloadCipher(key) for version, key in keys.items()}
        self.active_version = active_version

    def encrypt_bytes(self, payload: bytes, aad: bytes) -> tuple[str, bytes, bytes]:
        nonce, ciphertext = self._ciphers[self.active_version].encrypt_bytes(payload, aad)
        return self.active_version, nonce, ciphertext

    def decrypt_bytes(
        self, version: str, nonce: bytes, ciphertext: bytes, aad: bytes
    ) -> bytes:
        cipher = self._ciphers.get(version)
        if cipher is None:
            raise DataKeyConfigurationError(
                "The required Agent data key version is unavailable."
            )
        return cipher.decrypt_bytes(nonce, ciphertext, aad)

    def encrypt_response(self, response: AskResponse, aad: bytes) -> tuple[str, bytes, bytes]:
        return self.encrypt_bytes(response.model_dump_json(by_alias=True).encode("utf-8"), aad)

    def decrypt_response(
        self, version: str, nonce: bytes, ciphertext: bytes, aad: bytes
    ) -> AskResponse:
        return AskResponse.model_validate_json(
            self.decrypt_bytes(version, nonce, ciphertext, aad)
        )


def load_payload_keyring() -> PayloadCipherKeyring:
    active_key = os.getenv("DWP_AGENT_DATA_KEY", "").strip()
    if not active_key:
        raise DataKeyConfigurationError("Agent data encryption key is required.")
    active_version = os.getenv("DWP_AGENT_DATA_KEY_VERSION", "legacy-v1").strip()
    raw_previous = os.getenv("DWP_AGENT_PREVIOUS_DATA_KEYS", "{}").strip() or "{}"
    try:
        parsed = json.loads(raw_previous)
    except json.JSONDecodeError as error:
        raise DataKeyConfigurationError(
            "Agent previous data keys must be a JSON object."
        ) from error
    if not isinstance(parsed, dict) or any(
        not isinstance(version, str) or not isinstance(key, str)
        for version, key in parsed.items()
    ):
        raise DataKeyConfigurationError("Agent previous data keys must be a string map.")
    return PayloadCipherKeyring(
        active_version=active_version,
        active_key=active_key,
        previous_keys=parsed,
    )
