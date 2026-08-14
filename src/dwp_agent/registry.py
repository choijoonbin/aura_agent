from __future__ import annotations

import os

import httpx

from .contracts import (
    AgentRegistryResolution,
    RegistryResolutionStatus,
    RegistryRiskTier,
)


class RegistryResolutionError(RuntimeError):
    pass


def _reference_fallback(agent_key: str) -> AgentRegistryResolution:
    return AgentRegistryResolution(
        entry_key=agent_key,
        revision=0,
        artifact_version="reference",
        risk_tier=RegistryRiskTier.MEDIUM,
        resolution=RegistryResolutionStatus.REFERENCE_FALLBACK,
    )


def _enforced() -> bool:
    return os.getenv("DWP_AGENT_REGISTRY_MODE", "optional").strip().lower() == "enforced"


def resolve_agent(
    agent_key: str,
    *,
    tenant_id: str,
    user_id: str,
    correlation_id: str,
) -> AgentRegistryResolution:
    normalized_key = agent_key.strip().upper()
    platform_url = os.getenv("SERVICE_PLATFORM_URL", "").strip().rstrip("/")
    service_token = os.getenv("DWP_PLATFORM_RUNTIME_SERVICE_TOKEN", "").strip()
    if not platform_url or not service_token:
        if _enforced():
            raise RegistryResolutionError("Agent registry service identity is not configured.")
        return _reference_fallback(normalized_key)

    try:
        response = httpx.get(
            f"{platform_url}/v1/catalog/registry-entries/AGENT/{normalized_key}",
            headers={
                "X-DWP-Service-Token": service_token,
                "X-DWP-User-ID": user_id,
                "X-DWP-Tenant-ID": tenant_id,
                "X-Correlation-ID": correlation_id,
            },
            timeout=2.0,
        )
    except httpx.HTTPError as error:
        if _enforced():
            raise RegistryResolutionError("Agent registry is unavailable.") from error
        return _reference_fallback(normalized_key)

    if response.status_code != 200:
        if _enforced():
            raise RegistryResolutionError("An active Agent registry entry is required.")
        return _reference_fallback(normalized_key)

    try:
        data = response.json()["data"]
        if data["registryType"] != "AGENT" or data["entryKey"] != normalized_key:
            raise ValueError("Registry identity mismatch.")
        return AgentRegistryResolution(
            entry_key=data["entryKey"],
            revision=data["revision"],
            artifact_version=data["artifactVersion"],
            risk_tier=RegistryRiskTier(data["riskTier"]),
            resolution=RegistryResolutionStatus.ACTIVE,
        )
    except (KeyError, TypeError, ValueError) as error:
        if _enforced():
            raise RegistryResolutionError("Agent registry response is invalid.") from error
        return _reference_fallback(normalized_key)
