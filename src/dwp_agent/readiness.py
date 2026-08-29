from __future__ import annotations

import os
from urllib.parse import urlparse

from .crypto import DataKeyConfigurationError
from .envelope import (
    EnvelopeEncryptionError,
    KeyContext,
    load_payload_encryption,
)
from .key_provider import (
    KeyProvider,
    KeyProviderConfigurationError,
    normalized_environment,
)
from .model_provider import (
    ModelProvider,
    ProviderConfigurationError,
    ResponsesProviderConfiguration,
)


class RuntimeConfigurationError(RuntimeError):
    pass


def validate_runtime_configuration(key_provider: KeyProvider | None = None) -> None:
    environment = normalized_environment()
    if environment == "local":
        return

    errors: list[str] = []
    if not _managed_secret("DWP_AGENT_IDENTITY_SIGNING_SECRET"):
        errors.append("DWP_AGENT_IDENTITY_SIGNING_SECRET")
    try:
        encryption = load_payload_encryption(key_provider)
        probe_context = KeyContext(
            environment=environment,
            service="dwp-agent",
            purpose="payload",
            tenant_id=0,
            resource_type="runtime-probe",
            resource_id="startup",
            field="payload",
        )
        probe_envelope = encryption.encrypt_bytes(b"dwp-agent-readiness", probe_context)
        if encryption.decrypt_bytes(
            envelope=probe_envelope,
            context=probe_context,
            legacy_version=None,
            legacy_nonce=None,
            legacy_ciphertext=None,
            legacy_aad=b"",
        ) != b"dwp-agent-readiness":
            raise EnvelopeEncryptionError("Agent envelope key provider probe failed.")
    except (
        DataKeyConfigurationError,
        EnvelopeEncryptionError,
        KeyProviderConfigurationError,
        ValueError,
    ):
        errors.append("DWP_AGENT_KEY_PROVIDER")
    if environment != "prod":
        _raise_if_errors(errors, environment)
        return

    required_secrets = (
        "DWP_AGENT_SERVICE_TOKEN",
        "DWP_AGENT_IDENTITY_SIGNING_SECRET",
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
        "SERVICE_GATEWAY_URL",
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
    base_url = os.getenv("DWP_OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    if urlparse(base_url).scheme.lower() != "https":
        errors.append("DWP_OPENAI_BASE_URL=https")

    _require_distinct(
        errors,
        "service identity tokens",
        "DWP_AGENT_SERVICE_TOKEN",
        "DWP_AGENT_IDENTITY_SIGNING_SECRET",
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

    _raise_if_errors(errors, environment)


def _managed_secret(name: str) -> bool:
    value = os.getenv(name, "").strip()
    lowered = value.lower()
    return len(value) >= 24 and "replace-with" not in lowered and "change-me" not in lowered


def _require_distinct(errors: list[str], label: str, *names: str) -> None:
    values = [os.getenv(name, "").strip() for name in names]
    configured = [value for value in values if value]
    if len(configured) != len(set(configured)):
        errors.append(f"distinct {label}")


def _raise_if_errors(errors: list[str], environment: str) -> None:
    if not errors:
        return
    unique = ", ".join(dict.fromkeys(errors))
    raise RuntimeConfigurationError(
        f"{environment} Agent configuration is incomplete or unsafe: {unique}."
    )
