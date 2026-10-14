from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from .model_provider import ProviderConfigurationError, ResponsesProviderConfiguration
from .run_observability import RunSourceHealthStatus, SourceHealthObservation


_MODEL_CONFIGURATION_REQUIRED = "MODEL_ROUTE_CONFIGURATION_REQUIRED"
_MODEL_CONFIGURATION_INVALID = "MODEL_ROUTE_CONFIGURATION_INVALID"


@dataclass(frozen=True)
class _ModelObservation:
    status: str
    provider: str
    observed_at: datetime
    safe_error_code: str | None = None


class OperationalHealthRegistry:
    """Process-local, privacy-safe observations from recent Ask executions."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._models: dict[str, _ModelObservation] = {}
        self._sources: dict[tuple[str, str], SourceHealthObservation] = {}
        self._lock = threading.Lock()

    def record_model_available(self, *, tenant_id: str, provider: str) -> None:
        self._record_model(
            tenant_id=tenant_id,
            status="AVAILABLE",
            provider=provider,
        )

    def record_model_unavailable(
        self, *, tenant_id: str, provider: str, safe_error_code: str
    ) -> None:
        self._record_model(
            tenant_id=tenant_id,
            status="UNAVAILABLE",
            provider=provider,
            safe_error_code=safe_error_code,
        )

    def record_model_not_configured(self, *, tenant_id: str, provider: str) -> None:
        self._record_model(
            tenant_id=tenant_id,
            status="NOT_CONFIGURED",
            provider=provider,
            safe_error_code=_MODEL_CONFIGURATION_REQUIRED,
        )

    def record_model_invalid_configuration(
        self,
        *,
        tenant_id: str,
        provider: str,
    ) -> None:
        self._record_model(
            tenant_id=tenant_id,
            status="INVALID_CONFIGURATION",
            provider=provider,
            safe_error_code=_MODEL_CONFIGURATION_INVALID,
        )

    def _record_model(
        self,
        *,
        tenant_id: str,
        status: str,
        provider: str,
        safe_error_code: str | None = None,
    ) -> None:
        observation = _ModelObservation(
            status=status,
            provider=_safe_label(provider, fallback="UNKNOWN", maximum=40),
            observed_at=_as_utc(self._clock()),
            safe_error_code=safe_error_code,
        )
        tenant_key = _tenant_key(tenant_id)
        with self._lock:
            self._prune_locked(
                observation.observed_at
                - timedelta(seconds=_observation_window_seconds())
            )
            current = self._models.get(tenant_key)
            if current is None or observation.observed_at >= current.observed_at:
                self._models[tenant_key] = observation

    def record_sources(
        self,
        *,
        tenant_id: str,
        observations: Iterable[SourceHealthObservation],
    ) -> None:
        tenant_key = _tenant_key(tenant_id)
        normalized = tuple(observations)
        with self._lock:
            if normalized:
                newest = max(_as_utc(item.observed_at) for item in normalized)
                self._prune_locked(
                    newest - timedelta(seconds=_observation_window_seconds())
                )
            for observation in normalized:
                if not observation.operational:
                    continue
                source_type = _safe_label(
                    observation.source_type,
                    fallback="UNKNOWN",
                    maximum=40,
                )
                observed_at = _as_utc(observation.observed_at)
                source_key = (tenant_key, source_type)
                current = self._sources.get(source_key)
                if current is None or observed_at >= current.observed_at:
                    self._sources[source_key] = SourceHealthObservation(
                        source_type=source_type,
                        status=RunSourceHealthStatus(observation.status),
                        observed_at=observed_at,
                        latency_ms=observation.latency_ms,
                    )

    def snapshot(self, *, tenant_id: str) -> dict[str, object]:
        now = _as_utc(self._clock())
        window_seconds = _observation_window_seconds()
        cutoff = now - timedelta(seconds=window_seconds)
        tenant_key = _tenant_key(tenant_id)
        with self._lock:
            self._prune_locked(cutoff)
            model_observation = self._models.get(tenant_key)
            source_observations = tuple(
                observation
                for (candidate_tenant, _), observation in self._sources.items()
                if candidate_tenant == tenant_key
            )

        configuration = _model_configuration_status()
        if model_observation is not None and model_observation.observed_at >= cutoff:
            model = {
                "status": model_observation.status,
                "provider": model_observation.provider,
                "configured": model_observation.status in {
                    "AVAILABLE",
                    "CONFIGURED",
                    "UNAVAILABLE",
                },
                "lastObservedAt": _isoformat(model_observation.observed_at),
            }
            if model_observation.safe_error_code is not None:
                model["safeErrorCode"] = model_observation.safe_error_code
        else:
            model = configuration

        recent_sources = sorted(
            (
                observation
                for observation in source_observations
                if observation.observed_at >= cutoff
            ),
            key=lambda observation: observation.source_type,
        )
        source_items = {
            observation.source_type: {
                "status": str(observation.status),
                "lastObservedAt": _isoformat(observation.observed_at),
            }
            for observation in recent_sources
        }
        if not recent_sources:
            source_status = "UNKNOWN"
        elif any(
            observation.status != RunSourceHealthStatus.SUCCESS
            for observation in recent_sources
        ):
            source_status = "DEGRADED"
        else:
            source_status = "HEALTHY"

        reasons: list[str] = []
        if model["status"] in {
            "INVALID_CONFIGURATION",
            "NOT_CONFIGURED",
            "UNAVAILABLE",
        }:
            reasons.append(str(model.get("safeErrorCode") or "MODEL_PROVIDER_UNAVAILABLE"))
        reasons.extend(
            f"SOURCE_{observation.source_type}_{observation.status}"
            for observation in recent_sources
            if observation.status != RunSourceHealthStatus.SUCCESS
        )
        return {
            "degraded": bool(reasons),
            "degradationReasons": reasons,
            "modelProvider": model,
            "sources": {
                "status": source_status,
                "windowSeconds": window_seconds,
                "recent": source_items,
            },
        }

    def reset(self) -> None:
        with self._lock:
            self._models.clear()
            self._sources.clear()

    def _prune_locked(self, cutoff: datetime) -> None:
        self._models = {
            tenant: observation
            for tenant, observation in self._models.items()
            if observation.observed_at >= cutoff
        }
        self._sources = {
            key: observation
            for key, observation in self._sources.items()
            if observation.observed_at >= cutoff
        }


def _model_configuration_status() -> dict[str, object]:
    try:
        configuration = ResponsesProviderConfiguration.from_environment()
    except ProviderConfigurationError:
        return {
            "status": "INVALID_CONFIGURATION",
            "provider": "UNKNOWN",
            "configured": False,
            "lastObservedAt": None,
            "safeErrorCode": _MODEL_CONFIGURATION_INVALID,
        }
    base = {
        "provider": configuration.audit_label,
        "configured": configuration.configured,
        "lastObservedAt": None,
    }
    if not configuration.configured:
        return {
            **base,
            "status": "NOT_CONFIGURED",
            "safeErrorCode": _MODEL_CONFIGURATION_REQUIRED,
        }
    try:
        configuration.validate_endpoint()
    except ProviderConfigurationError:
        return {
            **base,
            "status": "INVALID_CONFIGURATION",
            "configured": False,
            "safeErrorCode": _MODEL_CONFIGURATION_INVALID,
        }
    return {**base, "status": "CONFIGURED"}


def _observation_window_seconds() -> int:
    try:
        value = int(os.getenv("DWP_OPERATIONAL_HEALTH_WINDOW_SECONDS", "300"))
    except ValueError:
        value = 300
    return max(30, min(3_600, value))


def _safe_label(value: object, *, fallback: str, maximum: int) -> str:
    normalized = str(value).strip().upper()
    safe = "".join(
        character
        for character in normalized
        if character.isalnum() or character in "_.-"
    )
    return safe[:maximum] or fallback


def _tenant_key(value: object) -> str:
    normalized = str(value).strip()
    return normalized or "__UNKNOWN_TENANT__"


def _isoformat(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


OPERATIONAL_HEALTH = OperationalHealthRegistry()


def operational_health_snapshot(*, tenant_id: str) -> dict[str, object]:
    return OPERATIONAL_HEALTH.snapshot(tenant_id=tenant_id)


def reset_operational_health_for_tests() -> None:
    OPERATIONAL_HEALTH.reset()
