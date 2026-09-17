from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Protocol

from .dwaion_workflow_contracts import WorkflowCapability


class AttachmentAuditSigningUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class AttachmentAuditSignature:
    algorithm: str
    signature: str
    key_fingerprint: str


class AttachmentAuditSigningProvider(Protocol):
    def capability(self, tenant_id: int) -> WorkflowCapability: ...

    def sign(self, tenant_id: int, content: bytes) -> AttachmentAuditSignature: ...


@dataclass(frozen=True)
class TenantSigningKey:
    key_id: str
    secret: bytes


class EnvironmentAttachmentAuditSigningProvider:
    """Tenant-bound signing provider loaded from an explicit JSON key registry.

    DWP_ATTACHMENT_AUDIT_SIGNING_KEYS_JSON maps decimal tenant IDs to an object with
    ``keyId`` and a base64-encoded ``secret``. Missing or malformed tenant bindings
    fail closed and are surfaced through the signed-audit capability.
    """

    def __init__(self, keys: dict[int, TenantSigningKey] | None = None) -> None:
        self.keys = keys if keys is not None else _load_keys()

    def capability(self, tenant_id: int) -> WorkflowCapability:
        if tenant_id in self.keys:
            return WorkflowCapability(available=True, configured=True)
        return WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ATTACHMENT_AUDIT_SIGNING_NOT_CONFIGURED",
            recovery_hint=(
                "Configure and attest a tenant-bound attachment audit signing key."
            ),
        )

    def sign(self, tenant_id: int, content: bytes) -> AttachmentAuditSignature:
        key = self.keys.get(tenant_id)
        if key is None:
            raise AttachmentAuditSigningUnavailable(
                "ATTACHMENT_AUDIT_SIGNING_NOT_CONFIGURED"
            )
        digest = hmac.new(key.secret, content, hashlib.sha256).digest()
        key_reference = f"tenant:{tenant_id}:attachment-audit:{key.key_id}".encode()
        return AttachmentAuditSignature(
            algorithm="HMAC-SHA256",
            signature=base64.urlsafe_b64encode(digest).decode().rstrip("="),
            key_fingerprint=hashlib.sha256(key_reference).hexdigest(),
        )


def _load_keys() -> dict[int, TenantSigningKey]:
    raw = os.getenv("DWP_ATTACHMENT_AUDIT_SIGNING_KEYS_JSON", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return {}
        keys: dict[int, TenantSigningKey] = {}
        for tenant, value in payload.items():
            if not isinstance(value, dict):
                continue
            key_id = value.get("keyId")
            secret = value.get("secret")
            if not isinstance(key_id, str) or not key_id.strip() or not isinstance(secret, str):
                continue
            decoded = base64.b64decode(secret, validate=True)
            if len(decoded) < 32:
                continue
            keys[int(tenant)] = TenantSigningKey(key_id=key_id.strip(), secret=decoded)
        return keys
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
