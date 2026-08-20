from __future__ import annotations

import os
from urllib.parse import urlparse

from .crypto import DataKeyConfigurationError, load_payload_keyring
from .model_provider import (
    ModelProvider,
    ProviderConfigurationError,
    ResponsesProviderConfiguration,
)


class RuntimeConfigurationError(RuntimeError):
    pass


def validate_runtime_configuration() -> None:
    if os.getenv("DWP_ENVIRONMENT", "local").strip().lower() != "production":
        return

    errors: list[str] = []
    required_secrets = (
        "DWP_AGENT_SERVICE_TOKEN",
        "DWP_PLATFORM_RUNTIME_SERVICE_TOKEN",
        "DWP_APPROVAL_RUNTIME_SERVICE_TOKEN",
        "DWP_AGENT_PRIVACY_HASH_SECRET",
        "DWP_AGENT_SAFETY_SECRET",
        "DWP_AUDIT_INGEST_TOKEN",
        "DWP_API_HISTORY_INGEST_TOKEN",
        "DWP_API_HISTORY_PRIVACY_HASH_SECRET",
    )
    for name in required_secrets:
        if not _managed_secret(name):
            errors.append(name)

    required_values = (
        "DWP_AGENT_DATABASE_URL",
        "DWP_AGENT_DATA_KEY_VERSION",
        "SERVICE_PLATFORM_URL",
        "SERVICE_APPROVAL_URL",
        "DWP_AUDIT_COLLECTOR_URL",
        "DWP_API_HISTORY_COLLECTOR_URL",
        "DWP_OPENAI_MODEL",
    )
    for name in required_values:
        if not os.getenv(name, "").strip():
            errors.append(name)

    provider: ModelProvider | None
    try:
        provider = ModelProvider.parse(os.getenv("DWP_MODEL_PROVIDER"))
    except ProviderConfigurationError:
        provider = None
        errors.append("DWP_MODEL_PROVIDER")
    if provider is not None:
        provider_configuration = ResponsesProviderConfiguration.from_environment(
            provider=provider
        )
        key_environment = provider_configuration.required_api_key_environment
        if not _managed_secret(key_environment):
            errors.append(key_environment)
        try:
            provider_configuration.validate_endpoint()
        except ProviderConfigurationError:
            errors.append("DWP_OPENAI_BASE_URL=approved-provider-endpoint")

    if os.getenv("DWP_AGENT_DATABASE_REQUIRED", "").strip().lower() != "true":
        errors.append("DWP_AGENT_DATABASE_REQUIRED=true")
    if os.getenv("DWP_AGENT_REGISTRY_MODE", "").strip().lower() != "enforced":
        errors.append("DWP_AGENT_REGISTRY_MODE=enforced")
    if os.getenv("DWP_AGENT_DATA_KEY_VERSION", "").strip() in {"", "legacy-v1"}:
        errors.append("DWP_AGENT_DATA_KEY_VERSION")

    try:
        load_payload_keyring()
    except DataKeyConfigurationError:
        errors.append("DWP_AGENT_DATA_KEY")

    base_url = os.getenv("DWP_OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    if urlparse(base_url).scheme.lower() != "https":
        errors.append("DWP_OPENAI_BASE_URL=https")

    _require_distinct(
        errors,
        "service identity tokens",
        "DWP_AGENT_SERVICE_TOKEN",
        "DWP_PLATFORM_RUNTIME_SERVICE_TOKEN",
        "DWP_APPROVAL_RUNTIME_SERVICE_TOKEN",
    )
    _require_distinct(
        errors,
        "privacy and safety secrets",
        "DWP_AGENT_PRIVACY_HASH_SECRET",
        "DWP_AGENT_SAFETY_SECRET",
        "DWP_API_HISTORY_PRIVACY_HASH_SECRET",
    )

    if errors:
        unique = ", ".join(dict.fromkeys(errors))
        raise RuntimeConfigurationError(
            f"Production Agent configuration is incomplete or unsafe: {unique}."
        )


def _managed_secret(name: str) -> bool:
    value = os.getenv(name, "").strip()
    lowered = value.lower()
    return len(value) >= 24 and "replace-with" not in lowered and "change-me" not in lowered


def _require_distinct(errors: list[str], label: str, *names: str) -> None:
    values = [os.getenv(name, "").strip() for name in names]
    configured = [value for value in values if value]
    if len(configured) != len(set(configured)):
        errors.append(f"distinct {label}")
