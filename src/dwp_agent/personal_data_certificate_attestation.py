from __future__ import annotations

import base64
import binascii
import hashlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from .canonical_json import canonical_json_bytes

if TYPE_CHECKING:
    from .personal_data_evidence_provider import (
        PersonalDataEvidenceProviderContext,
        PersonalDataEvidenceProviderResponse,
    )


PUBLIC_KEY_ENV = "DWP_PERSONAL_DATA_EVIDENCE_SIGNING_KMS_PUBLIC_KEY_PEM"
KEY_ID_ENV = "DWP_PERSONAL_DATA_EVIDENCE_SIGNING_KMS_KEY_ID"


class PersonalDataCertificateAttestationError(RuntimeError):
    def __init__(self, code: str, recovery_hint: str) -> None:
        super().__init__(code)
        self.code = code
        self.recovery_hint = recovery_hint


@dataclass(frozen=True)
class VerifiedCertificateAttestation:
    signed_payload_sha256: str
    signed_payload_base64url: str
    signing_key_fingerprint: str


def certificate_signing_payload(
    context: PersonalDataEvidenceProviderContext,
    provider_receipt_id: str,
) -> bytes:
    return canonical_json_bytes(
        {
            "schema": "dwp.personal-data.deletion-certificate-signing.v1",
            "commandId": str(context.command_id),
            "deletionJobId": str(context.deletion_job_id),
            "action": context.action.value,
            "evidenceDigest": context.evidence_digest,
            "providerReceiptId": provider_receipt_id,
        }
    )


def certificate_attestation_configured() -> bool:
    try:
        _configured_key()
    except PersonalDataCertificateAttestationError:
        return False
    return True


def verify_certificate_attestation(
    context: PersonalDataEvidenceProviderContext,
    response: PersonalDataEvidenceProviderResponse,
) -> VerifiedCertificateAttestation:
    if not response.signature or not response.signing_key_id or not response.signing_algorithm:
        raise _invalid("The signing provider omitted certificate signature evidence.")
    public_key, configured_key_id = _configured_key()
    if response.signing_key_id != configured_key_id:
        raise _invalid("The signing key identifier does not match the configured tenant key.")
    signature = _decode_signature(response.signature)
    payload = certificate_signing_payload(context, response.provider_receipt_id)
    try:
        _verify(public_key, response.signing_algorithm, signature, payload)
    except (InvalidSignature, TypeError, ValueError, UnsupportedAlgorithm) as error:
        raise _invalid(
            "The certificate signature could not be verified against the governed payload."
        ) from error
    public_der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return VerifiedCertificateAttestation(
        signed_payload_sha256=hashlib.sha256(payload).hexdigest(),
        signed_payload_base64url=base64.urlsafe_b64encode(payload).rstrip(b"=").decode(),
        signing_key_fingerprint=hashlib.sha256(public_der).hexdigest(),
    )


def _configured_key() -> tuple[object, str]:
    pem = os.getenv(PUBLIC_KEY_ENV, "").strip().replace("\\n", "\n")
    key_id = os.getenv(KEY_ID_ENV, "").strip()
    if not pem or not key_id:
        raise PersonalDataCertificateAttestationError(
            "SIGNED_DELETION_CERTIFICATE_VERIFIER_NOT_CONFIGURED",
            "Configure the tenant signing public key and exact signing key ID.",
        )
    try:
        public_key = serialization.load_pem_public_key(pem.encode("utf-8"))
    except (ValueError, TypeError, UnsupportedAlgorithm) as error:
        raise PersonalDataCertificateAttestationError(
            "SIGNED_DELETION_CERTIFICATE_KEY_INVALID",
            "Repair the configured tenant signing public key before issuing certificates.",
        ) from error
    if not isinstance(
        public_key,
        (ed25519.Ed25519PublicKey, ec.EllipticCurvePublicKey, rsa.RSAPublicKey),
    ):
        raise PersonalDataCertificateAttestationError(
            "SIGNED_DELETION_CERTIFICATE_KEY_UNSUPPORTED",
            "Use an Ed25519, P-256 ECDSA, or RSA signing public key.",
        )
    if isinstance(public_key, ec.EllipticCurvePublicKey) and not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise PersonalDataCertificateAttestationError(
            "SIGNED_DELETION_CERTIFICATE_KEY_UNSUPPORTED",
            "Configure the required P-256 ECDSA public key for ECDSA certificates.",
        )
    if isinstance(public_key, rsa.RSAPublicKey) and public_key.key_size < 2048:
        raise PersonalDataCertificateAttestationError(
            "SIGNED_DELETION_CERTIFICATE_KEY_UNSAFE",
            "Configure an RSA public key with at least 2048 bits.",
        )
    return public_key, key_id


def _decode_signature(value: str) -> bytes:
    try:
        signature = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise _invalid("The certificate signature is not valid base64url evidence.") from error
    if not signature:
        raise _invalid("The certificate signature is empty.")
    return signature


def _verify(public_key: object, algorithm: str, signature: bytes, payload: bytes) -> None:
    if algorithm == "ED25519" and isinstance(public_key, ed25519.Ed25519PublicKey):
        public_key.verify(signature, payload)
        return
    if (
        algorithm == "ECDSA-P256-SHA256"
        and isinstance(public_key, ec.EllipticCurvePublicKey)
        and isinstance(public_key.curve, ec.SECP256R1)
    ):
        public_key.verify(signature, payload, ec.ECDSA(hashes.SHA256()))
        return
    if algorithm == "RSA-PSS-SHA256" and isinstance(public_key, rsa.RSAPublicKey):
        public_key.verify(
            signature,
            payload,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )
        return
    raise _invalid("The signature algorithm does not match the configured signing key.")


def _invalid(detail: str) -> PersonalDataCertificateAttestationError:
    return PersonalDataCertificateAttestationError(
        "SIGNED_DELETION_CERTIFICATE_SIGNATURE_INVALID",
        f"{detail} Repair KMS signing configuration and retry with a new command ID.",
    )
