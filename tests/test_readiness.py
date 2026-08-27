import base64

import pytest

from dwp_agent.key_provider import VersionedKeyMaterial
from dwp_agent.readiness import RuntimeConfigurationError, validate_runtime_configuration


def test_local_runtime_does_not_require_production_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_ENVIRONMENT", "local")

    validate_runtime_configuration()


def test_production_runtime_fails_closed_with_safe_setting_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _PRODUCTION_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DWP_ENVIRONMENT", "production")

    with pytest.raises(RuntimeConfigurationError) as captured:
        validate_runtime_configuration()

    message = str(captured.value)
    assert "DWP_AGENT_DATABASE_URL" in message
    assert "DWP_AGENT_REGISTRY_MODE=enforced" in message
    assert "OPENAI_API_KEY" in message
    assert "postgresql://" not in message


def test_production_runtime_accepts_complete_distinct_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_production(monkeypatch)

    validate_runtime_configuration(ManagedTestKeyProvider())


def test_production_runtime_rejects_shared_identity_and_insecure_model_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_production(monkeypatch)
    monkeypatch.setenv(
        "DWP_PLATFORM_RUNTIME_SERVICE_TOKEN",
        _PRODUCTION_ENVIRONMENT["DWP_AGENT_SERVICE_TOKEN"],
    )
    monkeypatch.setenv("DWP_OPENAI_BASE_URL", "http://model.internal/v1")

    with pytest.raises(RuntimeConfigurationError) as captured:
        validate_runtime_configuration(ManagedTestKeyProvider())

    assert "distinct service identity tokens" in str(captured.value)
    assert "DWP_OPENAI_BASE_URL=https" in str(captured.value)


def test_production_runtime_accepts_approved_azure_openai_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_production(monkeypatch)
    monkeypatch.setenv("DWP_MODEL_PROVIDER", "azure_openai")
    monkeypatch.setenv(
        "AZURE_OPENAI_API_KEY",
        "azure-openai-key-for-production",
    )
    monkeypatch.setenv(
        "DWP_OPENAI_BASE_URL",
        "https://dwp-model.openai.azure.com/openai/v1",
    )

    validate_runtime_configuration(ManagedTestKeyProvider())


def test_production_runtime_rejects_azure_lookalike_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_production(monkeypatch)
    monkeypatch.setenv("DWP_MODEL_PROVIDER", "azure_openai")
    monkeypatch.setenv(
        "AZURE_OPENAI_API_KEY",
        "azure-openai-key-for-production",
    )
    monkeypatch.setenv(
        "DWP_OPENAI_BASE_URL",
        "https://dwp-model.openai.azure.com.evil.example/openai/v1",
    )

    with pytest.raises(RuntimeConfigurationError) as captured:
        validate_runtime_configuration(ManagedTestKeyProvider())

    assert "DWP_OPENAI_BASE_URL=approved-provider-endpoint" in str(captured.value)


def test_production_runtime_rejects_plaintext_key_without_managed_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_production(monkeypatch)
    monkeypatch.setenv("DWP_AGENT_DATA_KEY", base64.b64encode(b"a" * 32).decode("ascii"))

    with pytest.raises(RuntimeConfigurationError, match="DWP_AGENT_KEY_PROVIDER"):
        validate_runtime_configuration()


def _configure_production(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _PRODUCTION_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


_PRODUCTION_ENVIRONMENT = {
    "DWP_ENVIRONMENT": "production",
    "DWP_AGENT_SERVICE_TOKEN": "agent-service-token-for-production",
    "DWP_AGENT_IDENTITY_SIGNING_SECRET": "agent-identity-signing-secret-for-production",
    "DWP_PLATFORM_RUNTIME_SERVICE_TOKEN": "platform-runtime-token-for-production",
    "DWP_APPROVAL_RUNTIME_SERVICE_TOKEN": "approval-runtime-token-for-production",
    "DWP_AGENT_PRIVACY_HASH_SECRET": "agent-privacy-secret-for-production",
    "DWP_AGENT_SAFETY_SECRET": "agent-safety-secret-for-production",
    "DWP_AUDIT_INGEST_TOKEN": "audit-ingest-token-for-production",
    "DWP_API_HISTORY_INGEST_TOKEN": "history-ingest-token-for-production",
    "DWP_API_HISTORY_PRIVACY_HASH_SECRET": "history-privacy-secret-for-production",
    "OPENAI_API_KEY": "openai-project-key-for-production",
    "DWP_AGENT_DATABASE_URL": "postgresql://agent@database.internal/dwp_agent",
    "DWP_AGENT_DATABASE_REQUIRED": "true",
    "DWP_AGENT_REGISTRY_MODE": "enforced",
    "DWP_AGENT_KEY_REFERENCE": "kms://dwp-agent/payload/2026-08-v1",
    "SERVICE_PLATFORM_URL": "http://platform:8002",
    "SERVICE_APPROVAL_URL": "http://approval:8005",
    "DWP_AUDIT_COLLECTOR_URL": "http://platform:8002/internal/audit/events",
    "DWP_API_HISTORY_COLLECTOR_URL": (
        "http://platform:8002/internal/observability/api-history"
    ),
    "DWP_OPENAI_MODEL": "approved-model-snapshot",
    "DWP_OPENAI_BASE_URL": "https://api.openai.com/v1",
}


class ManagedTestKeyProvider:
    provider_id = "managed-test"
    managed = True

    def load(self) -> VersionedKeyMaterial:
        return VersionedKeyMaterial(
            provider_id=self.provider_id,
            active_version="2026-08-v1",
            active_key=base64.b64encode(b"a" * 32).decode("ascii"),
        )
