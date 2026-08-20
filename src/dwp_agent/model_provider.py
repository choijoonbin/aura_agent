from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlparse


class ModelProvider(StrEnum):
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"

    @classmethod
    def parse(cls, value: str | None) -> ModelProvider:
        normalized = (value or cls.OPENAI.value).strip().lower()
        try:
            return cls(normalized)
        except ValueError as error:
            raise ProviderConfigurationError("Unsupported model provider.") from error


class ProviderConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ResponsesProviderConfiguration:
    provider: ModelProvider
    api_key: str = field(repr=False)
    model: str
    base_url: str

    @classmethod
    def from_environment(
        cls,
        *,
        provider: str | ModelProvider | None = None,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> ResponsesProviderConfiguration:
        resolved_provider = (
            provider
            if isinstance(provider, ModelProvider)
            else ModelProvider.parse(provider or os.getenv("DWP_MODEL_PROVIDER"))
        )
        resolved_key = api_key
        if resolved_key is None:
            if resolved_provider is ModelProvider.AZURE_OPENAI:
                resolved_key = os.getenv("AZURE_OPENAI_API_KEY") or os.getenv(
                    "OPENAI_API_KEY", ""
                )
            else:
                resolved_key = os.getenv("OPENAI_API_KEY", "")

        resolved_base_url = base_url
        if resolved_base_url is None:
            resolved_base_url = os.getenv("DWP_OPENAI_BASE_URL")
        if not resolved_base_url and resolved_provider is ModelProvider.AZURE_OPENAI:
            resolved_base_url = os.getenv("AZURE_OPENAI_ENDPOINT")
        if not resolved_base_url and resolved_provider is ModelProvider.OPENAI:
            resolved_base_url = "https://api.openai.com/v1"

        normalized_base_url = _normalize_base_url(
            resolved_provider,
            resolved_base_url or "",
        )
        return cls(
            provider=resolved_provider,
            api_key=(resolved_key or "").strip(),
            model=(
                model if model is not None else os.getenv("DWP_OPENAI_MODEL", "")
            ).strip(),
            base_url=normalized_base_url,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model and self.base_url)

    @property
    def audit_label(self) -> str:
        if self.provider is ModelProvider.AZURE_OPENAI:
            return "AZURE_OPENAI"
        return "OPENAI"

    @property
    def required_api_key_environment(self) -> str:
        if self.provider is ModelProvider.AZURE_OPENAI:
            return "AZURE_OPENAI_API_KEY"
        return "OPENAI_API_KEY"

    def authentication_headers(self) -> dict[str, str]:
        if self.provider is ModelProvider.AZURE_OPENAI:
            return {"api-key": self.api_key}
        return {"Authorization": f"Bearer {self.api_key}"}

    def validate_endpoint(self, *, allow_test_override: bool = False) -> None:
        if allow_test_override:
            return
        parsed = urlparse(self.base_url)
        try:
            port = parsed.port
        except ValueError as error:
            raise ProviderConfigurationError(
                "The model provider endpoint is not approved."
            ) from error
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderConfigurationError("The model provider endpoint is not approved.")

        host = parsed.hostname.lower()
        path = parsed.path.rstrip("/")
        if self.provider is ModelProvider.OPENAI:
            approved = host == "api.openai.com" and path == "/v1"
        else:
            resource_name = host.removesuffix(".openai.azure.com")
            approved = (
                host.endswith(".openai.azure.com")
                and bool(resource_name)
                and "." not in resource_name
                and path == "/openai/v1"
            )
        if not approved:
            raise ProviderConfigurationError("The model provider endpoint is not approved.")


def _normalize_base_url(provider: ModelProvider, value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized or provider is not ModelProvider.AZURE_OPENAI:
        return normalized
    parsed = urlparse(normalized)
    if parsed.path in {"", "/"} and not parsed.query and not parsed.fragment:
        return f"{normalized}/openai/v1"
    return normalized
