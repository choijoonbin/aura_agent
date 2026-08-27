from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx

from .model_provider import (
    ModelProvider,
    ProviderConfigurationError,
    ResponsesProviderConfiguration,
)


class VoiceConfigurationError(RuntimeError):
    pass


class VoiceProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class VoiceProviderConfiguration:
    enabled: bool
    provider: ModelProvider
    api_key: str = field(repr=False)
    base_url: str
    transcription_model: str
    speech_model: str
    speech_voice: str
    timeout_seconds: float = 25.0

    @classmethod
    def from_environment(cls) -> "VoiceProviderConfiguration":
        try:
            provider = ModelProvider.parse(
                os.getenv("DWP_DWAION_VOICE_PROVIDER") or os.getenv("DWP_MODEL_PROVIDER")
            )
            transcription_model = os.getenv("DWP_DWAION_STT_MODEL", "").strip()
            base = ResponsesProviderConfiguration.from_environment(
                provider=provider,
                api_key=os.getenv("DWP_DWAION_VOICE_API_KEY", ""),
                model=transcription_model,
                base_url=os.getenv("DWP_DWAION_VOICE_BASE_URL") or None,
            )
        except ProviderConfigurationError as error:
            raise VoiceConfigurationError("DWAI-ON voice provider is invalid.") from error
        return cls(
            enabled=_enabled("DWP_DWAION_VOICE_ENABLED"),
            provider=provider,
            api_key=base.api_key,
            base_url=base.base_url,
            transcription_model=transcription_model,
            speech_model=os.getenv("DWP_DWAION_TTS_MODEL", "").strip(),
            speech_voice=os.getenv("DWP_DWAION_TTS_VOICE", "coral").strip(),
            timeout_seconds=_bounded_timeout(os.getenv("DWP_DWAION_VOICE_TIMEOUT_SECONDS")),
        )

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.api_key
            and self.base_url
            and self.transcription_model
            and self.speech_model
            and self.speech_voice
        )

    @property
    def authentication_headers(self) -> dict[str, str]:
        if self.provider is ModelProvider.AZURE_OPENAI:
            return {"api-key": self.api_key}
        return {"Authorization": f"Bearer {self.api_key}"}

    def validate(self, *, allow_test_endpoint: bool = False) -> None:
        if not self.enabled:
            return
        if not self.configured or self.api_key.startswith("replace-with-"):
            raise VoiceConfigurationError(
                "DWAI-ON voice requires a dedicated provider credential and STT/TTS models."
            )
        try:
            ResponsesProviderConfiguration(
                provider=self.provider,
                api_key=self.api_key,
                model=self.transcription_model,
                base_url=self.base_url,
            ).validate_endpoint(allow_test_override=allow_test_endpoint)
        except ProviderConfigurationError as error:
            raise VoiceConfigurationError(
                "DWAI-ON voice provider endpoint is not approved."
            ) from error


class VoiceProvider:
    def __init__(
        self,
        configuration: VoiceProviderConfiguration | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        allow_test_endpoint: bool = False,
    ) -> None:
        self.configuration = configuration or VoiceProviderConfiguration.from_environment()
        self.transport = transport
        self.allow_test_endpoint = allow_test_endpoint

    def transcribe(self, audio: bytes, content_type: str, locale: str) -> str:
        self._require_ready()
        extension = _extension(content_type)
        data = {
            "model": self.configuration.transcription_model,
            "language": locale.split("-", 1)[0].lower(),
            "response_format": "json",
        }
        response = self._post(
            "/audio/transcriptions",
            data=data,
            files={"file": (f"voice.{extension}", audio, content_type)},
        )
        try:
            text = str(response.json()["text"]).strip()
        except (ValueError, KeyError, TypeError) as error:
            raise VoiceProviderUnavailable("The voice transcription response is invalid.") from error
        if not text or len(text) > 4_000:
            raise VoiceProviderUnavailable("The voice transcription response is invalid.")
        return text

    def synthesize(self, text: str) -> bytes:
        self._require_ready()
        response = self._post(
            "/audio/speech",
            json={
                "model": self.configuration.speech_model,
                "input": text,
                "voice": self.configuration.speech_voice,
                "response_format": "mp3",
            },
        )
        audio = response.content
        if not audio or len(audio) > 8 * 1024 * 1024:
            raise VoiceProviderUnavailable("The voice synthesis response is invalid.")
        return audio

    def _require_ready(self) -> None:
        if not self.configuration.enabled:
            raise VoiceProviderUnavailable("DWAI-ON voice is not enabled for this environment.")
        try:
            self.configuration.validate(allow_test_endpoint=self.allow_test_endpoint)
        except VoiceConfigurationError as error:
            raise VoiceProviderUnavailable(str(error)) from error

    def _post(self, path: str, **kwargs: object) -> httpx.Response:
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=self.configuration.timeout_seconds,
            ) as client:
                response = client.post(
                    f"{self.configuration.base_url}{path}",
                    headers=self.configuration.authentication_headers,
                    **kwargs,
                )
                response.raise_for_status()
                return response
        except httpx.HTTPError as error:
            raise VoiceProviderUnavailable("The voice provider is unavailable.") from error


def validate_voice_runtime_configuration() -> None:
    VoiceProviderConfiguration.from_environment().validate(
        allow_test_endpoint=(
            os.getenv("DWP_AGENT_ALLOW_TEST_MODEL_URL", "false").strip().lower() == "true"
        )
    )


def _enabled(name: str) -> bool:
    value = os.getenv(name, "false").strip().lower()
    if value not in {"true", "false"}:
        raise VoiceConfigurationError(f"{name} must be true or false.")
    return value == "true"


def _bounded_timeout(value: str | None) -> float:
    try:
        timeout = float(value or "25")
    except ValueError as error:
        raise VoiceConfigurationError("DWP_DWAION_VOICE_TIMEOUT_SECONDS is invalid.") from error
    if not 3 <= timeout <= 30:
        raise VoiceConfigurationError(
            "DWP_DWAION_VOICE_TIMEOUT_SECONDS must be between 3 and 30 seconds."
        )
    return timeout


def _extension(content_type: str) -> str:
    extensions = {
        "audio/webm": "webm",
        "audio/ogg": "ogg",
        "audio/mp4": "mp4",
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
    }
    try:
        return extensions[content_type]
    except KeyError as error:
        raise VoiceProviderUnavailable("The voice recording format is not supported.") from error
