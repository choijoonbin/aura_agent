from __future__ import annotations

import json
import base64
import hashlib
import hmac
import time
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dwp_agent.meeting_intelligence_api import (
    get_meeting_intelligence_provider,
    router,
)
from dwp_agent.meeting_intelligence_body_limit import (
    MEETING_INTELLIGENCE_REQUEST_LIMIT_BYTES,
    install_meeting_intelligence_body_limit,
)
from dwp_agent.meeting_intelligence_contracts import MeetingIntelligenceRequest
from dwp_agent.meeting_intelligence_provider import (
    MeetingIntelligenceConfiguration,
    MEETING_INTELLIGENCE_PROVIDER_RESPONSE_LIMIT_BYTES,
    MeetingIntelligenceProvider,
    MeetingIntelligenceUnavailable,
)
from dwp_agent.model_provider import ModelProvider


TOKEN = "meeting-service-secret-2026-at-least-32"
SIGNING_SECRET = b"0123456789abcdef0123456789abcdef"
KEY_ID = "meeting-workload-v1"
SOURCE_HASH = "a" * 64
MEETING_ID = "29f14739-0f92-469e-8528-d3731e809f55"
RUN_ID = "776d0d8e-96e2-4c29-8df5-9f5e3beaf72f"


def _app(provider: object) -> FastAPI:
    application = FastAPI()
    install_meeting_intelligence_body_limit(application)
    application.include_router(router)
    application.dependency_overrides[get_meeting_intelligence_provider] = lambda: provider
    return application


def _request() -> dict[str, object]:
    return {
        "analysisProfile": "STANDARD_RECAP_V1",
        "outputLanguage": "ko-KR",
        "sourceSha256": SOURCE_HASH,
        "transcript": [
            {
                "segmentId": "seg-001",
                "startMillis": 1_000,
                "endMillis": 5_000,
                "text": "배포 결정을 내리고 김 담당자가 금요일까지 확인하기로 했습니다.",
            }
        ],
    }


def _analysis(citation_end: int = 5_000) -> dict[str, object]:
    cited = {
        "text": "팀은 배포를 진행하기로 결정했습니다.",
        "citations": [
            {"segmentId": "seg-001", "startMillis": 1_000, "endMillis": citation_end}
        ],
    }
    return {
        "executiveSummary": cited,
        "topics": [cited],
        "decisions": [cited],
        "actionItems": [cited],
        "openQuestions": [],
        "risks": [],
        "conversationClimate": {
            "label": "ALIGNED",
            "signals": ["BALANCED_TURN_TAKING"],
            "citations": cited["citations"],
        },
    }


class _FakeProvider:
    def capability(self):
        return _provider().capability()

    def analyze(
        self, request, *, correlation_id, tenant_id, meeting_id, run_id
    ):
        assert correlation_id == "corr-meeting-1"
        assert (tenant_id, meeting_id, run_id) == (77, MEETING_ID, RUN_ID)
        return _model_analysis()


class _UnavailableProvider(_FakeProvider):
    def analyze(self, request, **kwargs):
        raise MeetingIntelligenceUnavailable("INVALID_PROVIDER_OUTPUT")


def test_internal_api_requires_dedicated_service_identity(monkeypatch) -> None:
    _configure_identity(monkeypatch)
    client = TestClient(_app(_FakeProvider()))

    response = client.get("/internal/v1/meeting-intelligence/capabilities")

    assert response.status_code == 401


def test_internal_api_rejects_oversized_content_length_before_auth(monkeypatch) -> None:
    _configure_identity(monkeypatch)
    client = TestClient(_app(_FakeProvider()))

    response = client.post(
        "/internal/v1/meeting-intelligence/analyze",
        headers={"Content-Length": str(MEETING_INTELLIGENCE_REQUEST_LIMIT_BYTES + 1)},
        content=b"{}",
    )

    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Meeting intelligence request is too large."}


def test_internal_api_rejects_oversized_chunked_body_before_auth(monkeypatch) -> None:
    _configure_identity(monkeypatch)
    client = TestClient(_app(_FakeProvider()))

    response = client.post(
        "/internal/v1/meeting-intelligence/analyze",
        content=iter([b"x" * 1_024] * (MEETING_INTELLIGENCE_REQUEST_LIMIT_BYTES // 1_024 + 1)),
    )

    assert response.status_code == 413
    assert "x" not in response.text


def test_internal_api_returns_no_store_structured_analysis(monkeypatch) -> None:
    _configure_identity(monkeypatch)
    client = TestClient(_app(_FakeProvider()))
    body = json.dumps(_request(), ensure_ascii=False, separators=(",", ":")).encode()

    response = client.post(
        "/internal/v1/meeting-intelligence/analyze",
        headers={**_identity_headers("POST", "/internal/v1/meeting-intelligence/analyze", body),
                 "Content-Type": "application/json"},
        content=body,
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["conversationClimate"]["label"] == "ALIGNED"
    assert "emotion" not in json.dumps(response.json()).lower()


def test_internal_api_exposes_only_stable_provider_failure_code(monkeypatch) -> None:
    _configure_identity(monkeypatch)
    client = TestClient(_app(_UnavailableProvider()))
    body = json.dumps(_request(), ensure_ascii=False, separators=(",", ":")).encode()

    response = client.post(
        "/internal/v1/meeting-intelligence/analyze",
        headers={**_identity_headers("POST", "/internal/v1/meeting-intelligence/analyze", body),
                 "Content-Type": "application/json"},
        content=body,
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "INVALID_PROVIDER_OUTPUT"}
    assert "배포" not in response.text


def test_workload_assertion_is_one_time_and_body_bound(monkeypatch) -> None:
    _configure_identity(monkeypatch)
    client = TestClient(_app(_FakeProvider()))
    path = "/internal/v1/meeting-intelligence/analyze"
    body = json.dumps(_request(), ensure_ascii=False, separators=(",", ":")).encode()
    headers = {
        **_identity_headers("POST", path, body),
        "Content-Type": "application/json",
    }

    first = client.post(path, headers=headers, content=body)
    replay = client.post(path, headers=headers, content=body)
    changed = json.dumps(
        {**_request(), "outputLanguage": "en-US"},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    tampered = client.post(
        path,
        headers={
            **_identity_headers("POST", path, body),
            "Content-Type": "application/json",
        },
        content=changed,
    )

    assert first.status_code == 200
    assert replay.status_code == 401
    assert tampered.status_code == 401


def test_provider_revalidates_every_model_citation() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _model_response(_analysis(citation_end=9_000))

    provider = _provider(transport=httpx.MockTransport(handler))
    request = MeetingIntelligenceRequest.model_validate(_request())

    try:
        provider.analyze(
            request,
            correlation_id="corr-meeting-1",
            tenant_id=77,
            meeting_id=MEETING_ID,
            run_id=RUN_ID,
        )
    except MeetingIntelligenceUnavailable as error:
        assert error.code == "INVALID_PROVIDER_OUTPUT"
    else:
        raise AssertionError("Out-of-range model citation was accepted")


def test_provider_capability_is_fail_closed_for_an_unapproved_endpoint() -> None:
    configuration = MeetingIntelligenceConfiguration(
        enabled=True,
        provider=ModelProvider.OPENAI,
        api_key="test-key",
        base_url="http://unapproved.local/v1",
        model="meeting-model",
        processing_region="kr-central",
        customer_data_training_disabled=True,
        provider_retention_disabled=True,
    )

    capability = MeetingIntelligenceProvider(configuration).capability()

    assert capability.available is False
    assert capability.provider_code == "DISABLED"


def test_provider_uses_zero_storage_and_untrusted_transcript_boundary() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _model_response(_analysis())

    provider = _provider(transport=httpx.MockTransport(handler))
    analysis = provider.analyze(
        MeetingIntelligenceRequest.model_validate(_request()),
        correlation_id="corr-meeting-1",
        tenant_id=77,
        meeting_id=MEETING_ID,
        run_id=RUN_ID,
    )

    assert analysis.executive_summary.citations[0].segment_id == "seg-001"
    assert captured["store"] is False
    system = captured["input"][0]["content"]  # type: ignore[index]
    assert "untrusted data" in system
    assert "individual emotion" in system


class _ChunkedBody(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


class _UnreadBody(httpx.SyncByteStream):
    def __iter__(self):
        raise AssertionError("An oversized Content-Length response body was read")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(MEETING_INTELLIGENCE_PROVIDER_RESPONSE_LIMIT_BYTES + 1),
            },
            stream=_UnreadBody(),
        ),
        httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=_ChunkedBody(
                [
                    b"x" * 1_024,
                ]
                * (MEETING_INTELLIGENCE_PROVIDER_RESPONSE_LIMIT_BYTES // 1_024 + 1)
            ),
        ),
    ],
    ids=["content-length", "chunked"],
)
def test_provider_bounds_model_response_before_json_parsing(response: httpx.Response) -> None:
    provider = _provider(transport=httpx.MockTransport(lambda _: response))

    with pytest.raises(MeetingIntelligenceUnavailable) as captured:
        provider.analyze(
            MeetingIntelligenceRequest.model_validate(_request()),
            correlation_id="corr-meeting-1",
            tenant_id=77,
            meeting_id=MEETING_ID,
            run_id=RUN_ID,
        )

    assert captured.value.code == "INVALID_PROVIDER_OUTPUT"


def _provider(transport: httpx.BaseTransport | None = None) -> MeetingIntelligenceProvider:
    configuration = MeetingIntelligenceConfiguration(
        enabled=True,
        provider=ModelProvider.OPENAI,
        api_key="test-key",
        base_url="https://models.test/v1",
        model="meeting-model",
        processing_region="kr-central",
        customer_data_training_disabled=True,
        provider_retention_disabled=True,
    )
    return MeetingIntelligenceProvider(
        configuration, transport=transport, allow_test_endpoint=True
    )


def _model_analysis():
    from dwp_agent.meeting_intelligence_contracts import MeetingIntelligenceAnalysis

    return MeetingIntelligenceAnalysis.model_validate(_analysis())


def _model_response(analysis: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(analysis, ensure_ascii=False)}
                    ],
                }
            ]
        },
    )


def _configure_identity(monkeypatch) -> None:
    monkeypatch.setenv("DWP_MEETING_INTELLIGENCE_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv(
        "DWP_MEETING_INTELLIGENCE_ASSERTION_SECRET_BASE64",
        base64.b64encode(SIGNING_SECRET).decode(),
    )
    monkeypatch.setenv("DWP_MEETING_INTELLIGENCE_ASSERTION_KEY_ID", KEY_ID)
    monkeypatch.setenv("DWP_ENVIRONMENT", "test")


def _identity_headers(method: str, path: str, body: bytes) -> dict[str, str]:
    now = int(time.time())
    payload = {
        "v": 1,
        "kid": KEY_ID,
        "method": method,
        "path": path,
        "tenantId": 77,
        "meetingId": MEETING_ID,
        "runId": RUN_ID,
        "iat": now,
        "exp": now + 45,
        "jti": str(uuid4()),
        "bodySha256": hashlib.sha256(body).hexdigest(),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=")
    signature = base64.urlsafe_b64encode(
        hmac.new(SIGNING_SECRET, b"dwp1." + encoded, hashlib.sha256).digest()
    ).rstrip(b"=")
    return {
        "X-DWP-Meeting-Intelligence-Token": TOKEN,
        "X-DWP-Meeting-Workload-Assertion":
            f"dwp1.{encoded.decode()}.{signature.decode()}",
        "X-Correlation-ID": "corr-meeting-1",
        "X-DWP-Tenant-ID": "77",
        "X-DWP-Meeting-ID": MEETING_ID,
        "X-DWP-Intelligence-Run-ID": RUN_ID,
    }
