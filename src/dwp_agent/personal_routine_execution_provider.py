from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin, urlparse
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .personal_routine_contracts import (
    RoutineDefinition,
    RoutineNotificationState,
)


class RoutineExecutionProviderUnavailable(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RoutineProviderResult(_ProviderModel):
    routineRunId: UUID
    state: Literal["COMPLETED", "PARTIAL"]
    providerReceiptId: str = Field(min_length=1, max_length=240)
    resultSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidenceCount: int = Field(ge=0, le=1_000_000)
    proposalsCreated: int = Field(ge=0, le=100_000)
    approvalGatedActionsCreated: int = Field(ge=0, le=100_000)
    externalWritesPerformed: int = Field(ge=0, le=100_000)
    tokensUsed: int = Field(ge=0, le=2_000_000)
    elapsedMs: int = Field(ge=0, le=14_400_000)
    notificationState: RoutineNotificationState
    compensationRequired: bool = False
    authorizationDecisionRevision: int = Field(ge=1)
    authorizedSources: list[str] = Field(min_length=1, max_length=3)
    safeErrorCode: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_.-]{1,127}$"
    )
    recoveryHint: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def truthful_result(self) -> "RoutineProviderResult":
        if self.externalWritesPerformed != 0:
            raise ValueError(
                "Routine execution may create approval-gated actions but cannot perform external writes."
            )
        if self.state == "COMPLETED" and (
            self.safeErrorCode is not None or self.recoveryHint is not None
        ):
            raise ValueError("Completed routine execution cannot carry an error.")
        if self.state == "PARTIAL" and (
            self.safeErrorCode is None or self.recoveryHint is None
        ):
            raise ValueError("Partial routine execution requires a recoverable error.")
        return self


class RoutineProviderCompensationResult(_ProviderModel):
    routineRunId: UUID
    providerReceiptId: str = Field(min_length=1, max_length=240)
    resultSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revokedPendingHandoffs: int = Field(ge=0, le=100_000)
    externalWritesReversed: int = Field(default=0, ge=0, le=100_000)

    @model_validator(mode="after")
    def scoped_compensation(self) -> "RoutineProviderCompensationResult":
        if self.externalWritesReversed != 0:
            raise ValueError(
                "The routine provider cannot claim reversal of external writes."
            )
        return self


@dataclass(frozen=True)
class RoutineExecutionProviderConfiguration:
    enabled: bool
    base_url: str
    service_token: str = field(repr=False)
    allowed_hosts: frozenset[str]
    timeout_seconds: float

    @classmethod
    def from_environment(cls) -> "RoutineExecutionProviderConfiguration":
        return cls(
            enabled=_flag("DWP_ROUTINE_EXECUTION_ENABLED"),
            base_url=os.getenv("DWP_ROUTINE_EXECUTION_BROKER_BASE_URL", "").strip(),
            service_token=os.getenv(
                "DWP_ROUTINE_EXECUTION_BROKER_SERVICE_TOKEN", ""
            ).strip(),
            allowed_hosts=_csv("DWP_ROUTINE_EXECUTION_BROKER_ALLOWED_HOSTS"),
            timeout_seconds=_bounded_timeout(
                os.getenv("DWP_ROUTINE_EXECUTION_TIMEOUT_SECONDS", "20")
            ),
        )

    def validate(self) -> None:
        if not self.enabled:
            raise RoutineExecutionProviderUnavailable(
                "ROUTINE_EXECUTION_NOT_CONFIGURED"
            )
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.hostname.lower() not in self.allowed_hosts
            or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"})
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not self.service_token
            or not 3 <= self.timeout_seconds <= 30
        ):
            raise RoutineExecutionProviderUnavailable(
                "ROUTINE_EXECUTION_NOT_CONFIGURED"
            )

    @property
    def configured(self) -> bool:
        try:
            self.validate()
        except RoutineExecutionProviderUnavailable:
            return False
        return True


class RoutineExecutionProvider:
    def __init__(
        self,
        configuration: RoutineExecutionProviderConfiguration | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.configuration = (
            configuration or RoutineExecutionProviderConfiguration.from_environment()
        )
        self.transport = transport

    @property
    def configured(self) -> bool:
        return self.configuration.configured

    def execute(
        self,
        *,
        routine_run_id: UUID,
        routine_id: UUID,
        routine_revision: int,
        tenant_id: int,
        user_id: str,
        correlation_id: str,
        definition: RoutineDefinition,
    ) -> RoutineProviderResult:
        response = self._request(
            "/internal/v1/routine-executions",
            {
                "routineRunId": str(routine_run_id),
                "routineId": str(routine_id),
                "routineRevision": routine_revision,
                "tenantId": tenant_id,
                "userId": user_id,
                "definition": definition.model_dump(mode="json", by_alias=True),
                "externalWritesAllowed": False,
                "approvalGatedActionsOnly": True,
                "requireCurrentAuthorization": True,
            },
            correlation_id,
            RoutineProviderResult,
        )
        if response.routineRunId != routine_run_id:
            raise RoutineExecutionProviderUnavailable(
                "ROUTINE_EXECUTION_RESPONSE_MISMATCH"
            )
        expected_sources = {source.value for source in definition.sources}
        if set(response.authorizedSources) != expected_sources or len(
            response.authorizedSources
        ) != len(expected_sources):
            raise RoutineExecutionProviderUnavailable(
                "ROUTINE_SOURCE_AUTHORIZATION_MISMATCH"
            )
        return response

    def compensate(
        self,
        *,
        routine_run_id: UUID,
        provider_receipt_id: str,
        tenant_id: int,
        user_id: str,
        correlation_id: str,
    ) -> RoutineProviderCompensationResult:
        response = self._request(
            f"/internal/v1/routine-executions/{routine_run_id}/compensate",
            {
                "routineRunId": str(routine_run_id),
                "providerReceiptId": provider_receipt_id,
                "tenantId": tenant_id,
                "userId": user_id,
                "externalWritesAllowed": False,
            },
            correlation_id,
            RoutineProviderCompensationResult,
        )
        if response.routineRunId != routine_run_id:
            raise RoutineExecutionProviderUnavailable(
                "ROUTINE_COMPENSATION_RESPONSE_MISMATCH"
            )
        return response

    def _request(self, path: str, body: dict[str, object], correlation_id: str, model):
        self.configuration.validate()
        try:
            with httpx.Client(
                timeout=self.configuration.timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    urljoin(
                        self.configuration.base_url.rstrip("/") + "/",
                        path.lstrip("/"),
                    ),
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self.configuration.service_token}",
                        "X-Correlation-ID": correlation_id,
                        "Accept": "application/json",
                    },
                )
            if response.status_code != 200 or len(response.content) > 1_000_000:
                raise RoutineExecutionProviderUnavailable(
                    "ROUTINE_EXECUTION_PROVIDER_UNAVAILABLE"
                )
            return model.model_validate(response.json())
        except RoutineExecutionProviderUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise RoutineExecutionProviderUnavailable(
                "ROUTINE_EXECUTION_PROVIDER_UNAVAILABLE"
            ) from error


def _flag(name: str) -> bool:
    value = os.getenv(name, "false").strip().lower()
    if value not in {"true", "false"}:
        return False
    return value == "true"


def _csv(name: str) -> frozenset[str]:
    return frozenset(
        value.strip().lower()
        for value in os.getenv(name, "").split(",")
        if value.strip()
    )


def _bounded_timeout(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0
