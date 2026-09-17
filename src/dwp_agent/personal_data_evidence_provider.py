from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Mapping, Protocol
from urllib.parse import urlparse
from uuid import UUID

import httpx
from pydantic import Field, JsonValue, field_validator, model_validator

from .contract_model import ContractModel
from .dwaion_workflow_contracts import WorkflowCapability
from .personal_data_evidence_contracts import PersonalDataEvidenceAction


_ENV_SUFFIX = {
    PersonalDataEvidenceAction.BACKUP_LEDGER: "BACKUP_LEDGER",
    PersonalDataEvidenceAction.SRE_ESCALATION: "SRE_ESCALATION",
    PersonalDataEvidenceAction.LEGAL_HOLD_APPEAL: "LEGAL_HOLD_APPEAL",
    PersonalDataEvidenceAction.SIGNED_CERTIFICATE: "SIGNING_KMS",
    PersonalDataEvidenceAction.SIEM_SYNC: "SIEM_SYNC",
}

_UNAVAILABLE = {
    PersonalDataEvidenceAction.BACKUP_LEDGER: (
        "BACKUP_DESTRUCTION_LOG_NOT_CONFIGURED",
        "Configure the allowlisted backup destruction-ledger endpoint and delegated token.",
    ),
    PersonalDataEvidenceAction.SRE_ESCALATION: (
        "DELETION_SRE_SUPPORT_NOT_CONFIGURED",
        "Configure the allowlisted audited SRE escalation endpoint and delegated token.",
    ),
    PersonalDataEvidenceAction.LEGAL_HOLD_APPEAL: (
        "LEGAL_HOLD_APPEAL_NOT_CONFIGURED",
        "Configure the allowlisted compliance appeal endpoint and delegated token.",
    ),
    PersonalDataEvidenceAction.SIGNED_CERTIFICATE: (
        "SIGNED_DELETION_CERTIFICATE_NOT_CONFIGURED",
        "Configure the tenant signing/KMS endpoint, delegated token, allowed host, public verification key and exact key ID.",
    ),
    PersonalDataEvidenceAction.SIEM_SYNC: (
        "DELETION_SIEM_SYNC_NOT_CONFIGURED",
        "Configure the allowlisted governed SIEM endpoint and delegated token.",
    ),
}


class PersonalDataEvidenceProviderResponse(ContractModel):
    command_id: UUID
    deletion_job_id: UUID
    action: PersonalDataEvidenceAction
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_receipt_id: str = Field(min_length=1, max_length=240)
    state: str = Field(pattern=r"^(COMPLETED|FAILED)$")
    result: dict[str, JsonValue] = Field(default_factory=dict, max_length=40)
    safe_error_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    recovery_hint: str | None = Field(default=None, max_length=1_000)
    signature: str | None = Field(default=None, min_length=16, max_length=8_192)
    signing_key_id: str | None = Field(default=None, min_length=1, max_length=240)
    signing_algorithm: str | None = Field(
        default=None, pattern=r"^(RSA-PSS-SHA256|ECDSA-P256-SHA256|ED25519)$"
    )

    @field_validator("provider_receipt_id")
    @classmethod
    def valid_provider_receipt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Provider evidence requires a non-empty receipt identifier.")
        return normalized

    @field_validator("signature")
    @classmethod
    def valid_signature(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if len(normalized) < 16:
            raise ValueError("Certificate signature evidence must contain at least 16 characters.")
        return normalized

    @field_validator("signing_key_id", "recovery_hint")
    @classmethod
    def valid_optional_evidence(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Provider evidence fields cannot be blank.")
        return normalized

    @model_validator(mode="after")
    def terminal_contract(self) -> "PersonalDataEvidenceProviderResponse":
        if self.state == "COMPLETED":
            if self.safe_error_code is not None or self.recovery_hint is not None:
                raise ValueError("Completed provider evidence cannot include a failure.")
            if self.action == PersonalDataEvidenceAction.SIGNED_CERTIFICATE and not all(
                (self.signature, self.signing_key_id, self.signing_algorithm)
            ):
                raise ValueError("A signed certificate requires KMS signature evidence.")
            from .personal_data_evidence_provider_outcomes import (
                validate_personal_data_provider_outcome,
            )

            self.result = validate_personal_data_provider_outcome(
                self.action,
                self.result,
                evidence_digest=self.evidence_digest,
            )
        elif not (self.safe_error_code and self.recovery_hint):
            raise ValueError("Failed provider evidence requires a safe recovery receipt.")
        return self


@dataclass(frozen=True)
class PersonalDataEvidenceProviderContext:
    command_id: UUID
    deletion_job_id: UUID
    tenant_id: int
    user_id: str
    correlation_id: str
    action: PersonalDataEvidenceAction
    evidence_digest: str
    evidence: Mapping[str, object]
    parameters: Mapping[str, object]


class PersonalDataEvidenceProvider(Protocol):
    @property
    def configured(self) -> bool: ...

    def execute(
        self, context: PersonalDataEvidenceProviderContext
    ) -> PersonalDataEvidenceProviderResponse: ...


class PersonalDataEvidenceProviderError(RuntimeError):
    def __init__(self, code: str, recovery_hint: str) -> None:
        super().__init__(code)
        self.code = code
        self.recovery_hint = recovery_hint


class HttpPersonalDataEvidenceProvider:
    """Strict per-action connector with tenant- and command-bound receipts."""

    def __init__(
        self,
        action: PersonalDataEvidenceAction,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.action = action
        suffix = _ENV_SUFFIX[action]
        prefix = f"DWP_PERSONAL_DATA_EVIDENCE_{suffix}"
        self.url = os.getenv(f"{prefix}_URL", "").strip()
        self.token = os.getenv(f"{prefix}_TOKEN", "").strip()
        self.allowed_hosts = frozenset(
            item.strip().lower()
            for item in os.getenv(
                "DWP_PERSONAL_DATA_EVIDENCE_ALLOWED_HOSTS", ""
            ).split(",")
            if item.strip()
        )
        self.transport = transport

    @property
    def configured(self) -> bool:
        provider_configured = bool(
            self.url
            and len(self.token) >= 24
            and _is_allowlisted_url(self.url, self.allowed_hosts)
        )
        if self.action != PersonalDataEvidenceAction.SIGNED_CERTIFICATE:
            return provider_configured
        from .personal_data_certificate_attestation import (
            certificate_attestation_configured,
        )

        return provider_configured and certificate_attestation_configured()

    def execute(
        self, context: PersonalDataEvidenceProviderContext
    ) -> PersonalDataEvidenceProviderResponse:
        if context.action != self.action or not self.configured:
            _, recovery = _UNAVAILABLE[self.action]
            raise PersonalDataEvidenceProviderError(
                "PERSONAL_DATA_EVIDENCE_PROVIDER_NOT_CONFIGURED", recovery
            )
        body = {
            "commandId": str(context.command_id),
            "deletionJobId": str(context.deletion_job_id),
            "action": context.action.value,
            "evidenceDigest": context.evidence_digest,
            "evidence": dict(context.evidence),
            "parameters": dict(context.parameters),
        }
        try:
            with httpx.Client(
                timeout=20.0,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = client.post(
                    self.url,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "X-DWP-Tenant-ID": str(context.tenant_id),
                        "X-DWP-User-ID": context.user_id,
                        "X-DWP-Command-ID": str(context.command_id),
                        "X-DWP-Idempotency-Key": str(context.command_id),
                        "X-Correlation-ID": context.correlation_id,
                    },
                )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise PersonalDataEvidenceProviderError(
                "PERSONAL_DATA_EVIDENCE_PROVIDER_UNREACHABLE",
                "Verify the provider endpoint and retry with the same command ID.",
            ) from error
        if response.status_code < 200 or response.status_code >= 300:
            raise PersonalDataEvidenceProviderError(
                "PERSONAL_DATA_EVIDENCE_PROVIDER_REJECTED",
                "Review provider authorization and evidence policy, then retry with the same command ID.",
            )
        try:
            value = response.json()
            if isinstance(value, dict) and isinstance(value.get("data"), dict):
                value = value["data"]
            result = PersonalDataEvidenceProviderResponse.model_validate(value)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise PersonalDataEvidenceProviderError(
                "PERSONAL_DATA_EVIDENCE_PROVIDER_RECEIPT_INVALID",
                "Repair the provider receipt binding and retry with the same command ID.",
            ) from error
        if (
            result.command_id != context.command_id
            or result.deletion_job_id != context.deletion_job_id
            or result.action != context.action
            or result.evidence_digest != context.evidence_digest
        ):
            raise PersonalDataEvidenceProviderError(
                "PERSONAL_DATA_EVIDENCE_PROVIDER_BINDING_INVALID",
                "Reject the receipt and repair command, action and digest propagation.",
            )
        if result.state == "COMPLETED":
            from .personal_data_evidence_provider_outcomes import (
                validate_personal_data_provider_outcome,
            )

            try:
                result.result = validate_personal_data_provider_outcome(
                    result.action,
                    result.result,
                    evidence_digest=result.evidence_digest,
                    parameters=context.parameters,
                    evidence=context.evidence,
                )
            except (TypeError, ValueError) as error:
                raise PersonalDataEvidenceProviderError(
                    "PERSONAL_DATA_EVIDENCE_PROVIDER_BINDING_INVALID",
                    "Reject the receipt and repair its governed action outcome binding.",
                ) from error
        return result


def personal_data_evidence_capability(
    action: PersonalDataEvidenceAction,
) -> WorkflowCapability:
    configured = HttpPersonalDataEvidenceProvider(action).configured
    reason, recovery = _UNAVAILABLE[action]
    return WorkflowCapability(
        available=configured,
        configured=configured,
        reason_code=None if configured else reason,
        recovery_hint=None if configured else recovery,
    )


def build_personal_data_evidence_provider(
    action: PersonalDataEvidenceAction,
) -> PersonalDataEvidenceProvider:
    return HttpPersonalDataEvidenceProvider(action)


def _is_allowlisted_url(url: str, allowed_hosts: frozenset[str]) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.path in {"", "/"}:
        return False
    if host not in allowed_hosts:
        return False
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    return parsed.scheme == "https" or (loopback and parsed.scheme == "http" and port is not None)
