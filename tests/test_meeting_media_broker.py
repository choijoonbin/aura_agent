from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dwp_agent.main import app as production_app
from dwp_agent.meeting_media_api import get_meeting_media_broker, router
from dwp_agent.meeting_media_body_limit import (
    MEETING_MEDIA_REQUEST_LIMIT_BYTES,
    install_meeting_media_body_limit,
)
from dwp_agent.meeting_media_contracts import (
    RecordingAccessTicketResponse,
    RecordingAccessTicketRequest,
    RecordingCapability,
    RecordingCommandRequest,
    RecordingCommandResponse,
    RecordingDeleteResponse,
    TranscriptDeleteResponse,
    TranscriptReadResponse,
    TranscriptReadRequest,
    TranscriptRetentionCapability,
)
from dwp_agent.meeting_media_provider import (
    RECORDING_ACCESS_PATH,
    RECORDING_CAPABILITY_PATH,
    RECORDING_DELETE_PATH,
    RECORDING_START_PATH,
    RECORDING_STOP_PATH,
    TRANSCRIPT_DELETE_PATH,
    TRANSCRIPT_READ_PATH,
    TRANSCRIPT_RETENTION_PATH,
    MeetingMediaBroker,
    MeetingMediaProviderConfiguration,
    MeetingMediaUnavailable,
)
from dwp_agent.meeting_media_security import (
    InMemoryMeetingMediaReplayStore,
    MeetingMediaAssertionVerifier,
    MeetingMediaIdentityConfiguration,
    MeetingMediaIdentityError,
    MeetingMediaPurpose,
    meeting_media_replay_store,
)


RECORDING_TOKEN = "recording-service-token-at-least-32-chars"
TRANSCRIPT_TOKEN = "transcript-service-token-at-least-32-chars"
RECORDING_SECRET = b"R" * 32
TRANSCRIPT_SECRET = b"T" * 32
RECORDING_KEY = "recording-workload-v1"
TRANSCRIPT_KEY = "transcript-workload-v1"
TENANT_ID = 77
MEETING_ID = UUID("29f14739-0f92-469e-8528-d3731e809f55")
RESOURCE_ID = UUID("776d0d8e-96e2-4c29-8df5-9f5e3beaf72f")
ARTIFACT_ID = UUID("130d865c-69bc-48d8-b22e-ae5761cd2728")
SOURCE_SHA256 = "a" * 64
DELETION_SHA256 = "b" * 64
JAVA_RECORDING_SERVICE_GOLDEN = (
    "dwp1.eyJ2IjoxLCJraWQiOiJyZWNvcmRpbmctd29ya2xvYWQtdjEiLCJzY29wZSI6IlNFUl"
    "ZJQ0UiLCJtZXRob2QiOiJHRVQiLCJwYXRoIjoiL2ludGVybmFsL3YxL21lZXRpbmctcmVjb3"
    "JkaW5nL2NhcGFiaWxpdHkiLCJpYXQiOjE3ODg1MDAwMDAsImV4cCI6MTc4ODUwMDAzMCwia"
    "nRpIjoiZTBhYTZjNjItNmNkMS00NWQwLWJmOTQtNTQ3OTM0MzY5ZWI4IiwiYm9keVNoYTI1"
    "NiI6ImUzYjBjNDQyOThmYzFjMTQ5YWZiZjRjODk5NmZiOTI0MjdhZTQxZTQ2NDliOTM0Y2E"
    "0OTU5OTFiNzg1MmI4NTUifQ.gGPzYIUE-jY4wnpCltX5KbUTzW8gDIzsBwz2uXB25VQ"
)
JAVA_TRANSCRIPT_RESOURCE_GOLDEN = (
    "dwp1.eyJ2IjoxLCJraWQiOiJ0cmFuc2NyaXB0LXdvcmtsb2FkLXYxIiwibWV0aG9kIjoiUE"
    "9TVCIsInBhdGgiOiIvaW50ZXJuYWwvdjEvbWVldGluZy10cmFuc2NyaXB0cy9yZWFkIiwidG"
    "VuYW50SWQiOjc3LCJtZWV0aW5nSWQiOiIyOWYxNDczOS0wZjkyLTQ2OWUtODUyOC1kMzczMW"
    "U4MDlmNTUiLCJydW5JZCI6Ijc3NmQwZDhlLTk2ZTItNGMyOS04ZGY1LTlmNWUzYmVhZjcyZi"
    "IsImlhdCI6MTc4ODUwMDAwMCwiZXhwIjoxNzg4NTAwMDMwLCJqdGkiOiJkNDcwYmNlYi1jY"
    "2RkLTRlZjUtYjkxNS00ODkyZmU1Yzk1NDIiLCJib2R5U2hhMjU2IjoiY2ZlNGQwYjI3NzQy"
    "ZDFjMzU0OTgzODJmZmM2NjNmZGI3MjZjMjVjZjE3ZTkxY2Q3MTI2ZmY1ZWQwY2YwZjU4ZiJ"
    "9.GcAOSl4-s7yT94ZfhgE_faay8NQ5OkSEdb28Xn4yMu4"
)


@pytest.fixture(autouse=True)
def configure_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_ENVIRONMENT", "test")
    monkeypatch.delenv("DWP_AGENT_DATABASE_URL", raising=False)
    monkeypatch.setenv("DWP_MEETING_RECORDING_SERVICE_TOKEN", RECORDING_TOKEN)
    monkeypatch.setenv(
        "DWP_MEETING_RECORDING_ASSERTION_SECRET_BASE64",
        base64.b64encode(RECORDING_SECRET).decode(),
    )
    monkeypatch.setenv("DWP_MEETING_RECORDING_ASSERTION_KEY_ID", RECORDING_KEY)
    monkeypatch.setenv("DWP_MEETING_TRANSCRIPT_SERVICE_TOKEN", TRANSCRIPT_TOKEN)
    monkeypatch.setenv(
        "DWP_MEETING_TRANSCRIPT_ASSERTION_SECRET_BASE64",
        base64.b64encode(TRANSCRIPT_SECRET).decode(),
    )
    monkeypatch.setenv("DWP_MEETING_TRANSCRIPT_ASSERTION_KEY_ID", TRANSCRIPT_KEY)
    meeting_media_replay_store.cache_clear()
    yield
    meeting_media_replay_store.cache_clear()


class _FakeBroker:
    def recording_capability(self) -> RecordingCapability:
        return _recording_capability()

    def recording_command(self, path, request, **values) -> RecordingCommandResponse:
        assert values["idempotency_key"] == f"{request.command_type}:{RESOURCE_ID}"
        assert values["correlation_id"] == "corr-media-001"
        state = "STARTED" if path == RECORDING_START_PATH else "STOPPED"
        return RecordingCommandResponse(
            schemaVersion="meeting-recording-command-v1",
            recordingSessionId=request.recording_session_id,
            commandState=state,
            providerCommandId=f"provider-{state.lower()}-001",
        )

    def recording_access_ticket(self, request, **values) -> RecordingAccessTicketResponse:
        assert values["correlation_id"] == "corr-media-001"
        return RecordingAccessTicketResponse(
            schemaVersion="meeting-recording-access-ticket-v1",
            artifactId=request.artifact_id,
            requesterUserId=request.requester_user_id,
            artifactVersion=request.artifact_version,
            sourceSha256=request.source_sha256,
            accessUrl="https://media.example.test/playback/opaque?ticket=opaque-ticket-value-1234",
            expiresAt=datetime.now(UTC) + timedelta(seconds=30),
        )

    def recording_delete(self, request, **values) -> RecordingDeleteResponse:
        assert values["idempotency_key"] == f"DELETE:{ARTIFACT_ID}"
        return RecordingDeleteResponse(
            schemaVersion="meeting-recording-delete-v1",
            artifactId=request.artifact_id,
            artifactVersion=request.artifact_version,
            deletionBindingSha256=request.deletion_binding_sha256,
            deletionState="DELETED",
            cryptoShredded=True,
            providerDeletionId="recording-delete-proof-001",
            deletedAt=datetime.now(UTC),
        )

    def transcript_retention_capability(self) -> TranscriptRetentionCapability:
        return _transcript_capability()

    def transcript_read(self, request, **values) -> TranscriptReadResponse:
        assert values["correlation_id"] == "corr-media-001"
        return TranscriptReadResponse(
            schemaVersion="meeting-transcript-v1",
            sourceSha256=request.source_sha256,
            segments=[
                {
                    "segmentId": "segment-001",
                    "startMillis": 0,
                    "endMillis": 1_000,
                    "text": "민감 원문은 응답 중에만 존재합니다.",
                }
            ],
        )

    def transcript_delete(self, request, **values) -> TranscriptDeleteResponse:
        assert values["idempotency_key"] == f"DELETE:{ARTIFACT_ID}"
        return TranscriptDeleteResponse(
            schemaVersion="meeting-transcript-delete-v1",
            artifactId=request.artifact_id,
            artifactVersion=request.artifact_version,
            deletionBindingSha256=request.deletion_binding_sha256,
            deletionState="DELETED",
            cryptoShredded=True,
            providerDeletionId="transcript-delete-proof-001",
            deletedAt=datetime.now(UTC),
        )


def _app(broker: object | None = None) -> FastAPI:
    application = FastAPI()
    install_meeting_media_body_limit(application)
    application.include_router(router)
    if broker is not None:
        application.dependency_overrides[get_meeting_media_broker] = lambda: broker
    return application


def test_all_java_contract_routes_are_runtime_only_and_absent_from_public_openapi() -> None:
    expected = {
        RECORDING_CAPABILITY_PATH,
        RECORDING_START_PATH,
        RECORDING_STOP_PATH,
        RECORDING_ACCESS_PATH,
        RECORDING_DELETE_PATH,
        TRANSCRIPT_READ_PATH,
        TRANSCRIPT_DELETE_PATH,
        TRANSCRIPT_RETENTION_PATH,
    }
    assert not expected.intersection(_app().openapi()["paths"])
    assert not {
        path for path in production_app.openapi()["paths"] if path.startswith("/internal/")
    }


def test_java_golden_service_assertion_is_single_use_and_empty_body_bound() -> None:
    # Literal generated from Java MeetingWorkloadAssertionSigner.signService with fixed clock/JTI.
    assertion = JAVA_RECORDING_SERVICE_GOLDEN
    verifier = MeetingMediaAssertionVerifier(
        MeetingMediaIdentityConfiguration(
            purpose=MeetingMediaPurpose.RECORDING,
            service_token=RECORDING_TOKEN,
            signing_secret=RECORDING_SECRET,
            key_id=RECORDING_KEY,
        ),
        InMemoryMeetingMediaReplayStore(
            now=lambda: datetime.fromtimestamp(1_788_500_001, tz=UTC)
        ),
        now=lambda: 1_788_500_001,
    )
    values = dict(
        service_token=RECORDING_TOKEN,
        assertion=assertion,
        method="GET",
        path=RECORDING_CAPABILITY_PATH,
        body=b"",
    )

    verifier.verify_service(**values)

    with pytest.raises(MeetingMediaIdentityError, match="already used"):
        verifier.verify_service(**values)
    with pytest.raises(MeetingMediaIdentityError, match="Invalid"):
        verifier.verify_service(**{**values, "body": b"{}"})


def test_java_golden_resource_assertion_binds_purpose_method_path_body_and_resource() -> None:
    body = _transcript_read_body()
    assertion = JAVA_TRANSCRIPT_RESOURCE_GOLDEN
    verifier = MeetingMediaAssertionVerifier(
        MeetingMediaIdentityConfiguration(
            purpose=MeetingMediaPurpose.TRANSCRIPT,
            service_token=TRANSCRIPT_TOKEN,
            signing_secret=TRANSCRIPT_SECRET,
            key_id=TRANSCRIPT_KEY,
        ),
        InMemoryMeetingMediaReplayStore(),
        now=lambda: 1_788_500_001,
    )
    values = dict(
        service_token=TRANSCRIPT_TOKEN,
        assertion=assertion,
        method="POST",
        path=TRANSCRIPT_READ_PATH,
        tenant_id=TENANT_ID,
        meeting_id=MEETING_ID,
        resource_id=RESOURCE_ID,
        body=body,
    )

    verifier.verify_resource(**values)

    for change in (
        {"path": TRANSCRIPT_DELETE_PATH},
        {"body": body + b" "},
        {"resource_id": ARTIFACT_ID},
        {"service_token": RECORDING_TOKEN},
    ):
        with pytest.raises(MeetingMediaIdentityError, match="Invalid"):
            fresh = MeetingMediaAssertionVerifier(
                verifier.configuration, InMemoryMeetingMediaReplayStore(), now=lambda: 1_788_500_001
            )
            fresh.verify_resource(**{**values, **change})


@pytest.mark.parametrize(
    ("path", "command_type", "terminal"),
    [
        (RECORDING_START_PATH, "START", "STARTED"),
        (RECORDING_STOP_PATH, "STOP", "STOPPED"),
    ],
)
def test_recording_command_matches_java_body_headers_and_response(
    path: str, command_type: str, terminal: str
) -> None:
    client = TestClient(_app(_FakeBroker()))
    body = _recording_command_body(command_type)
    response = client.post(
        path,
        headers=_resource_headers(
            MeetingMediaPurpose.RECORDING,
            path,
            body,
            resource_id=RESOURCE_ID,
            extra={
                "X-DWP-Recording-Session-ID": str(RESOURCE_ID),
                "Idempotency-Key": f"{command_type}:{RESOURCE_ID}",
            },
        ),
        content=body,
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "schemaVersion": "meeting-recording-command-v1",
        "recordingSessionId": str(RESOURCE_ID),
        "commandState": terminal,
        "providerCommandId": f"provider-{terminal.lower()}-001",
    }


def test_recording_access_delete_and_transcript_contracts_match_java() -> None:
    client = TestClient(_app(_FakeBroker()))
    access_body = _recording_access_body()
    access = client.post(
        RECORDING_ACCESS_PATH,
        headers=_resource_headers(
            MeetingMediaPurpose.RECORDING,
            RECORDING_ACCESS_PATH,
            access_body,
            resource_id=ARTIFACT_ID,
            extra={
                "X-DWP-Recording-Artifact-ID": str(ARTIFACT_ID),
                "X-DWP-Requester-User-ID": "101",
            },
        ),
        content=access_body,
    )
    recording_delete_body = _recording_delete_body()
    recording_delete = client.post(
        RECORDING_DELETE_PATH,
        headers=_resource_headers(
            MeetingMediaPurpose.RECORDING,
            RECORDING_DELETE_PATH,
            recording_delete_body,
            resource_id=ARTIFACT_ID,
            extra={
                "X-DWP-Recording-Artifact-ID": str(ARTIFACT_ID),
                "Idempotency-Key": f"DELETE:{ARTIFACT_ID}",
            },
        ),
        content=recording_delete_body,
    )
    transcript_body = _transcript_read_body()
    transcript = client.post(
        TRANSCRIPT_READ_PATH,
        headers=_resource_headers(
            MeetingMediaPurpose.TRANSCRIPT,
            TRANSCRIPT_READ_PATH,
            transcript_body,
            resource_id=RESOURCE_ID,
            extra={
                "X-DWP-Intelligence-Run-ID": str(RESOURCE_ID),
                "X-DWP-Transcript-Artifact-ID": str(ARTIFACT_ID),
                "X-DWP-Source-SHA256": SOURCE_SHA256,
            },
        ),
        content=transcript_body,
    )
    transcript_delete_body = _transcript_delete_body()
    transcript_delete = client.post(
        TRANSCRIPT_DELETE_PATH,
        headers=_resource_headers(
            MeetingMediaPurpose.TRANSCRIPT,
            TRANSCRIPT_DELETE_PATH,
            transcript_delete_body,
            resource_id=ARTIFACT_ID,
            extra={
                "X-DWP-Transcript-Artifact-ID": str(ARTIFACT_ID),
                "Idempotency-Key": f"DELETE:{ARTIFACT_ID}",
            },
        ),
        content=transcript_delete_body,
    )

    assert access.status_code == 200
    assert set(access.json()) == {
        "schemaVersion",
        "artifactId",
        "requesterUserId",
        "artifactVersion",
        "sourceSha256",
        "accessUrl",
        "expiresAt",
    }
    assert recording_delete.status_code == 200
    assert recording_delete.json()["cryptoShredded"] is True
    assert transcript.status_code == 200
    assert transcript.json()["sourceSha256"] == SOURCE_SHA256
    assert transcript_delete.status_code == 200
    assert transcript_delete.json()["deletionState"] == "DELETED"


def test_service_capabilities_use_separate_identity_keys_and_reject_replay() -> None:
    client = TestClient(_app(_FakeBroker()))
    recording_headers = _service_headers(
        MeetingMediaPurpose.RECORDING, RECORDING_CAPABILITY_PATH
    )
    transcript_headers = _service_headers(
        MeetingMediaPurpose.TRANSCRIPT, TRANSCRIPT_RETENTION_PATH
    )

    recording = client.get(RECORDING_CAPABILITY_PATH, headers=recording_headers)
    recording_replay = client.get(RECORDING_CAPABILITY_PATH, headers=recording_headers)
    wrong_purpose = client.get(RECORDING_CAPABILITY_PATH, headers=transcript_headers)
    transcript = client.get(TRANSCRIPT_RETENTION_PATH, headers=transcript_headers)

    assert recording.status_code == 200
    assert recording.json()["schemaVersion"] == "meeting-recording-capability-v1"
    assert recording_replay.status_code == 401
    assert wrong_purpose.status_code == 401
    assert transcript.status_code == 200
    assert transcript.json()["schemaVersion"] == (
        "meeting-transcript-retention-capability-v1"
    )
    assert transcript.json()["processingRegion"] == "ap-northeast-2"


def test_header_body_mismatch_and_unknown_fields_fail_without_echoing_locator() -> None:
    client = TestClient(_app(_FakeBroker()))
    private_locator = "private/customer/object-key"
    payload = json.loads(_recording_delete_body())
    payload["objectKey"] = private_locator
    payload["unexpected"] = "private-content"
    body = json.dumps(payload, separators=(",", ":")).encode()
    response = client.post(
        RECORDING_DELETE_PATH,
        headers=_resource_headers(
            MeetingMediaPurpose.RECORDING,
            RECORDING_DELETE_PATH,
            body,
            resource_id=ARTIFACT_ID,
            extra={
                "X-DWP-Recording-Artifact-ID": str(ARTIFACT_ID),
                "Idempotency-Key": f"DELETE:{ARTIFACT_ID}",
            },
        ),
        content=body,
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "INVALID_RECORDING_DELETE_REQUEST"}
    assert private_locator not in response.text
    assert "private-content" not in response.text


def test_duplicate_security_header_is_rejected_before_provider_dispatch() -> None:
    client = TestClient(_app(_FakeBroker()))
    body = _recording_command_body()
    headers = _resource_headers(
        MeetingMediaPurpose.RECORDING,
        RECORDING_START_PATH,
        body,
        resource_id=RESOURCE_ID,
        extra={
            "X-DWP-Recording-Session-ID": str(RESOURCE_ID),
            "Idempotency-Key": f"START:{RESOURCE_ID}",
        },
    )
    raw_headers = list(headers.items()) + [
        ("X-DWP-Meeting-Recording-Token", "attacker-controlled-duplicate-token")
    ]

    response = client.post(RECORDING_START_PATH, headers=raw_headers, content=body)

    assert response.status_code == 401


def test_media_body_limit_rejects_declared_and_chunked_oversize_before_auth() -> None:
    client = TestClient(_app(_FakeBroker()))
    declared = client.post(
        RECORDING_START_PATH,
        headers={"Content-Length": str(MEETING_MEDIA_REQUEST_LIMIT_BYTES + 1)},
        content=b"{}",
    )
    chunked = client.post(
        TRANSCRIPT_READ_PATH,
        content=iter([b"x" * 1_024] * (MEETING_MEDIA_REQUEST_LIMIT_BYTES // 1_024 + 1)),
    )

    assert declared.status_code == 413
    assert chunked.status_code == 413
    assert declared.headers["cache-control"] == "no-store"
    assert "x" not in chunked.text


def test_managed_recording_provider_requires_attestation_and_live_capability() -> None:
    invalid = _provider_configuration(MeetingMediaPurpose.RECORDING, attested=False)
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_recording_capability().model_dump(by_alias=True))

    unavailable = MeetingMediaBroker(
        recording=invalid,
        transcript=_provider_configuration(MeetingMediaPurpose.TRANSCRIPT, enabled=False),
        recording_transport=httpx.MockTransport(handler),
    ).recording_capability()

    assert unavailable.available is False
    assert called is False


def test_managed_provider_is_https_allowlisted_redirect_free_and_response_bounded() -> None:
    configuration = _provider_configuration(MeetingMediaPurpose.RECORDING)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"Location": "https://evil.example.test/capability"},
            json=_recording_capability().model_dump(by_alias=True),
        )

    capability = MeetingMediaBroker(
        recording=configuration,
        transcript=_provider_configuration(MeetingMediaPurpose.TRANSCRIPT, enabled=False),
        recording_transport=httpx.MockTransport(handler),
    ).recording_capability()

    assert capability.available is False
    assert len(requests) == 1
    assert requests[0].url.host == "recording-broker.example.test"
    assert requests[0].headers["authorization"] == "Bearer " + "u" * 40
    assert "Meeting-Recording-Token" not in str(requests[0].headers)
    assert "Workload-Assertion" not in str(requests[0].headers)

    oversized = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Content-Length": "1025"},
            content=b"{}",
            request=request,
        )
    )
    small_limit = MeetingMediaProviderConfiguration(
        **{**configuration.__dict__, "maximum_response_bytes": 1_024}
    )
    bounded = MeetingMediaBroker(
        recording=small_limit,
        transcript=_provider_configuration(MeetingMediaPurpose.TRANSCRIPT, enabled=False),
        recording_transport=oversized,
    ).recording_capability()
    assert bounded.available is False


def test_managed_recording_command_forwards_exact_contract_with_bounded_identity() -> None:
    configuration = _provider_configuration(MeetingMediaPurpose.RECORDING)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == RECORDING_CAPABILITY_PATH:
            return httpx.Response(
                200, json=_recording_capability().model_dump(by_alias=True)
            )
        return httpx.Response(
            200,
            json={
                "schemaVersion": "meeting-recording-command-v1",
                "recordingSessionId": str(RESOURCE_ID),
                "commandState": "STARTED",
                "providerCommandId": "provider-start-001",
            },
        )

    broker = MeetingMediaBroker(
        recording=configuration,
        transcript=_provider_configuration(MeetingMediaPurpose.TRANSCRIPT, enabled=False),
        recording_transport=httpx.MockTransport(handler),
    )
    response = broker.recording_command(
        RECORDING_START_PATH,
        RecordingCommandRequest.model_validate_json(_recording_command_body()),
        correlation_id="corr-media-001",
        idempotency_key=f"START:{RESOURCE_ID}",
    )

    assert response.command_state == "STARTED"
    assert len(captured) == 2
    forwarded = captured[1]
    assert forwarded.url.path == RECORDING_START_PATH
    assert forwarded.headers["idempotency-key"] == f"START:{RESOURCE_ID}"
    assert forwarded.headers["x-correlation-id"] == "corr-media-001"
    assert set(json.loads(forwarded.content)) == {
        "schemaVersion",
        "commandType",
        "tenantId",
        "meetingId",
        "recordingSessionId",
        "planVersion",
        "noticeId",
        "providerRoomName",
    }
    assert not {
        "objectKey",
        "transcript",
        "participantName",
        "serviceToken",
    }.intersection(json.loads(forwarded.content))


def test_managed_transcript_response_is_hash_bound_and_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recording = _provider_configuration(MeetingMediaPurpose.RECORDING, enabled=False)
    transcript = _provider_configuration(MeetingMediaPurpose.TRANSCRIPT)
    secret_text = "raw-private-transcript-must-not-be-logged"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TRANSCRIPT_RETENTION_PATH:
            return httpx.Response(
                200, json=_transcript_capability().model_dump(by_alias=True)
            )
        return httpx.Response(
            200,
            json={
                "schemaVersion": "meeting-transcript-v1",
                "sourceSha256": "c" * 64,
                "segments": [
                    {
                        "segmentId": "s1",
                        "startMillis": 0,
                        "endMillis": 1,
                        "text": secret_text,
                    }
                ],
            },
        )

    broker = MeetingMediaBroker(
        recording=recording,
        transcript=transcript,
        transcript_transport=httpx.MockTransport(handler),
    )
    request = _transcript_request_model()

    with pytest.raises(MeetingMediaUnavailable, match="unavailable"):
        broker.transcript_read(request, correlation_id="corr-media-001")

    assert secret_text not in caplog.text


def test_transcript_capability_region_mismatch_with_attestation_fails_closed() -> None:
    recording = _provider_configuration(MeetingMediaPurpose.RECORDING, enabled=False)
    transcript = _provider_configuration(MeetingMediaPurpose.TRANSCRIPT)
    mismatched = _transcript_capability().model_copy(
        update={"processing_region": "us-east-1"}
    )
    broker = MeetingMediaBroker(
        recording=recording,
        transcript=transcript,
        transcript_transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=mismatched.model_dump(by_alias=True), request=request
            )
        ),
    )

    capability = broker.transcript_retention_capability()

    assert capability.available is False
    assert capability.processing_region == "none"


def test_access_ticket_rejects_locator_or_hash_leak_in_short_lived_url() -> None:
    recording = _provider_configuration(MeetingMediaPurpose.RECORDING)
    expires = datetime.now(UTC) + timedelta(seconds=30)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == RECORDING_CAPABILITY_PATH:
            return httpx.Response(
                200, json=_recording_capability().model_dump(by_alias=True)
            )
        return httpx.Response(
            200,
            json={
                "schemaVersion": "meeting-recording-access-ticket-v1",
                "artifactId": str(ARTIFACT_ID),
                "requesterUserId": 101,
                "artifactVersion": 4,
                "sourceSha256": SOURCE_SHA256,
                "accessUrl": (
                    "https://media.example.test/playback/" + SOURCE_SHA256
                    + "?ticket=opaque-ticket-value-1234"
                ),
                "expiresAt": expires.isoformat(),
            },
        )

    broker = MeetingMediaBroker(
        recording=recording,
        transcript=_provider_configuration(MeetingMediaPurpose.TRANSCRIPT, enabled=False),
        recording_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(MeetingMediaUnavailable, match="unavailable"):
        broker.recording_access_ticket(
            _recording_access_model(), correlation_id="corr-media-001"
        )


def _recording_capability() -> RecordingCapability:
    return RecordingCapability(
        available=True,
        egressAvailable=True,
        storageAvailable=True,
        speechToTextAvailable=True,
        deletionAvailable=True,
        cryptoShredAvailable=True,
        orphanCleanupAvailable=True,
        maximumOrphanTtlSeconds=300,
        legacyLocatorDeletionAvailable=True,
        customerManagedStorage=True,
        providerRetentionDisabled=True,
        processingRegion="ap-northeast-2",
        providerCode="GOVERNED_EGRESS",
    )


def _transcript_capability() -> TranscriptRetentionCapability:
    return TranscriptRetentionCapability(
        available=True,
        deletionAvailable=True,
        cryptoShredAvailable=True,
        customerManagedStorage=True,
        providerRetentionDisabled=True,
        orphanCleanupAvailable=True,
        maximumOrphanTtlSeconds=300,
        legacyLocatorDeletionAvailable=True,
        providerCode="TRANSCRIPT_BROKER",
        storageProviderCode="BROKER",
        processingRegion="ap-northeast-2",
    )


def _provider_configuration(
    purpose: MeetingMediaPurpose,
    *,
    enabled: bool = True,
    attested: bool = True,
) -> MeetingMediaProviderConfiguration:
    host = (
        "recording-broker.example.test"
        if purpose is MeetingMediaPurpose.RECORDING
        else "transcript-broker.example.test"
    )
    provider = "GOVERNED_EGRESS" if purpose is MeetingMediaPurpose.RECORDING else "TRANSCRIPT_BROKER"
    storage = "BROKER"
    attestation, public_key = _attestation(purpose, host, provider, storage)
    return MeetingMediaProviderConfiguration(
        purpose=purpose,
        enabled=enabled,
        base_url=f"https://{host}",
        allowed_hosts=frozenset({host}),
        api_token="u" * 40,
        provider_code=provider,
        storage_provider_code=storage,
        processing_region="ap-northeast-2",
        policy_attestation=attestation if attested else "",
        attestation_public_key_base64=public_key,
        attestation_key_id="media-policy-v1",
        approved_policy_sha256="f" * 64,
        access_allowed_hosts=frozenset({"media.example.test"}),
        access_path_prefix="/playback/",
        access_ticket_ttl_seconds=120,
        timeout_seconds=2,
        maximum_response_bytes=(1_000_000 if purpose is MeetingMediaPurpose.RECORDING else 5_000_000),
    )


def _attestation(
    purpose: MeetingMediaPurpose, host: str, provider: str, storage: str
) -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    now = int(time.time())
    payload = {
        "v": 1,
        "kid": "media-policy-v1",
        "attestationId": str(uuid4()),
        "purpose": purpose.value,
        "originHost": host,
        "providerCode": provider,
        "storageProviderCode": storage,
        "processingRegion": "ap-northeast-2",
        "egressAvailable": purpose is MeetingMediaPurpose.RECORDING,
        "storageAvailable": True,
        "speechToTextAvailable": True,
        "deletionAvailable": True,
        "cryptoShredAvailable": True,
        "orphanCleanupAvailable": True,
        "maximumOrphanTtlSeconds": 300,
        "customerManagedStorage": True,
        "providerRetentionDisabled": True,
        "policySha256": "f" * 64,
        "issuedAt": now - 1,
        "expiresAt": now + 300,
    }
    encoded = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signing_input = f"dwpma1.{encoded}"
    compact = f"{signing_input}.{_b64(private_key.sign(signing_input.encode()))}"
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode()
    return compact, public_key


def _recording_command_body(command_type: str = "START") -> bytes:
    return json.dumps(
        {
            "schemaVersion": "meeting-recording-command-v1",
            "commandType": command_type,
            "tenantId": TENANT_ID,
            "meetingId": str(MEETING_ID),
            "recordingSessionId": str(RESOURCE_ID),
            "planVersion": 4,
            "noticeId": str(UUID("77061892-9cf4-4439-8388-f257b36c83a9")),
            "providerRoomName": "tenant-77-room",
        },
        separators=(",", ":"),
    ).encode()


def _recording_access_body() -> bytes:
    return json.dumps(
        {
            "schemaVersion": "meeting-recording-access-ticket-v1",
            "tenantId": TENANT_ID,
            "meetingId": str(MEETING_ID),
            "artifactId": str(ARTIFACT_ID),
            "requesterUserId": 101,
            "storageProvider": "BROKER",
            "objectKey": "opaque/recording/object",
            "contentType": "video/mp4",
            "sourceSha256": SOURCE_SHA256,
            "artifactVersion": 4,
            "expiresNoLaterThan": (datetime.now(UTC) + timedelta(seconds=90)).isoformat(),
        },
        separators=(",", ":"),
    ).encode()


def _recording_delete_body() -> bytes:
    return _delete_body("meeting-recording-delete-v1")


def _transcript_delete_body() -> bytes:
    return _delete_body("meeting-transcript-delete-v1")


def _delete_body(schema: str) -> bytes:
    return json.dumps(
        {
            "schemaVersion": schema,
            "tenantId": TENANT_ID,
            "meetingId": str(MEETING_ID),
            "artifactId": str(ARTIFACT_ID),
            "storageProvider": "BROKER",
            "objectKey": "opaque/media/object",
            "deletionBindingSha256": DELETION_SHA256,
            "artifactVersion": 4,
        },
        separators=(",", ":"),
    ).encode()


def _transcript_read_body() -> bytes:
    return json.dumps(
        {
            "schemaVersion": "meeting-transcript-read-v1",
            "tenantId": TENANT_ID,
            "meetingId": str(MEETING_ID),
            "runId": str(RESOURCE_ID),
            "artifactId": str(ARTIFACT_ID),
            "sourceSha256": SOURCE_SHA256,
        },
        separators=(",", ":"),
    ).encode()


def _transcript_request_model():
    return TranscriptReadRequest.model_validate_json(_transcript_read_body())


def _recording_access_model():
    return RecordingAccessTicketRequest.model_validate_json(_recording_access_body())


def _service_headers(purpose: MeetingMediaPurpose, path: str) -> dict[str, str]:
    token, secret, key_id = _identity(purpose)
    payload = {
        "v": 1,
        "kid": key_id,
        "scope": "SERVICE",
        "method": "GET",
        "path": path,
        "iat": int(time.time()),
        "exp": int(time.time()) + 30,
        "jti": str(uuid4()),
        "bodySha256": hashlib.sha256(b"").hexdigest(),
    }
    return {
        _token_name(purpose): token,
        "X-DWP-Meeting-Workload-Assertion": _sign(payload, secret),
    }


def _resource_headers(
    purpose: MeetingMediaPurpose,
    path: str,
    body: bytes,
    *,
    resource_id: UUID,
    extra: dict[str, str],
) -> dict[str, str]:
    token, secret, key_id = _identity(purpose)
    now = int(time.time())
    payload = {
        "v": 1,
        "kid": key_id,
        "method": "POST",
        "path": path,
        "tenantId": TENANT_ID,
        "meetingId": str(MEETING_ID),
        "runId": str(resource_id),
        "iat": now,
        "exp": now + 30,
        "jti": str(uuid4()),
        "bodySha256": hashlib.sha256(body).hexdigest(),
    }
    return {
        _token_name(purpose): token,
        "X-DWP-Meeting-Workload-Assertion": _sign(payload, secret),
        "X-DWP-Tenant-ID": str(TENANT_ID),
        "X-DWP-Meeting-ID": str(MEETING_ID),
        "X-Correlation-ID": "corr-media-001",
        "Content-Type": "application/json",
        **extra,
    }


def _identity(purpose: MeetingMediaPurpose) -> tuple[str, bytes, str]:
    if purpose is MeetingMediaPurpose.RECORDING:
        return RECORDING_TOKEN, RECORDING_SECRET, RECORDING_KEY
    return TRANSCRIPT_TOKEN, TRANSCRIPT_SECRET, TRANSCRIPT_KEY


def _token_name(purpose: MeetingMediaPurpose) -> str:
    return (
        "X-DWP-Meeting-Recording-Token"
        if purpose is MeetingMediaPurpose.RECORDING
        else "X-DWP-Meeting-Transcript-Token"
    )


def _sign(payload: dict[str, object], secret: bytes) -> str:
    encoded = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"dwp1.{encoded}"
    signature = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()
