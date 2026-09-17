from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Mapping, Protocol
from urllib.parse import urlparse
from uuid import UUID

import httpx
from pydantic import Field, model_validator

from .admin_control_plane_contracts import (
    AdminCommandCapabilitiesSnapshot,
    AdminCommandCapability,
    CapabilityStatus,
    CommandProblem,
    CommandTarget,
    GovernedCommandKind,
    GovernedCommandState,
)
from .admin_control_plane_registry import (
    ADMIN_COMMAND_REGISTRY,
    AdminCommandExecutionMode,
    AdminCommandSpec,
)
from .contract_model import ContractModel
from .admin_control_plane_result_support import external_result_contract_available


class AdminCommandExecutionRejected(RuntimeError):
    """A permanent, safely reportable command failure."""

    def __init__(self, code: str, detail: str, recovery_hint: str) -> None:
        super().__init__(detail)
        self.problem = CommandProblem(
            code=code,
            detail=detail,
            recovery_hint=recovery_hint,
        )


class AdminCommandExecutionTransient(RuntimeError):
    """An infrastructure failure that should consume the outbox retry budget."""


@dataclass(frozen=True)
class AdminCommandExecutionContext:
    command_id: UUID
    attempt_id: UUID
    tenant_id: int
    maker_user_id: str
    correlation_id: str
    kind: GovernedCommandKind
    state: GovernedCommandState
    command_revision: int
    target_type: str
    target_id: str
    expected_target_version: int
    payload: dict[str, object]
    review: dict[str, object]
    rollback_requested: bool
    rollback_source_receipt_ref: str | None


@dataclass(frozen=True)
class AdminCommandExecutionResult:
    state: GovernedCommandState
    summary: str | None = None
    domain_receipt_ref: str | None = None
    rollback_ref: str | None = None
    snapshot: dict[str, object] | None = None
    version: int | None = None
    problem: CommandProblem | None = None


class AdminCommandAdapterResponse(ContractModel):
    command_id: UUID
    tenant_id: int | None = Field(default=None, ge=1)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=160)
    attempt_id: UUID | None = None
    kind: GovernedCommandKind | None = None
    target: CommandTarget | None = None
    expected_version: int | None = Field(default=None, ge=0)
    state: GovernedCommandState
    result_summary: str | None = Field(default=None, max_length=2_000)
    domain_receipt_ref: str | None = Field(default=None, max_length=500)
    rollback_ref: str | None = Field(default=None, max_length=500)
    result_snapshot: dict[str, object] | None = None
    result_version: int | None = Field(default=None, ge=1)
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    problem: CommandProblem | None = None

    @model_validator(mode="after")
    def terminal_contract(self) -> "AdminCommandAdapterResponse":
        if self.state not in {
            GovernedCommandState.SUCCEEDED,
            GovernedCommandState.PARTIAL,
            GovernedCommandState.FAILED,
            GovernedCommandState.ROLLED_BACK,
        }:
            raise ValueError("An admin command adapter must return a terminal state.")
        if not (
            self.tenant_id is not None
            and self.correlation_id
            and self.correlation_id.strip()
            and
            self.attempt_id
            and self.kind
            and self.target
            and self.expected_version is not None
        ):
            raise ValueError("An adapter result requires governed command context binding.")
        self.correlation_id = self.correlation_id.strip()
        if self.state in {GovernedCommandState.SUCCEEDED, GovernedCommandState.ROLLED_BACK}:
            if not (
                self.result_summary
                and self.result_summary.strip()
                and self.domain_receipt_ref
                and self.domain_receipt_ref.strip()
                and self.result_snapshot is not None
                and self.result_version is not None
                and self.result_sha256
            ):
                raise ValueError(
                    "A successful adapter result requires a context-bound receipt and versioned snapshot."
                )
            self.result_summary = self.result_summary.strip()
            self.domain_receipt_ref = self.domain_receipt_ref.strip()
        if self.state in {GovernedCommandState.PARTIAL, GovernedCommandState.FAILED} and self.problem is None:
            raise ValueError("An incomplete adapter result requires a safe problem.")
        if self.state == GovernedCommandState.ROLLED_BACK:
            if not self.rollback_ref or not self.rollback_ref.strip():
                raise ValueError("A rollback adapter result must identify the reversed receipt.")
            self.rollback_ref = self.rollback_ref.strip()
        return self


class AdminCommandAdapter(Protocol):
    def execute(
        self,
        *,
        service: str,
        context: AdminCommandExecutionContext,
        spec: AdminCommandSpec,
    ) -> AdminCommandAdapterResponse: ...


class HttpAdminCommandAdapter:
    """Calls an explicitly configured, allowlisted domain command endpoint."""

    def __init__(
        self,
        service: str,
        *,
        transport: httpx.BaseTransport | None = None,
        maximum_response_bytes: int = 1_048_576,
    ) -> None:
        self.service = service
        suffix = _service_env_suffix(service)
        self.url = os.getenv(f"DWP_ADMIN_CONTROL_{suffix}_URL", "").strip()
        self.token = os.getenv(f"DWP_ADMIN_CONTROL_{suffix}_TOKEN", "").strip()
        self.transport = transport
        self.maximum_response_bytes = maximum_response_bytes

    @property
    def configured(self) -> bool:
        return bool(self.url and len(self.token) >= 24 and _safe_adapter_url(self.url))

    def execute(
        self,
        *,
        service: str,
        context: AdminCommandExecutionContext,
        spec: AdminCommandSpec,
    ) -> AdminCommandAdapterResponse:
        if service != self.service or not self.configured:
            raise AdminCommandExecutionRejected(
                "ADMIN_ADAPTER_NOT_CONFIGURED",
                f"The {service} command adapter is not configured.",
                f"Configure the allowlisted {service} adapter URL and delegated worker token, then retry.",
            )
        body = {
            "commandId": str(context.command_id),
            "attemptId": str(context.attempt_id),
            "kind": context.kind.value,
            "family": spec.family,
            "target": {"type": context.target_type, "id": context.target_id},
            "expectedVersion": context.expected_target_version,
            "payload": context.payload,
            "review": context.review,
            "rollbackRequested": context.rollback_requested,
            "rollbackSourceReceiptRef": context.rollback_source_receipt_ref,
        }
        try:
            with httpx.Client(
                timeout=20.0,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "POST",
                    self.url,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "X-DWP-Tenant-ID": str(context.tenant_id),
                        "X-DWP-Command-ID": str(context.command_id),
                        "X-DWP-Idempotency-Key": str(context.attempt_id),
                        "X-Correlation-ID": context.correlation_id,
                    },
                ) as response:
                    if response.status_code >= 500 or response.status_code == 429:
                        raise AdminCommandExecutionTransient(
                            "The domain command adapter is temporarily unavailable."
                        )
                    if not 200 <= response.status_code < 300:
                        raise AdminCommandExecutionRejected(
                            "ADMIN_ADAPTER_REJECTED",
                            f"The {service} adapter rejected the governed command with HTTP {response.status_code}.",
                            "Review the target version, delegated identity, capability and command evidence before retrying.",
                        )
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65_536):
                        content.extend(chunk)
                        if len(content) > self.maximum_response_bytes:
                            raise AdminCommandExecutionRejected(
                                "ADMIN_ADAPTER_RECEIPT_TOO_LARGE",
                                f"The {service} adapter receipt exceeds the allowed response size.",
                                "Return only the bounded governed receipt and store large evidence externally.",
                            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise AdminCommandExecutionTransient("The domain command adapter is temporarily unreachable.") from error
        try:
            value = json.loads(bytes(content))
            if isinstance(value, dict) and isinstance(value.get("data"), dict):
                value = value["data"]
            result = AdminCommandAdapterResponse.model_validate(value)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise AdminCommandExecutionRejected(
                "ADMIN_ADAPTER_RECEIPT_INVALID",
                f"The {service} adapter returned an invalid governed receipt.",
                "Repair the adapter response contract and retry with the same idempotency key.",
            ) from error
        if result.command_id != context.command_id:
            raise AdminCommandExecutionRejected(
                "ADMIN_ADAPTER_BINDING_INVALID",
                "The domain receipt does not match the governed command.",
                "Reject the receipt and repair command ID propagation in the adapter.",
            )
        return result


def resolve_admin_command_adapter(
    service: str,
    adapters: Mapping[str, AdminCommandAdapter],
) -> AdminCommandAdapter:
    adapter = adapters.get(service)
    if adapter is not None:
        return adapter
    configured = HttpAdminCommandAdapter(service)
    if not configured.configured:
        raise AdminCommandExecutionRejected(
            "ADMIN_ADAPTER_NOT_CONFIGURED",
            f"The {service} command adapter is not configured.",
            f"Configure the allowlisted {service} adapter URL and delegated worker token, then retry.",
        )
    return configured


def admin_command_capabilities() -> AdminCommandCapabilitiesSnapshot:
    from .governed_worker_runtime import governed_worker_available

    worker_available = governed_worker_available("ADMIN_CONTROL_COMMAND")
    commands: list[AdminCommandCapability] = []
    for kind, spec in ADMIN_COMMAND_REGISTRY.items():
        if spec.mode == AdminCommandExecutionMode.INTERNAL:
            configured = worker_available
            status = CapabilityStatus.AVAILABLE if configured else CapabilityStatus.UNAVAILABLE
            reason = None if configured else "The governed admin command worker is not running."
            recovery = None if configured else "Enable the governed worker runtime and verify its database schema."
        else:
            contract_available = external_result_contract_available(kind, spec)
            adapter_configured = HttpAdminCommandAdapter(spec.service or "").configured
            configured = worker_available and adapter_configured and contract_available
            if not contract_available:
                status = CapabilityStatus.NOT_CONFIGURED
                reason = f"{kind.value} has no registered typed external result contract."
                recovery = "Implement and register the authoritative result DTO before enabling this command."
            elif not adapter_configured:
                status = CapabilityStatus.NOT_CONFIGURED
                reason = f"The {spec.service} domain command adapter is not configured."
                recovery = f"Configure the allowlisted {spec.service} adapter URL and delegated worker token."
            elif not worker_available:
                status = CapabilityStatus.UNAVAILABLE
                reason = "The governed admin command worker is not running."
                recovery = "Enable the governed worker runtime and verify its database schema."
            else:
                status = CapabilityStatus.AVAILABLE
                reason = None
                recovery = None
        commands.append(
            AdminCommandCapability(
                kind=kind,
                family=spec.family,
                execution_mode=spec.mode.value,
                status=status,
                configured=configured,
                reason=reason,
                recovery_hint=recovery,
            )
        )
    return AdminCommandCapabilitiesSnapshot(
        generated_at=datetime.now(UTC),
        worker_available=worker_available,
        commands=commands,
    )


def _service_env_suffix(service: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", service.upper()).strip("_")


def _safe_adapter_url(value: str) -> bool:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if parsed.username or parsed.password or parsed.fragment:
        return False
    if hostname in {"127.0.0.1", "localhost", "::1"}:
        return parsed.scheme in {"http", "https"}
    allowed_hosts = {
        item.strip().lower()
        for item in os.getenv("DWP_ADMIN_CONTROL_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    return parsed.scheme == "https" and hostname in allowed_hosts
