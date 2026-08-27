from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import dwp_agent.question_launch_api as question_launch_api_module
from dwp_agent.main import app
from dwp_agent.question_launch_store import (
    QuestionLaunchCapacityExceeded,
    QuestionLaunchNotFound,
    QuestionLaunchUnavailable,
    StoredQuestionLaunch,
)


SERVICE_TOKEN = "test-gateway-service-token"
LAUNCH_ID = UUID("00000000-0000-4000-8000-000000000016")


@pytest.fixture(autouse=True)
def configured_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.delenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", raising=False)


class FakeQuestionLaunchStore:
    def __init__(self) -> None:
        self.created: tuple[str, str, str, str] | None = None
        self.consumed: tuple[str, str, str, UUID] | None = None
        self.create_error: Exception | None = None
        self.consume_error: Exception | None = None

    def create(self, *, tenant_id: str, user_id: str, session_family_id: str, question: str):
        if self.create_error:
            raise self.create_error
        self.created = (tenant_id, user_id, session_family_id, question)
        return StoredQuestionLaunch(
            launch_id=LAUNCH_ID,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )

    def consume(self, *, tenant_id: str, user_id: str, session_family_id: str, launch_id: UUID):
        if self.consume_error:
            raise self.consume_error
        self.consumed = (tenant_id, user_id, session_family_id, launch_id)
        return "Prepare the restricted launch report."


async def request(
    method: str,
    path: str,
    *,
    json: dict,
    permissions: str = "APP.ASK:VIEW",
    extra_headers: dict[str, str] | None = None,
):
    transport = ASGITransport(app=app)
    headers = {
        "X-DWP-Service-Token": SERVICE_TOKEN,
        "X-DWP-Tenant-ID": "42",
        "X-DWP-User-ID": "user-7",
        "X-DWP-Auth-Session-ID": "session-family-1",
        "X-DWP-Identity-Plane": "TENANT",
        "X-DWP-Permissions": permissions,
        "X-Correlation-ID": "question-launch-correlation",
        **(extra_headers or {}),
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, json=json)


def test_create_and_consume_are_bound_to_verified_tenant_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeQuestionLaunchStore()
    monkeypatch.setattr(question_launch_api_module, "get_question_launch_store", lambda: store)

    created = asyncio.run(
        request("POST", "/v1/question-launches", json={"question": "  Prepare the report.  "})
    )
    consumed = asyncio.run(
        request(
            "POST",
            "/v1/question-launches/consume",
            json={"launchId": str(LAUNCH_ID)},
        )
    )

    assert created.status_code == 201
    assert created.json()["data"]["launchId"] == str(LAUNCH_ID)
    assert store.created == ("42", "user-7", "session-family-1", "Prepare the report.")
    assert consumed.status_code == 200
    assert consumed.json()["data"]["question"] == "Prepare the restricted launch report."
    assert store.consumed == ("42", "user-7", "session-family-1", LAUNCH_ID)


@pytest.mark.parametrize(
    ("headers", "permissions"),
    [
        ({"X-DWP-Identity-Plane": "PROVIDER"}, "APP.ASK:VIEW"),
        ({}, "APP.ASK:VIEW"),
        ({"X-DWP-Identity-Plane": "TENANT"}, "APP.WORK:VIEW"),
    ],
)
def test_question_launch_rejects_the_wrong_identity_plane_or_permission(
    headers: dict[str, str], permissions: str
) -> None:
    async def denied_request() -> httpx.Response:
        transport = ASGITransport(app=app)
        base_headers = {
            "X-DWP-Service-Token": SERVICE_TOKEN,
            "X-DWP-Tenant-ID": "42",
            "X-DWP-User-ID": "user-7",
            "X-DWP-Auth-Session-ID": "session-family-1",
            "X-DWP-Permissions": permissions,
            **headers,
        }
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/v1/question-launches", headers=base_headers, json={"question": "Question"}
            )

    assert asyncio.run(denied_request()).status_code == 403


def test_create_preserves_retry_after_and_hides_storage_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeQuestionLaunchStore()
    monkeypatch.setattr(question_launch_api_module, "get_question_launch_store", lambda: store)
    store.create_error = QuestionLaunchCapacityExceeded("capacity")

    limited = asyncio.run(
        request("POST", "/v1/question-launches", json={"question": "Question"})
    )
    store.create_error = QuestionLaunchUnavailable("database details")
    unavailable = asyncio.run(
        request("POST", "/v1/question-launches", json={"question": "Question"})
    )

    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert unavailable.status_code == 503
    assert "database details" not in unavailable.text


def test_consume_returns_the_same_non_disclosing_result_for_missing_launches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeQuestionLaunchStore()
    store.consume_error = QuestionLaunchNotFound("wrong tenant, expired, or replayed")
    monkeypatch.setattr(question_launch_api_module, "get_question_launch_store", lambda: store)

    response = asyncio.run(
        request(
            "POST",
            "/v1/question-launches/consume",
            json={"launchId": str(LAUNCH_ID)},
        )
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Question launch is unavailable."
    assert "tenant" not in response.text.lower()


def test_signed_delegated_identity_binds_the_auth_session_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "question-launch-signing-secret-at-least-32-characters"
    store = FakeQuestionLaunchStore()
    monkeypatch.setenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", secret)
    monkeypatch.setattr(question_launch_api_module, "get_question_launch_store", lambda: store)
    assertion = _assertion(secret=secret, session_family_id="session-family-1")

    allowed = asyncio.run(
        request(
            "POST",
            "/v1/question-launches",
            json={"question": "Signed session question"},
            extra_headers={"X-DWP-Delegated-Identity": assertion},
        )
    )
    mismatched = asyncio.run(
        request(
            "POST",
            "/v1/question-launches",
            json={"question": "Signed session question"},
            extra_headers={
                "X-DWP-Auth-Session-ID": "session-family-2",
                "X-DWP-Delegated-Identity": assertion,
            },
        )
    )

    assert allowed.status_code == 201
    assert mismatched.status_code == 401


def _assertion(*, secret: str, session_family_id: str) -> str:
    now = int(time.time())
    protected = {"alg": "HS256", "kid": "gateway-agent-v1", "typ": "dwp-identity+jwt"}
    claims = {
        "iss": "dwp-gateway",
        "aud": "dwp-agent",
        "sub": "user-7",
        "tid": "42",
        "cid": "question-launch-correlation",
        "htm": "POST",
        "htu": "/v1/question-launches",
        "roles": [],
        "permissions": ["APP.ASK:VIEW"],
        "pid": None,
        "dn": None,
        "sid": session_family_id,
        "ip": "TENANT",
        "iat": now,
        "nbf": now - 1,
        "exp": now + 15,
        "jti": "00000000-0000-4000-8000-000000000099",
    }
    encoded_header = _b64(json.dumps(protected, separators=(",", ":"), sort_keys=True).encode())
    encoded_claims = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signed = f"{encoded_header}.{encoded_claims}"
    signature = _b64(hmac.new(secret.encode(), signed.encode(), hashlib.sha256).digest())
    return f"{signed}.{signature}"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")
