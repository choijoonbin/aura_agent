from __future__ import annotations

import asyncio

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import dwp_agent.voice_api as voice_api_module
from dwp_agent.main import app
from dwp_agent.voice_provider import VoiceProviderUnavailable


SERVICE_TOKEN = "test-gateway-service-token"


class FakeVoiceProvider:
    def __init__(self) -> None:
        self.transcription: tuple[bytes, str, str] | None = None
        self.speech_text: str | None = None
        self.error: Exception | None = None

    def transcribe(self, audio: bytes, content_type: str, locale: str) -> str:
        if self.error:
            raise self.error
        self.transcription = (audio, content_type, locale)
        return "오늘 우선순위를 알려주세요."

    def synthesize(self, text: str) -> bytes:
        if self.error:
            raise self.error
        self.speech_text = text
        return b"safe-mp3"


@pytest.fixture(autouse=True)
def configured_runtime(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.delenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", raising=False)
    monkeypatch.setattr(voice_api_module, "require_delivery_capability", lambda **_: None)
    yield
    app.dependency_overrides.pop(voice_api_module.get_voice_provider, None)


async def request(
    method: str,
    path: str,
    *,
    content: bytes | None = None,
    json: dict[str, str] | None = None,
    content_type: str = "application/json",
    permissions: str = "APP.ASK:VIEW",
    locale: str = "ko-KR",
) -> httpx.Response:
    headers = {
        "X-DWP-Service-Token": SERVICE_TOKEN,
        "X-DWP-Tenant-ID": "42",
        "X-DWP-User-ID": "user-7",
        "X-DWP-Permissions": permissions,
        "X-Correlation-ID": "voice-correlation",
        "X-DWP-Voice-Locale": locale,
        "Content-Type": content_type,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(
            method, path, headers=headers, content=content, json=json
        )


def test_transcription_requires_access_and_returns_reviewable_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeVoiceProvider()
    app.dependency_overrides[voice_api_module.get_voice_provider] = lambda: provider

    allowed = asyncio.run(
        request(
            "POST",
            "/v1/voice/transcriptions",
            content=b"ephemeral-audio",
            content_type="audio/webm",
        )
    )
    denied = asyncio.run(
        request(
            "POST",
            "/v1/voice/transcriptions",
            content=b"ephemeral-audio",
            content_type="audio/webm",
            permissions="APP.WORK:VIEW",
        )
    )

    assert allowed.status_code == 200
    assert allowed.headers["Cache-Control"] == "no-store"
    assert allowed.json()["data"] == {
        "text": "오늘 우선순위를 알려주세요.",
        "language": "ko-KR",
    }
    assert provider.transcription == (b"ephemeral-audio", "audio/webm", "ko-KR")
    assert denied.status_code == 403


def test_transcription_rejects_unsupported_media_and_invalid_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.dependency_overrides[voice_api_module.get_voice_provider] = FakeVoiceProvider

    unsupported = asyncio.run(
        request(
            "POST",
            "/v1/voice/transcriptions",
            content=b"audio",
            content_type="audio/aac",
        )
    )
    invalid_locale = asyncio.run(
        request(
            "POST",
            "/v1/voice/transcriptions",
            content=b"audio",
            content_type="audio/webm",
            locale="invalid_locale!",
        )
    )

    assert unsupported.status_code == 415
    assert invalid_locale.status_code == 422


def test_speech_returns_no_store_audio_and_hides_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeVoiceProvider()
    app.dependency_overrides[voice_api_module.get_voice_provider] = lambda: provider

    speech = asyncio.run(
        request(
            "POST",
            "/v1/voice/speech",
            json={"text": "검증된 답변입니다.", "locale": "ko-KR"},
        )
    )
    provider.error = VoiceProviderUnavailable("The voice provider is unavailable.")
    failed = asyncio.run(
        request(
            "POST",
            "/v1/voice/speech",
            json={"text": "검증된 답변입니다.", "locale": "ko-KR"},
        )
    )

    assert speech.status_code == 200
    assert speech.headers["Content-Type"].startswith("audio/mpeg")
    assert speech.headers["Cache-Control"] == "no-store"
    assert speech.content == b"safe-mp3"
    assert provider.speech_text == "검증된 답변입니다."
    assert failed.status_code == 503


def test_speech_rejects_whitespace_without_calling_the_provider() -> None:
    provider = FakeVoiceProvider()
    app.dependency_overrides[voice_api_module.get_voice_provider] = lambda: provider

    response = asyncio.run(
        request(
            "POST",
            "/v1/voice/speech",
            json={"text": "   ", "locale": "ko-KR"},
        )
    )

    assert response.status_code == 422
    assert provider.speech_text is None
