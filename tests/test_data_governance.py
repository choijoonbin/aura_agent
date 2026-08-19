import base64

import pytest

from dwp_agent.conversation_store import _retention_days
from dwp_agent.crypto import DataKeyConfigurationError, PayloadCipherKeyring
from dwp_agent.run_store import RunStoreUnavailable, load_payload_keyring


def key(byte: int) -> str:
    return base64.b64encode(bytes([byte]) * 32).decode("ascii")


def test_keyring_encrypts_with_active_version_and_reads_previous_versions() -> None:
    previous = PayloadCipherKeyring(active_version="v1", active_key=key(1))
    version, nonce, ciphertext = previous.encrypt_bytes(b"retained data", b"tenant:1")
    rotated = PayloadCipherKeyring(
        active_version="v2", active_key=key(2), previous_keys={"v1": key(1)}
    )

    assert version == "v1"
    assert rotated.decrypt_bytes(version, nonce, ciphertext, b"tenant:1") == b"retained data"
    assert rotated.encrypt_bytes(b"new data", b"tenant:1")[0] == "v2"


def test_keyring_fails_closed_when_historical_key_is_missing() -> None:
    keyring = PayloadCipherKeyring(active_version="v2", active_key=key(2))

    with pytest.raises(DataKeyConfigurationError, match="unavailable"):
        keyring.decrypt_bytes("v1", b"0" * 12, b"ciphertext", b"tenant:1")


def test_environment_keyring_requires_a_valid_versioned_key_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_AGENT_DATA_KEY", key(2))
    monkeypatch.setenv("DWP_AGENT_DATA_KEY_VERSION", "v2")
    monkeypatch.setenv("DWP_AGENT_PREVIOUS_DATA_KEYS", '{"v1":"not-base64"}')

    with pytest.raises(RunStoreUnavailable, match="not valid base64"):
        load_payload_keyring()


def test_keyring_rejects_invalid_historical_key_version() -> None:
    with pytest.raises(DataKeyConfigurationError, match="previous data key version"):
        PayloadCipherKeyring(
            active_version="v2",
            active_key=key(2),
            previous_keys={"../../v1": key(1)},
        )


@pytest.mark.parametrize("value", ["29", "3651", "not-a-number"])
def test_conversation_retention_rejects_unsupported_policy_defaults(value: str) -> None:
    with pytest.raises(RunStoreUnavailable):
        _retention_days(value)


def test_conversation_retention_accepts_enterprise_policy_range() -> None:
    assert _retention_days("90") == 90
    assert _retention_days("3650") == 3650
