from __future__ import annotations

import json

import httpx
import pytest

from dwp_agent.model_provider import ModelProvider
from dwp_agent.voice_provider import (
    VoiceConfigurationError,
    VoiceProvider,
    VoiceProviderConfiguration,
    VoiceProviderUnavailable,
    validate_voice_runtime_configuration,
)


def configuration(*, enabled: bool = True) -> VoiceProviderConfiguration:
    return VoiceProviderConfiguration(
        enabled=enabled,
        provider=ModelProvider.OPENAI,
        api_key="voice-test-key",
        base_url="https://api.openai.com/v1",
        transcription_model="gpt-4o-mini-transcribe",
        speech_model="gpt-4o-mini-tts",
        speech_voice="coral",
        timeout_seconds=10,
    )


def test_provider_transcribes_and_synthesizes_without_persisting_audio() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "오늘 일정을 알려주세요."})
        return httpx.Response(
            200,
            content=b"mp3-audio",
            headers={"Content-Type": "audio/mpeg"},
        )

    provider = VoiceProvider(
        configuration(), transport=httpx.MockTransport(handler)
    )

    transcript = provider.transcribe(b"ephemeral-webm", "audio/webm", "ko-KR")
    speech = provider.synthesize("오늘 일정은 두 건입니다.")

    assert transcript == "오늘 일정을 알려주세요."
    assert speech == b"mp3-audio"
    assert [request.url.path for request in requests] == [
        "/v1/audio/transcriptions",
        "/v1/audio/speech",
    ]
    assert requests[0].headers["Authorization"] == "Bearer voice-test-key"
    assert b'name="language"\r\n\r\nko' in requests[0].content
    assert json.loads(requests[1].content)["voice"] == "coral"


def test_provider_is_fail_closed_when_voice_is_disabled_or_response_is_invalid() -> None:
    disabled = VoiceProvider(configuration(enabled=False))
    invalid = VoiceProvider(
        configuration(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )

    with pytest.raises(VoiceProviderUnavailable, match="not enabled"):
        disabled.transcribe(b"audio", "audio/webm", "en")
    with pytest.raises(VoiceProviderUnavailable, match="response is invalid"):
        invalid.transcribe(b"audio", "audio/webm", "en")


def test_runtime_configuration_requires_explicit_models_and_approved_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_DWAION_VOICE_ENABLED", "true")
    monkeypatch.setenv("DWP_DWAION_VOICE_PROVIDER", "openai")
    monkeypatch.setenv("DWP_DWAION_VOICE_API_KEY", "voice-test-key")
    monkeypatch.setenv("DWP_DWAION_STT_MODEL", "gpt-4o-mini-transcribe")
    monkeypatch.delenv("DWP_DWAION_TTS_MODEL", raising=False)

    with pytest.raises(VoiceConfigurationError, match="STT/TTS"):
        validate_voice_runtime_configuration()

    monkeypatch.setenv("DWP_DWAION_TTS_MODEL", "gpt-4o-mini-tts")
    monkeypatch.setenv("DWP_DWAION_VOICE_BASE_URL", "http://unapproved.test/v1")
    with pytest.raises(VoiceConfigurationError, match="not approved"):
        validate_voice_runtime_configuration()
