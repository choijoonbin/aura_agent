import base64
from dataclasses import replace

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from dwp_agent.crypto import PayloadCipherKeyring
from dwp_agent.envelope import (
    EnvelopeCiphertext,
    EnvelopeEncryptionError,
    EnvelopeEncryptionService,
    KeyContext,
    KeySlot,
    MaterialEnvelopeKeyProvider,
    PayloadEncryption,
)
from dwp_agent.envelope_codec import EnvelopeCiphertextCodec
from dwp_agent.key_provider import VersionedKeyMaterial


def test_canonical_aad_matches_the_java_contract() -> None:
    context = context_for(tenant_id=42)

    assert context.canonical_aad() == (
        b"dwp-aad-v1.bG9jYWw.ZHdwLWFnZW50.cGF5bG9hZA.NDI.bWVzc2FnZQ."
        b"bS03.Ym9keQ.Mg"
    )


def test_envelope_round_trip_uses_a_fresh_dek_and_nonce_per_write() -> None:
    service = service_for(key(1), version="local-v1")
    context = context_for()

    first = service.encrypt(b"confidential payload", context)
    second = service.encrypt(b"confidential payload", context)

    assert service.decrypt(first, context) == b"confidential payload"
    assert service.decrypt(second, context) == b"confidential payload"
    assert first.nonce != second.nonce
    assert first.key_slot.wrapped_dek != second.key_slot.wrapped_dek
    assert first.ciphertext != second.ciphertext


@pytest.mark.parametrize(
    "mutate",
    [
        lambda envelope: replace(envelope, nonce=b"x" * 12),
        lambda envelope: replace(envelope, ciphertext=b"x" * len(envelope.ciphertext)),
        lambda envelope: replace(
            envelope,
            key_slot=replace(envelope.key_slot, wrapped_dek=b"x" * 60),
        ),
        lambda envelope: replace(
            envelope,
            key_slot=replace(envelope.key_slot, immutable_key_id="local://other/key"),
        ),
    ],
)
def test_envelope_rejects_tampered_metadata_and_ciphertext(mutate) -> None:
    service = service_for(key(1), version="local-v1")
    context = context_for()
    envelope = service.encrypt(b"payload", context)

    with pytest.raises((EnvelopeEncryptionError, InvalidTag)):
        service.decrypt(mutate(envelope), context)


def test_envelope_rejects_a_tenant_or_resource_swap() -> None:
    service = service_for(key(1), version="local-v1")
    envelope = service.encrypt(b"payload", context_for())

    with pytest.raises(EnvelopeEncryptionError, match="trusted resource"):
        service.decrypt(envelope, context_for(tenant_id=43))
    with pytest.raises(EnvelopeEncryptionError, match="trusted resource"):
        service.decrypt(envelope, context_for(resource_id="m-8"))


def test_rewrap_changes_only_the_key_slot() -> None:
    source = provider_for(key(1), version="local-v1")
    target = provider_for(key(2), version="local-v2")
    service = EnvelopeEncryptionService(source)
    context = context_for()
    original = service.encrypt(b"payload", context)

    rewrapped = service.rewrap(original, context, target)

    assert rewrapped.key_slot != original.key_slot
    assert rewrapped.nonce == original.nonce
    assert rewrapped.ciphertext == original.ciphertext
    assert rewrapped.aad_sha256 == original.aad_sha256
    assert EnvelopeEncryptionService(target).decrypt(rewrapped, context) == b"payload"


def test_codec_round_trip_and_strict_rejection() -> None:
    codec = EnvelopeCiphertextCodec()
    envelope = service_for(key(1), version="local-v1").encrypt(b"payload", context_for())
    encoded = codec.encode(envelope)

    assert codec.decode(encoded) == envelope
    with pytest.raises(ValueError):
        codec.decode("v1." + encoded)
    with pytest.raises(ValueError):
        codec.decode(encoded[:-2])
    with pytest.raises(ValueError):
        codec.decode(encoded + "AA")


def test_codec_matches_the_java_golden_envelope() -> None:
    envelope = EnvelopeCiphertext(
        format_version=2,
        content_algorithm="A256GCM",
        aad_profile="dwp-aad-v1",
        key_slot=KeySlot(
            provider="local-inline",
            immutable_key_id="local://dwp-agent/payload",
            key_version="local-v1",
            wrap_algorithm="A256GCMKW",
            wrapped_dek=bytes([0x10]) * 60,
        ),
        nonce=bytes([0x20]) * 12,
        ciphertext=bytes([0x30]) * 16,
        aad_sha256=bytes([0x40]) * 32,
    )

    assert EnvelopeCiphertextCodec().encode(envelope) == GOLDEN_ENVELOPE
    assert EnvelopeCiphertextCodec().decode(GOLDEN_ENVELOPE) == envelope


def test_v2_failure_never_falls_back_to_legacy_ciphertext() -> None:
    legacy = PayloadCipherKeyring(active_version="legacy-v1", active_key=key(1))
    context = context_for()
    encryption = PayloadEncryption(service_for(key(1), version="local-v1"), legacy)
    legacy_version, legacy_nonce, legacy_ciphertext = legacy.encrypt_bytes(
        b"legacy payload", b"legacy-aad"
    )
    encoded = encryption.encrypt_bytes(b"v2 payload", context)

    with pytest.raises(ValueError):
        encryption.decrypt_bytes(
            envelope=encoded[:-2],
            context=context,
            legacy_version=legacy_version,
            legacy_nonce=legacy_nonce,
            legacy_ciphertext=legacy_ciphertext,
            legacy_aad=b"legacy-aad",
        )

    assert encryption.decrypt_bytes(
        envelope=None,
        context=context,
        legacy_version=legacy_version,
        legacy_nonce=legacy_nonce,
        legacy_ciphertext=legacy_ciphertext,
        legacy_aad=b"legacy-aad",
    ) == b"legacy payload"


def test_nist_aes_256_gcm_known_answer() -> None:
    cipher = AESGCM(bytes(32))

    encrypted = cipher.encrypt(bytes(12), bytes(16), None)

    assert encrypted.hex() == (
        "cea7403d4d606b6e074ec5d3baf39d18"
        "d0d1c8a799996bf0265b98b5d48ab919"
    )


def context_for(
    *, tenant_id: int = 42, resource_id: str = "m-7"
) -> KeyContext:
    return KeyContext(
        environment="local",
        service="dwp-agent",
        purpose="payload",
        tenant_id=tenant_id,
        resource_type="message",
        resource_id=resource_id,
        field="body",
    )


def service_for(encoded_key: str, *, version: str) -> EnvelopeEncryptionService:
    return EnvelopeEncryptionService(provider_for(encoded_key, version=version))


def provider_for(encoded_key: str, *, version: str) -> MaterialEnvelopeKeyProvider:
    return MaterialEnvelopeKeyProvider(
        VersionedKeyMaterial(
            provider_id="local-inline",
            active_version=version,
            active_key=encoded_key,
        ),
        "local://dwp-agent/payload",
    )


def key(byte: int) -> str:
    return base64.b64encode(bytes([byte]) * 32).decode("ascii")


GOLDEN_ENVELOPE = (
    "dwp2.RFdQMgAAAAdBMjU2R0NNAAAACmR3cC1hYWQtdjEAAAAMbG9jYWwtaW5saW5l"
    "AAAAGWxvY2FsOi8vZHdwLWFnZW50L3BheWxvYWQAAAAIbG9jYWwtdjEAAAAJQTI1"
    "NkdDTUtXAAAAPBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB"
    "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEAAAAAwgICAgICAgICAgICAAAAAQMDAwMD"
    "AwMDAwMDAwMDAwMAAAACBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQA"
)
