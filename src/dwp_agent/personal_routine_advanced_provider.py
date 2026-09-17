from __future__ import annotations

import os
import hashlib
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import UUID

import httpx
from pydantic import Field, TypeAdapter, field_validator, model_validator

from .contract_model import ContractModel
from .dwaion_workflow_contracts import WorkflowCapability
from .governed_domain_core import canonical_json_bytes
from .personal_routine_advanced_contracts import (
    RoutineAdvancedCommandKind,
    RoutineAdvancedPayload,
    RoutineAdvancedProviderOutcome,
    RoutineChangeApprovalPayload,
)


_PAYLOAD_ADAPTER = TypeAdapter(RoutineAdvancedPayload)


class RoutineAdvancedProviderResult(ContractModel):
    command_id: UUID
    kind: RoutineAdvancedCommandKind
    state: str = Field(pattern=r"^(SUCCEEDED|PARTIAL)$")
    provider_receipt_id: str = Field(min_length=1, max_length=240)
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    applied_revision: int | None = Field(default=None, ge=1)
    result: RoutineAdvancedProviderOutcome
    problem_code: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    problem_detail: str | None = Field(default=None, max_length=500)
    recovery_hint: str | None = Field(default=None, max_length=1_000)

    @field_validator("provider_receipt_id")
    @classmethod
    def normalize_provider_receipt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Provider receipt ID must not be blank.")
        return normalized

    @model_validator(mode="after")
    def coherent_result(self) -> "RoutineAdvancedProviderResult":
        problem = (self.problem_code, self.problem_detail, self.recovery_hint)
        if self.state == "SUCCEEDED" and any(value is not None for value in problem):
            raise ValueError("A successful routine command cannot include a problem.")
        if self.state == "PARTIAL" and not all(value is not None for value in problem):
            raise ValueError("A partial routine command requires recovery evidence.")
        expected_outcome = "APPLIED" if self.state == "SUCCEEDED" else "PARTIALLY_APPLIED"
        if self.result.outcome != expected_outcome:
            raise ValueError("The provider outcome must match its terminal state.")
        if self.result.kind != self.kind:
            raise ValueError("The provider outcome kind must match its receipt kind.")
        if provider_result_digest(self.result) != self.result_sha256:
            raise ValueError("The provider result digest is not canonical.")
        return self


@dataclass(frozen=True)
class RoutineAdvancedProviderContext:
    command_id: UUID
    routine_id: UUID
    tenant_id: int
    user_id: str
    correlation_id: str
    kind: RoutineAdvancedCommandKind
    expected_revision: int
    payload: dict[str, object]


class RoutineAdvancedProviderError(RuntimeError):
    def __init__(self, code: str, recovery_hint: str) -> None:
        super().__init__(code)
        self.code = code
        self.recovery_hint = recovery_hint


class HttpRoutineAdvancedProvider:
    def __init__(
        self,
        kind: RoutineAdvancedCommandKind,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.kind = kind
        prefix = f"DWP_ROUTINE_ADVANCED_{kind.value}"
        self.url = os.getenv(f"{prefix}_URL", "").strip()
        self.token = os.getenv(f"{prefix}_TOKEN", "").strip()
        self.allowed_hosts = frozenset(
            item.strip().lower()
            for item in os.getenv("DWP_ROUTINE_ADVANCED_ALLOWED_HOSTS", "").split(",")
            if item.strip()
        )
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(
            self.kind != RoutineAdvancedCommandKind.CHANGE_APPROVAL
            and self.url
            and len(self.token) >= 24
            and _allowlisted(self.url, self.allowed_hosts)
        )

    def execute(
        self, context: RoutineAdvancedProviderContext
    ) -> RoutineAdvancedProviderResult:
        if not self.configured or context.kind != self.kind:
            raise RoutineAdvancedProviderError(
                "ROUTINE_ADVANCED_PROVIDER_NOT_CONFIGURED",
                "Configure the allowlisted routine provider endpoint and delegated token.",
            )
        try:
            with httpx.Client(
                timeout=20.0, follow_redirects=False, transport=self.transport
            ) as client:
                response = client.post(
                    self.url,
                    json={
                        "commandId": str(context.command_id),
                        "routineId": str(context.routine_id),
                        "kind": context.kind.value,
                        "expectedRevision": context.expected_revision,
                        "payload": context.payload,
                    },
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
            raise RoutineAdvancedProviderError(
                "ROUTINE_ADVANCED_PROVIDER_UNREACHABLE",
                "Restore the configured routine provider and retry with the same command ID.",
            ) from error
        if response.status_code >= 400:
            raise RoutineAdvancedProviderError(
                "ROUTINE_ADVANCED_PROVIDER_REJECTED",
                "Review provider authorization and retry with the same command ID.",
            )
        try:
            value = response.json()
            if isinstance(value, dict) and isinstance(value.get("data"), dict):
                value = value["data"]
            result = RoutineAdvancedProviderResult.model_validate(value)
        except (ValueError, TypeError) as error:
            raise RoutineAdvancedProviderError(
                "ROUTINE_ADVANCED_PROVIDER_RECEIPT_INVALID",
                "Repair the provider receipt contract before retrying.",
            ) from error
        ensure_provider_result_bound(context, result)
        return result


def routine_advanced_capability(kind: RoutineAdvancedCommandKind) -> WorkflowCapability:
    if kind == RoutineAdvancedCommandKind.CHANGE_APPROVAL:
        return WorkflowCapability(available=True, configured=True)
    configured = HttpRoutineAdvancedProvider(kind).configured
    return WorkflowCapability(
        available=configured,
        configured=configured,
        reason_code=None if configured else f"ROUTINE_{kind.value}_NOT_CONFIGURED",
        recovery_hint=(
            None
            if configured
            else "Configure the allowlisted advanced routine provider and delegated token."
        ),
    )


def provider_result_digest(result: RoutineAdvancedProviderOutcome) -> str:
    return hashlib.sha256(
        canonical_json_bytes(result.model_dump(mode="json", by_alias=True))
    ).hexdigest()


def internal_change_approval_result(
    *,
    command_id: UUID,
    routine_id: UUID,
    expected_revision: int,
    applied_revision: int,
    payload: RoutineChangeApprovalPayload,
    decision_id: UUID,
) -> RoutineAdvancedProviderResult:
    outcome = RoutineAdvancedProviderOutcome(
        routine_id=routine_id,
        expected_revision=expected_revision,
        kind=RoutineAdvancedCommandKind.CHANGE_APPROVAL,
        outcome="APPLIED",
        applied_payload=payload,
        evidence_ref=f"checker-decision:{decision_id}",
    )
    return RoutineAdvancedProviderResult(
        command_id=command_id,
        kind=RoutineAdvancedCommandKind.CHANGE_APPROVAL,
        state="SUCCEEDED",
        provider_receipt_id=f"internal-maker-checker:{command_id}",
        result_sha256=provider_result_digest(outcome),
        applied_revision=applied_revision,
        result=outcome,
    )


def execute_bound_provider(
    provider: HttpRoutineAdvancedProvider,
    context: RoutineAdvancedProviderContext,
) -> RoutineAdvancedProviderResult:
    result = provider.execute(context)
    ensure_provider_result_bound(context, result)
    return result


def ensure_provider_result_bound(
    context: RoutineAdvancedProviderContext,
    result: RoutineAdvancedProviderResult,
) -> None:
    try:
        expected_payload = _PAYLOAD_ADAPTER.validate_python(context.payload)
    except (ValueError, TypeError) as error:
        raise RoutineAdvancedProviderError(
            "ROUTINE_ADVANCED_PROVIDER_BINDING_INVALID",
            "Reject the receipt because its command payload is invalid.",
        ) from error
    outcome = result.result
    if (
        result.command_id != context.command_id
        or result.kind != context.kind
        or outcome.routine_id != context.routine_id
        or outcome.expected_revision != context.expected_revision
        or outcome.kind != context.kind
        or outcome.applied_payload.model_dump(mode="json", by_alias=True)
        != expected_payload.model_dump(mode="json", by_alias=True)
    ):
        raise RoutineAdvancedProviderError(
            "ROUTINE_ADVANCED_PROVIDER_BINDING_INVALID",
            "Reject the receipt and repair routine, revision, kind, and payload propagation.",
        )


def _allowlisted(url: str, allowed_hosts: frozenset[str]) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if not host or host not in allowed_hosts or parsed.username or parsed.password:
        return False
    if parsed.query or parsed.fragment or parsed.path in {"", "/"}:
        return False
    return parsed.scheme == "https" or (
        parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}
    )
