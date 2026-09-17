from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
from pydantic import Field, JsonValue, field_validator, model_validator

from .canonical_json import canonical_json_bytes
from .contract_model import ContractModel
from .dwaion_workflow_contracts import ResearchDeliveryType, ResearchRun
from .personal_domain_security import PersonalDomainIdentity
from .workflow_capability_contracts import WorkflowCapability


class ResearchSharePermission(StrEnum):
    VIEW = "VIEW"
    COMMENT = "COMMENT"


class ResearchHandoffParameters(ContractModel):
    locale: str = Field(pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    approval_target: str = Field(min_length=2, max_length=160)
    request_title: str = Field(min_length=2, max_length=240)
    request_reason: str = Field(min_length=10, max_length=1_000)
    request_metadata: dict[str, JsonValue] = Field(default_factory=dict, max_length=20)

    @field_validator("approval_target", "request_title", "request_reason")
    @classmethod
    def strip_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Research handoff text cannot contain outer whitespace.")
        return value


class ResearchShareParameters(ContractModel):
    locale: str = Field(pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    recipient_ids: list[str] = Field(default_factory=list, max_length=50)
    team_id: str | None = Field(default=None, min_length=2, max_length=160)
    permission: ResearchSharePermission
    expires_at: datetime

    @field_validator("recipient_ids")
    @classmethod
    def valid_recipients(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() or len(value) > 160 for value in values):
            raise ValueError("Research share recipients are invalid.")
        if len(set(values)) != len(values):
            raise ValueError("Research share recipients must be unique.")
        return values

    @field_validator("team_id")
    @classmethod
    def valid_team(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("Research share team cannot contain outer whitespace.")
        return value

    @model_validator(mode="after")
    def valid_scope_and_expiry(self) -> "ResearchShareParameters":
        if not self.recipient_ids and self.team_id is None:
            raise ValueError("Research sharing requires at least one recipient or team.")
        if self.expires_at.tzinfo is None:
            raise ValueError("Research share expiry must include a timezone.")
        now = datetime.now(UTC)
        expiry = self.expires_at.astimezone(UTC)
        if expiry <= now + timedelta(minutes=5) or expiry > now + timedelta(days=90):
            raise ValueError("Research share expiry must be between 5 minutes and 90 days.")
        return self


class ResearchHandoffReceiptEffect(ContractModel):
    effect_type: Literal["HANDOFF"]
    approval_target: str = Field(min_length=2, max_length=160)
    request_title: str = Field(min_length=2, max_length=240)
    request_reason: str = Field(min_length=10, max_length=1_000)
    request_metadata: dict[str, JsonValue] = Field(default_factory=dict, max_length=20)


class ResearchShareReceiptEffect(ContractModel):
    effect_type: Literal["SHARE"]
    recipient_ids: list[str] = Field(default_factory=list, max_length=50)
    team_id: str | None = Field(default=None, min_length=2, max_length=160)
    permission: ResearchSharePermission
    expires_at: datetime


class ResearchDownstreamProviderReceipt(ContractModel):
    delivery_id: UUID
    run_id: UUID
    delivery_type: ResearchDeliveryType
    receipt_id: UUID
    provider_receipt_id: str = Field(min_length=1, max_length=240)
    target_id: UUID
    target_path: str = Field(min_length=2, max_length=500)
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameters_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effect: ResearchHandoffReceiptEffect | ResearchShareReceiptEffect
    accepted_at: datetime

    @field_validator("target_path")
    @classmethod
    def safe_target_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or "\n" in value:
            raise ValueError("The provider target path must be an application-relative path.")
        return value

    @field_validator("provider_receipt_id")
    @classmethod
    def non_blank_provider_receipt(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("The provider receipt ID cannot contain outer whitespace.")
        return value

    @field_validator("accepted_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("The provider acceptance time must include a timezone.")
        return value

    @model_validator(mode="after")
    def effect_matches_delivery_type(self) -> "ResearchDownstreamProviderReceipt":
        if self.effect.effect_type != self.delivery_type.value:
            raise ValueError("The downstream effect type does not match the delivery type.")
        return self


@dataclass(frozen=True)
class ResearchDownstreamContext:
    identity: PersonalDomainIdentity
    run: ResearchRun
    delivery_id: UUID
    delivery_type: ResearchDeliveryType
    parameters: dict[str, object]


class ResearchDownstreamProviderError(RuntimeError):
    def __init__(self, code: str, recovery_hint: str) -> None:
        super().__init__(code)
        self.code = code
        self.recovery_hint = recovery_hint


class HttpResearchDownstreamProvider:
    def __init__(
        self,
        delivery_type: ResearchDeliveryType,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if delivery_type not in {ResearchDeliveryType.HANDOFF, ResearchDeliveryType.SHARE}:
            raise ValueError("The HTTP research delivery type is unsupported.")
        self.delivery_type = delivery_type
        prefix = f"DWP_RESEARCH_{delivery_type.value}_PROVIDER"
        self.url = os.getenv(f"{prefix}_URL", "").strip()
        self.token = os.getenv(f"{prefix}_TOKEN", "").strip()
        self.allowed_hosts = frozenset(
            item.strip().lower()
            for item in os.getenv("DWP_RESEARCH_DOWNSTREAM_ALLOWED_HOSTS", "").split(",")
            if item.strip()
        )
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(
            self.url
            and len(self.token) >= 24
            and _allowlisted(self.url, self.allowed_hosts)
        )

    def deliver(
        self, context: ResearchDownstreamContext
    ) -> ResearchDownstreamProviderReceipt:
        if not self.configured or context.delivery_type != self.delivery_type:
            raise ResearchDownstreamProviderError(
                f"RESEARCH_{self.delivery_type.value}_PROVIDER_NOT_CONFIGURED",
                "Configure the allowlisted research delivery endpoint and delegated token.",
            )
        if context.run.result is None:
            raise ResearchDownstreamProviderError(
                "RESEARCH_RESULT_UNAVAILABLE",
                "Restore the completed research result before retrying delivery.",
            )
        payload = self._payload(context)
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20.0, connect=5.0),
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "POST",
                    self.url,
                    json=payload,
                    headers=self._headers(context),
                ) as response:
                    body = _bounded_body(response)
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise ResearchDownstreamProviderError(
                f"RESEARCH_{self.delivery_type.value}_PROVIDER_UNREACHABLE",
                "Restore the downstream provider and retry with the same delivery ID.",
            ) from error
        if not 200 <= response.status_code < 300:
            suffix = (
                "UNAVAILABLE"
                if response.status_code in {408, 425, 429}
                or response.status_code >= 500
                else "REJECTED"
            )
            raise ResearchDownstreamProviderError(
                f"RESEARCH_{self.delivery_type.value}_PROVIDER_{suffix}",
                "Review provider availability and authorization, then retry with the same delivery ID.",
            )
        try:
            value = json.loads(body)
            if isinstance(value, dict) and isinstance(value.get("data"), dict):
                value = value["data"]
            receipt = ResearchDownstreamProviderReceipt.model_validate(value)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise ResearchDownstreamProviderError(
                f"RESEARCH_{self.delivery_type.value}_PROVIDER_RECEIPT_INVALID",
                "Repair the downstream receipt contract before retrying.",
            ) from error
        self._verify_receipt(context, receipt)
        return receipt

    def _payload(self, context: ResearchDownstreamContext) -> dict[str, object]:
        assert context.run.result is not None
        return {
            "deliveryId": str(context.delivery_id),
            "runId": str(context.run.run_id),
            "deliveryType": context.delivery_type.value,
            "parameters": context.parameters,
            "result": {
                "reportMarkdown": context.run.result.report_markdown,
                "resultSha256": context.run.result.result_sha256,
                "citations": [
                    citation.model_dump(mode="json", by_alias=True)
                    for citation in context.run.result.citations
                ],
            },
        }

    def _headers(self, context: ResearchDownstreamContext) -> dict[str, str]:
        assert context.run.result is not None
        return {
            "Authorization": f"Bearer {self.token}",
            "X-DWP-Tenant-ID": str(context.identity.tenant_id),
            "X-DWP-User-ID": context.identity.user_id,
            "X-DWP-Delivery-ID": str(context.delivery_id),
            "X-DWP-Idempotency-Key": str(context.delivery_id),
            "X-DWP-Result-SHA256": context.run.result.result_sha256,
            "X-Correlation-ID": context.identity.correlation_id,
        }

    def _verify_receipt(
        self,
        context: ResearchDownstreamContext,
        receipt: ResearchDownstreamProviderReceipt,
    ) -> None:
        assert context.run.result is not None
        parameters = normalize_research_downstream_parameters(
            context.delivery_type, context.parameters
        )
        bound = (
            receipt.delivery_id == context.delivery_id
            and receipt.run_id == context.run.run_id
            and receipt.delivery_type == context.delivery_type
            and receipt.result_sha256 == context.run.result.result_sha256
            and receipt.parameters_sha256 == _parameters_sha256(parameters)
            and _effect_binds(context.delivery_type, parameters, receipt.effect)
            and _target_path_binds(receipt.target_path, receipt.target_id)
            and receipt.accepted_at.astimezone(UTC) <= datetime.now(UTC) + timedelta(minutes=5)
        )
        if not bound:
            raise ResearchDownstreamProviderError(
                f"RESEARCH_{self.delivery_type.value}_PROVIDER_BINDING_INVALID",
                "Reject the receipt and repair delivery, target, and result binding.",
            )


def validate_research_downstream_observation_receipt(
    *, delivery_id: UUID, run_id: UUID, delivery_type: ResearchDeliveryType,
    result_sha256: str, receipt_id: UUID, receipt: dict[str, object],
    created_at: datetime, parameters: dict[str, object],
) -> ResearchDownstreamProviderReceipt:
    expected_keys = {
        "schemaVersion", "targetType", "deliveryId", "runId", "deliveryType",
        "receiptId", "providerReceiptId", "targetId", "targetPath",
        "resultSha256", "parametersSha256", "effect", "acceptedAt",
    }
    if set(receipt) != expected_keys:
        raise ValueError("The downstream observation receipt schema is invalid.")
    if receipt.get("schemaVersion") != 2 or receipt.get("targetType") != delivery_type.value:
        raise ValueError("The downstream observation receipt type is invalid.")
    typed = ResearchDownstreamProviderReceipt.model_validate({
        key: value for key, value in receipt.items()
        if key not in {"schemaVersion", "targetType"}
    })
    accepted = typed.accepted_at.astimezone(UTC)
    created = created_at.astimezone(UTC)
    normalized = normalize_research_downstream_parameters(delivery_type, parameters)
    if not (
        typed.receipt_id == receipt_id
        and typed.delivery_id == delivery_id
        and typed.run_id == run_id
        and typed.delivery_type == delivery_type
        and typed.result_sha256 == result_sha256
        and typed.parameters_sha256 == _parameters_sha256(normalized)
        and _effect_binds(delivery_type, normalized, typed.effect)
        and _target_path_binds(typed.target_path, typed.target_id)
        and created - timedelta(minutes=5) <= accepted <= datetime.now(UTC) + timedelta(minutes=5)
    ):
        raise ValueError("The downstream observation receipt binding is invalid.")
    return typed


def normalize_research_downstream_parameters(
    delivery_type: ResearchDeliveryType, parameters: dict[str, object]
) -> dict[str, object]:
    model: ContractModel
    if delivery_type == ResearchDeliveryType.HANDOFF:
        model = ResearchHandoffParameters.model_validate(parameters)
    elif delivery_type == ResearchDeliveryType.SHARE:
        model = ResearchShareParameters.model_validate(parameters)
    else:
        return dict(parameters)
    normalized = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    if len(json.dumps(normalized, separators=(",", ":"), ensure_ascii=False).encode()) > 16_384:
        raise ValueError("Research downstream parameters exceed the governed limit.")
    return normalized


def _parameters_sha256(parameters: dict[str, object]) -> str:
    import hashlib

    return hashlib.sha256(canonical_json_bytes(parameters)).hexdigest()


def _effect_binds(
    delivery_type: ResearchDeliveryType,
    parameters: dict[str, object],
    effect: ResearchHandoffReceiptEffect | ResearchShareReceiptEffect,
) -> bool:
    if delivery_type == ResearchDeliveryType.HANDOFF:
        reviewed = ResearchHandoffParameters.model_validate(parameters)
        return isinstance(effect, ResearchHandoffReceiptEffect) and (
            effect.approval_target == reviewed.approval_target
            and effect.request_title == reviewed.request_title
            and effect.request_reason == reviewed.request_reason
            and effect.request_metadata == reviewed.request_metadata
        )
    reviewed_share = ResearchShareParameters.model_validate(parameters)
    return isinstance(effect, ResearchShareReceiptEffect) and (
        effect.recipient_ids == reviewed_share.recipient_ids
        and effect.team_id == reviewed_share.team_id
        and effect.permission == reviewed_share.permission
        and effect.expires_at == reviewed_share.expires_at
    )


def research_downstream_capability(
    delivery_type: ResearchDeliveryType,
) -> WorkflowCapability:
    configured = HttpResearchDownstreamProvider(delivery_type).configured
    return WorkflowCapability(
        available=configured,
        configured=configured,
        reason_code=(
            None
            if configured
            else f"RESEARCH_{delivery_type.value}_PROVIDER_NOT_CONFIGURED"
        ),
        recovery_hint=(
            None
            if configured
            else "Configure the allowlisted research delivery endpoint and delegated token."
        ),
    )


def _bounded_body(response: httpx.Response) -> bytes:
    maximum = 65_536
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > maximum:
                raise ValueError("The provider response is too large.")
        except ValueError as error:
            raise ResearchDownstreamProviderError(
                "RESEARCH_DOWNSTREAM_PROVIDER_RESPONSE_INVALID",
                "Return a bounded JSON delivery receipt.",
            ) from error
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > maximum:
            raise ResearchDownstreamProviderError(
                "RESEARCH_DOWNSTREAM_PROVIDER_RESPONSE_INVALID",
                "Return a bounded JSON delivery receipt.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


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


def _target_path_binds(target_path: str, target_id: UUID) -> bool:
    parsed = urlparse(target_path)
    expected = str(target_id)
    segments = [segment for segment in parsed.path.split("/") if segment]
    query_values = [value for values in parse_qs(parsed.query).values() for value in values]
    return expected in segments or expected in query_values
