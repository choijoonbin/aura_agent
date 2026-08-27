from __future__ import annotations

from .crypto import (
    DataKeyConfigurationError,
    PayloadCipher as BasePayloadCipher,
    PayloadCipherKeyring,
    load_payload_keyring as load_keyring,
)
from .envelope import (
    EnvelopeEncryptionError,
    PayloadEncryption,
    load_payload_encryption as load_encryption,
)
from .key_provider import KeyProviderConfigurationError
from .run_store_errors import RunStoreUnavailable


class PayloadCipher(BasePayloadCipher):
    def __init__(self, encoded_key: str) -> None:
        try:
            super().__init__(encoded_key)
        except DataKeyConfigurationError as error:
            raise RunStoreUnavailable(str(error)) from error


def load_payload_keyring() -> PayloadCipherKeyring:
    try:
        return load_keyring()
    except DataKeyConfigurationError as error:
        raise RunStoreUnavailable(str(error)) from error


def load_payload_encryption() -> PayloadEncryption:
    try:
        return load_encryption()
    except (
        DataKeyConfigurationError,
        EnvelopeEncryptionError,
        KeyProviderConfigurationError,
        ValueError,
    ) as error:
        raise RunStoreUnavailable(str(error)) from error
