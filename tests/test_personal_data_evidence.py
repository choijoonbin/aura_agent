from __future__ import annotations

import base64
import hashlib
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from dwp_agent.personal_data_certificate_attestation import (
    KEY_ID_ENV,
    PUBLIC_KEY_ENV,
    PersonalDataCertificateAttestationError,
    certificate_signing_payload,
    verify_certificate_attestation,
)
from dwp_agent.personal_data_evidence_contracts import PersonalDataEvidenceAction
from dwp_agent.personal_data_evidence_pdf import render_deletion_certificate
from dwp_agent.personal_data_evidence_provider import (
    HttpPersonalDataEvidenceProvider,
    PersonalDataEvidenceProviderContext,
    PersonalDataEvidenceProviderError,
    PersonalDataEvidenceProviderResponse,
    personal_data_evidence_capability,
)


def _configure(monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
    monkeypatch.setenv("DWP_PERSONAL_DATA_EVIDENCE_ALLOWED_HOSTS", "provider.test")
    monkeypatch.setenv(
        f"DWP_PERSONAL_DATA_EVIDENCE_{suffix}_URL",
        "https://provider.test/v1/evidence",
    )
    monkeypatch.setenv(
        f"DWP_PERSONAL_DATA_EVIDENCE_{suffix}_TOKEN",
        "provider-token-that-is-long-enough",
    )


def test_provider_is_allowlisted_idempotent_and_receipt_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, "BACKUP_LEDGER")
    command_id = uuid4()
    job_id = uuid4()
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["headers"] = request.headers
        observed["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "data": {
                    "commandId": str(command_id),
                    "deletionJobId": str(job_id),
                    "action": "BACKUP_LEDGER",
                    "evidenceDigest": "a" * 64,
                    "providerReceiptId": "backup-receipt-1",
                    "state": "COMPLETED",
                    "result": {
                        "schemaVersion": 1,
                        "ledgerScope": "latest",
                        "destroyedPartitionIds": ["backup-partition-1"],
                        "retainedPartitionIds": [],
                        "ledgerEntryIds": ["ledger-entry-1"],
                        "observedAt": "2026-09-17T00:00:00Z",
                    },
                }
            },
        )

    provider = HttpPersonalDataEvidenceProvider(
        PersonalDataEvidenceAction.BACKUP_LEDGER,
        transport=httpx.MockTransport(handler),
    )
    result = provider.execute(
        PersonalDataEvidenceProviderContext(
            command_id=command_id,
            deletion_job_id=job_id,
            tenant_id=7001,
            user_id="member-1",
            correlation_id="correlation-1",
            action=PersonalDataEvidenceAction.BACKUP_LEDGER,
            evidence_digest="a" * 64,
            evidence={"deletionJobId": str(job_id)},
            parameters={},
        )
    )

    assert result.provider_receipt_id == "backup-receipt-1"
    headers = observed["headers"]
    assert headers["x-dwp-idempotency-key"] == str(command_id)
    assert headers["x-dwp-tenant-id"] == "7001"
    assert headers["x-dwp-user-id"] == "member-1"


def test_provider_rejects_unbound_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, "SIEM_SYNC")
    command_id = uuid4()
    job_id = uuid4()
    provider = HttpPersonalDataEvidenceProvider(
        PersonalDataEvidenceAction.SIEM_SYNC,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "commandId": str(uuid4()),
                    "deletionJobId": str(job_id),
                    "action": "SIEM_SYNC",
                    "evidenceDigest": "b" * 64,
                    "providerReceiptId": "siem-1",
                    "state": "COMPLETED",
                    "result": {
                        "schemaVersion": 1,
                        "syncId": "siem-1",
                        "destination": "tenant-siem",
                        "acceptedEventCount": 1,
                        "sourceDigest": "b" * 64,
                        "acceptedAt": "2026-09-17T00:00:00Z",
                    },
                },
            )
        ),
    )

    with pytest.raises(PersonalDataEvidenceProviderError) as raised:
        provider.execute(
            PersonalDataEvidenceProviderContext(
                command_id=command_id,
                deletion_job_id=job_id,
                tenant_id=7001,
                user_id="member-1",
                correlation_id="correlation-1",
                action=PersonalDataEvidenceAction.SIEM_SYNC,
                evidence_digest="b" * 64,
                evidence={},
                parameters={},
            )
        )
    assert raised.value.code == "PERSONAL_DATA_EVIDENCE_PROVIDER_BINDING_INVALID"


def test_provider_rejects_blank_terminal_receipt_and_signature_evidence() -> None:
    base = {
        "commandId": str(uuid4()),
        "deletionJobId": str(uuid4()),
        "evidenceDigest": "a" * 64,
        "state": "COMPLETED",
        "result": {},
    }
    with pytest.raises(ValidationError, match="non-empty receipt"):
        PersonalDataEvidenceProviderResponse.model_validate(
            {**base, "action": "BACKUP_LEDGER", "providerReceiptId": "   "}
        )
    for update, message in (
        ({"providerReceiptId": "certificate-1", "signature": " " * 16,
          "signingKeyId": "key-1", "signingAlgorithm": "ED25519"}, "signature"),
        ({"providerReceiptId": "certificate-1", "signature": "signed-value-1234",
          "signingKeyId": "   ", "signingAlgorithm": "ED25519"}, "blank"),
    ):
        with pytest.raises(ValidationError, match=message):
            PersonalDataEvidenceProviderResponse.model_validate(
                {**base, **update, "action": "SIGNED_CERTIFICATE"}
            )


def test_capability_requires_exact_allowlisted_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, "SRE_ESCALATION")
    configured = personal_data_evidence_capability(
        PersonalDataEvidenceAction.SRE_ESCALATION
    )
    monkeypatch.setenv("DWP_PERSONAL_DATA_EVIDENCE_ALLOWED_HOSTS", "other.test")
    unavailable = personal_data_evidence_capability(
        PersonalDataEvidenceAction.SRE_ESCALATION
    )

    assert configured.available is True
    assert configured.reason_code is None
    assert unavailable.available is False
    assert unavailable.reason_code == "DELETION_SRE_SUPPORT_NOT_CONFIGURED"
    assert unavailable.recovery_hint


def test_signed_certificate_verifies_the_exact_governed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv(PUBLIC_KEY_ENV, public_pem)
    monkeypatch.setenv(KEY_ID_ENV, "tenant-signing-key-1")
    context = PersonalDataEvidenceProviderContext(
        command_id=uuid4(),
        deletion_job_id=uuid4(),
        tenant_id=7001,
        user_id="member-1",
        correlation_id="correlation-1",
        action=PersonalDataEvidenceAction.SIGNED_CERTIFICATE,
        evidence_digest="d" * 64,
        evidence={},
        parameters={},
    )
    payload = certificate_signing_payload(context, "certificate-receipt-1")
    signature = base64.urlsafe_b64encode(private_key.sign(payload)).rstrip(b"=").decode()
    response = PersonalDataEvidenceProviderResponse.model_validate(
        {
            "commandId": context.command_id,
            "deletionJobId": context.deletion_job_id,
            "action": context.action,
            "evidenceDigest": context.evidence_digest,
            "providerReceiptId": "certificate-receipt-1",
            "state": "COMPLETED",
            "result": {},
            "signature": signature,
            "signingKeyId": "tenant-signing-key-1",
            "signingAlgorithm": "ED25519",
        }
    )

    verified = verify_certificate_attestation(context, response)

    assert verified.signed_payload_sha256 == hashlib.sha256(payload).hexdigest()
    assert base64.urlsafe_b64decode(
        verified.signed_payload_base64url
        + "=" * (-len(verified.signed_payload_base64url) % 4)
    ) == payload
    assert len(verified.signing_key_fingerprint) == 64
    tampered = response.model_copy(update={"provider_receipt_id": "fabricated-receipt"})
    with pytest.raises(PersonalDataCertificateAttestationError, match="SIGNATURE_INVALID"):
        verify_certificate_attestation(context, tampered)


def test_signed_certificate_capability_requires_a_trusted_verification_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, "SIGNING_KMS")
    assert not personal_data_evidence_capability(
        PersonalDataEvidenceAction.SIGNED_CERTIFICATE
    ).available

    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        PUBLIC_KEY_ENV,
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )
    monkeypatch.setenv(KEY_ID_ENV, "tenant-signing-key-1")
    assert personal_data_evidence_capability(
        PersonalDataEvidenceAction.SIGNED_CERTIFICATE
    ).available

    for unsafe_key in (
        ec.generate_private_key(ec.SECP384R1()).public_key(),
        rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key(),
    ):
        monkeypatch.setenv(
            PUBLIC_KEY_ENV,
            unsafe_key.public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode(),
        )
        assert not personal_data_evidence_capability(
            PersonalDataEvidenceAction.SIGNED_CERTIFICATE
        ).available


@pytest.mark.parametrize(
    ("action", "result"),
    [
        ("BACKUP_LEDGER", {}),
        (
            "SRE_ESCALATION",
            {
                "schemaVersion": 1,
                "caseId": "case-1",
                "queue": "privacy-sre",
                "severity": "P2",
                "state": "QUEUED",
                "acceptedAt": "2026-09-17T00:00:00Z",
            },
        ),
        (
            "LEGAL_HOLD_APPEAL",
            {
                "schemaVersion": 1,
                "holdId": "hold-1",
                "appealId": "appeal-1",
                "state": "SUBMITTED",
            },
        ),
        (
            "SIEM_SYNC",
            {
                "schemaVersion": 1,
                "syncId": "sync-1",
                "destination": "tenant-siem",
                "acceptedEventCount": 0,
                "sourceDigest": "a" * 64,
                "acceptedAt": "2026-09-17T00:00:00Z",
            },
        ),
    ],
)
def test_completed_provider_actions_require_typed_effect_receipts(
    action: str,
    result: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        PersonalDataEvidenceProviderResponse.model_validate(
            {
                "commandId": uuid4(),
                "deletionJobId": uuid4(),
                "action": action,
                "evidenceDigest": "a" * 64,
                "providerReceiptId": "receipt-1",
                "state": "COMPLETED",
                "result": result,
            }
        )


def test_siem_outcome_must_bind_the_exact_source_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, "SIEM_SYNC")
    command_id = uuid4()
    job_id = uuid4()
    provider = HttpPersonalDataEvidenceProvider(
        PersonalDataEvidenceAction.SIEM_SYNC,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "commandId": str(command_id),
                    "deletionJobId": str(job_id),
                    "action": "SIEM_SYNC",
                    "evidenceDigest": "b" * 64,
                    "providerReceiptId": "siem-1",
                    "state": "COMPLETED",
                    "result": {
                        "schemaVersion": 1,
                        "syncId": "sync-1",
                        "destination": "tenant-siem",
                        "acceptedEventCount": 1,
                        "sourceDigest": "c" * 64,
                        "acceptedAt": "2026-09-17T00:00:00Z",
                    },
                },
            )
        ),
    )
    with pytest.raises(PersonalDataEvidenceProviderError) as raised:
        provider.execute(
            PersonalDataEvidenceProviderContext(
                command_id=command_id,
                deletion_job_id=job_id,
                tenant_id=7001,
                user_id="member-1",
                correlation_id="correlation-1",
                action=PersonalDataEvidenceAction.SIEM_SYNC,
                evidence_digest="b" * 64,
                evidence={},
                parameters={},
            )
        )
    assert raised.value.code == "PERSONAL_DATA_EVIDENCE_PROVIDER_RECEIPT_INVALID"

def test_internal_certificate_renderer_emits_valid_pdf() -> None:
    rsa_sized_signature = "s" * 342
    signed_payload = "p" * 420
    pdf = render_deletion_certificate(
        {
            "Deletion job": uuid4(),
            "Evidence digest": "c" * 64,
            "Signed payload base64url": signed_payload,
            "Signature": rsa_sized_signature,
        }
    )
    assert pdf.startswith(b"%PDF-1.4")
    assert b"startxref" in pdf
    assert pdf.endswith(b"%%EOF\n")
    assert signed_payload[-60:].encode() in pdf
    assert rsa_sized_signature[-54:].encode() in pdf
