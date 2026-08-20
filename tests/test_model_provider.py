import pytest

from dwp_agent.model_provider import (
    ModelProvider,
    ProviderConfigurationError,
    ResponsesProviderConfiguration,
)


def test_azure_configuration_normalizes_resource_endpoint_and_hides_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_MODEL_PROVIDER", "azure_openai")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://dwp-model.openai.azure.com/",
    )
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-secret-that-must-not-leak")
    monkeypatch.setenv("DWP_OPENAI_MODEL", "dwp-gpt-deployment")

    configuration = ResponsesProviderConfiguration.from_environment()

    assert configuration.provider is ModelProvider.AZURE_OPENAI
    assert configuration.base_url == "https://dwp-model.openai.azure.com/openai/v1"
    assert configuration.authentication_headers() == {
        "api-key": "azure-secret-that-must-not-leak"
    }
    assert "azure-secret-that-must-not-leak" not in repr(configuration)
    configuration.validate_endpoint()


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://dwp-model.openai.azure.com/openai/v1",
        "https://dwp-model.openai.azure.com.evil.example/openai/v1",
        "https://dwp-model.openai.azure.com/openai/deployments/dwp-gpt",
        "https://user@dwp-model.openai.azure.com/openai/v1",
    ),
)
def test_azure_configuration_rejects_unapproved_endpoints(endpoint: str) -> None:
    configuration = ResponsesProviderConfiguration.from_environment(
        provider="azure_openai",
        api_key="test-key",
        model="test-deployment",
        base_url=endpoint,
    )

    with pytest.raises(ProviderConfigurationError):
        configuration.validate_endpoint()


def test_openai_configuration_keeps_official_v1_contract() -> None:
    configuration = ResponsesProviderConfiguration.from_environment(
        provider="openai",
        api_key="test-key",
        model="gpt-test",
        base_url="https://api.openai.com/v1/",
    )

    assert configuration.base_url == "https://api.openai.com/v1"
    assert configuration.authentication_headers() == {
        "Authorization": "Bearer test-key"
    }
    configuration.validate_endpoint()
