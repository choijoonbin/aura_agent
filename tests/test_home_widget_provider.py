from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dwp_agent import home_widget_api
from dwp_agent import home_widget_identity
from dwp_agent.artifact_contracts import ArtifactState, ArtifactType
from dwp_agent.governed_domain_core import GovernedDomainUnavailable
from dwp_agent.home_widget_contracts import DwaionArtifactHomeItem
from dwp_agent.home_widget_body_limit import (
    HOME_WIDGET_REQUEST_LIMIT_BYTES,
    HomeWidgetBodyLimitMiddleware,
    install_home_widget_body_limit,
)


SIGNING_SECRET = "dwaion-home-delegated-identity-test-secret"
MANIFEST_HASH = home_widget_api.DEFINITION_MANIFEST_HASH
BINDING_REVISION = "c" * 64


class _Store:
    def __init__(self, artifacts: list[object] | None = None, *, unavailable: bool = False):
        self.artifacts = artifacts or []
        self.unavailable = unavailable
        self.identities: list[object] = []
        self.limits: list[int] = []
        self.unreadable_positions: set[int] = set()

    def home_projection(
        self, identity: object, *, limit: int
    ) -> object:
        self.identities.append(identity)
        self.limits.append(limit)
        if self.unavailable:
            raise GovernedDomainUnavailable("Governed artifacts are unavailable.")
        items = [
            DwaionArtifactHomeItem(
                artifact_id=artifact.artifact_id,
                title=artifact.content.title,
                artifact_type=artifact.artifact_type.value,
                state=artifact.state.value,
                revision=artifact.revision,
                updated_at=artifact.updated_at,
            )
            for artifact in self.artifacts[:limit]
        ]
        slots = [
            None if index in self.unreadable_positions else item
            for index, item in enumerate(items)
        ]
        return home_widget_api.DwaionArtifactHomeProjection(
            visible_count=len(self.artifacts),
            slots=slots,
        )


@pytest.fixture(autouse=True)
def _service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_DWAION_HOME_IDENTITY_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.delenv("DWP_DWAION_HOME_IDENTITY_PREVIOUS_KEY_ID", raising=False)
    monkeypatch.delenv(
        "DWP_DWAION_HOME_IDENTITY_PREVIOUS_SIGNING_SECRET", raising=False
    )
    monkeypatch.setenv("DWP_DWAION_HOME_TITLE_PROJECTION_READY", "true")
    monkeypatch.setenv("DWP_ENVIRONMENT", "test")
    home_widget_identity.home_identity_replay_store.cache_clear()


def _client(
    monkeypatch: pytest.MonkeyPatch,
    store: _Store,
    *,
    install_body_limit: bool = False,
) -> TestClient:
    monkeypatch.setattr(home_widget_api, "get_artifact_store", lambda: store)
    app = FastAPI()
    if install_body_limit:
        install_home_widget_body_limit(app)
    app.include_router(home_widget_api.router)
    return TestClient(app)


def _headers(
    *,
    body: bytes,
    permissions: str | None = None,
    path: str = "/internal/home/v1/widget-data:batch",
    key_id: str = "platform-dwaion-home-v1",
    signing_secret: str = SIGNING_SECRET,
) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    headers = {
        "X-DWP-Tenant-ID": "71",
        "X-DWP-User-ID": "82",
        "X-Correlation-ID": "home:decision-17",
        "X-DWP-Identity-Plane": "TENANT",
        "X-DWP-Permissions": permissions
        or "APP.ASK:VIEW,APP.DWAION_ARTIFACTS:VIEW",
        "X-DWP-Roles": "WORKSPACE_MEMBER",
        "X-DWP-Group-Refs": "team-a",
        "X-DWP-Current-Decision-Revision": "decision-17",
        "X-DWP-Current-Revalidate-At": (now + timedelta(minutes=5)).isoformat(),
        "X-DWP-Home-Deadline-At": (now + timedelta(seconds=5)).isoformat(),
        "Accept-Language": "ko-KR",
        "Content-Type": "application/json",
    }
    headers["X-DWP-Home-Assertion"] = _assertion(
        headers, path=path, body=body, key_id=key_id, signing_secret=signing_secret
    )
    return headers


def _assertion(
    headers: dict[str, str],
    *,
    path: str,
    body: bytes,
    key_id: str = "platform-dwaion-home-v1",
    signing_secret: str = SIGNING_SECRET,
) -> str:
    now = int(time.time())
    claims = {
        "v": 1,
        "kid": key_id,
        "iss": "dwp-platform-server",
        "aud": "dwp-agent-home",
        "sub": headers["X-DWP-User-ID"],
        "tid": headers["X-DWP-Tenant-ID"],
        "pid": headers.get("X-DWP-Person-Public-ID"),
        "cid": headers["X-Correlation-ID"],
        "traceparent": headers.get("traceparent"),
        "tracestate": headers.get("tracestate"),
        "ip": headers["X-DWP-Identity-Plane"],
        "htm": "POST",
        "htu": path,
        "permissions": sorted(headers.get("X-DWP-Permissions", "").split(",")),
        "roles": sorted(headers.get("X-DWP-Roles", "").split(",")),
        "groups": sorted(headers.get("X-DWP-Group-Refs", "").split(",")),
        "authorityRevision": headers["X-DWP-Current-Decision-Revision"],
        "authorityRevalidateAt": headers["X-DWP-Current-Revalidate-At"],
        "deadlineAt": headers["X-DWP-Home-Deadline-At"],
        "bodySha256": hashlib.sha256(body).hexdigest(),
        "iat": now,
        "nbf": now - 1,
        "exp": now + 4,
        "jti": str(uuid4()),
    }
    encoded_claims = _b64(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    )
    signed = f"dwp1.{encoded_claims}"
    signature = _b64(
        hmac.new(signing_secret.encode(), signed.encode(), hashlib.sha256).digest()
    )
    return f"{signed}.{signature}"


def _signed_claims(
    claims: dict[str, object], *, signing_secret: str = SIGNING_SECRET
) -> str:
    encoded_claims = _b64(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    )
    signed = f"dwp1.{encoded_claims}"
    signature = _b64(
        hmac.new(signing_secret.encode(), signed.encode(), hashlib.sha256).digest()
    )
    return f"{signed}.{signature}"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _body(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _request(*, item_limit: int = 3, definition_key: str = "dwaion.artifact") -> dict:
    return {
        "schemaVersion": 1,
        "widgets": [
            {
                "instanceId": str(uuid4()),
                "definitionKey": definition_key,
                "definitionVersion": home_widget_api.DEFINITION_VERSION,
                "definitionManifestHash": MANIFEST_HASH,
                "rendererBindingRevision": BINDING_REVISION,
                "configuration": {},
                "itemLimit": item_limit,
            }
        ],
    }


def _request_many(item_limits: list[int]) -> dict:
    return {
        "schemaVersion": 1,
        "widgets": [
            {
                "instanceId": str(uuid4()),
                "definitionKey": "dwaion.artifact",
                "definitionVersion": home_widget_api.DEFINITION_VERSION,
                "definitionManifestHash": MANIFEST_HASH,
                "rendererBindingRevision": BINDING_REVISION,
                "configuration": {},
                "itemLimit": item_limit,
            }
            for item_limit in item_limits
        ],
    }


def _post(
    client: TestClient,
    request: dict,
    *,
    permissions: str | None = None,
    path: str = "/internal/home/v1/widget-data:batch",
    extra_headers: dict[str, str] | None = None,
):
    raw = _body(request)
    headers = _headers(body=raw, permissions=permissions, path=path)
    headers.update(extra_headers or {})
    return client.post(path, headers=headers, content=raw)


def _artifact(title: str, *, minutes_ago: int) -> object:
    return SimpleNamespace(
        artifact_id=uuid4(),
        artifact_type=ArtifactType.DOCUMENT,
        state=ArtifactState.DRAFT,
        revision=3,
        content=SimpleNamespace(title=title, body="private body must never leave owner"),
        sources=[{"reference": "private-source"}],
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


def test_available_projection_is_recipient_bound_bounded_and_least_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store([_artifact("전략 초안", minutes_ago=1), _artifact("회의 메모", minutes_ago=2)])
    response = _post(_client(monkeypatch, store), _request(item_limit=1))

    assert response.status_code == 200
    body = response.json()
    assert body["tenantId"] == 71
    assert body["userId"] == 82
    assert body["authorityDecisionRevision"] == "decision-17"
    result = body["results"][0]
    assert result["state"] == "AVAILABLE"
    assert result["definitionManifestHash"] == MANIFEST_HASH
    assert result["rendererBindingRevision"] == BINDING_REVISION
    assert result["source"]["sourceKey"] == "DWAION_HOME"
    assert datetime.fromisoformat(result["source"]["expiresAt"]) <= datetime.fromisoformat(
        response.request.headers["X-DWP-Home-Deadline-At"]
    )
    assert result["source"]["resultVersion"].startswith("v1:")
    assert result["payload"]["visibleCount"] == 2
    assert len(result["payload"]["items"]) == 1
    assert result["payload"]["items"][0].keys() == {
        "artifactId", "title", "artifactType", "state", "revision", "updatedAt"
    }
    assert result["actions"] == [
        {
            "actionId": "open-source",
            "labelKey": "home.action.openSource",
            "kind": "SOURCE_ROUTE",
            "sourceRoute": "/dwaion/artifacts",
            "commandKey": None,
            "expectedResultVersion": None,
            "requiresConfirmation": False,
        }
    ]
    assert "private body" not in response.text
    assert "private-source" not in response.text
    assert len(store.identities) == 1
    identity = store.identities[0]
    assert identity.tenant_id == 71
    assert identity.user_id == "82"
    assert identity.permissions == frozenset(
        {"APP.ASK:VIEW", "APP.DWAION_ARTIFACTS:VIEW"}
    )


def test_empty_projection_keeps_only_the_safe_source_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _post(_client(monkeypatch, _Store()), _request())

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["state"] == "EMPTY"
    assert result["payload"] == {}
    assert result["actions"][0]["sourceRoute"] == "/dwaion/artifacts"


def test_missing_owner_authority_is_forbidden_without_reading_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store([_artifact("must remain hidden", minutes_ago=1)])
    response = _post(
        _client(monkeypatch, store),
        _request(),
        permissions="APP.DWAION_ARTIFACTS:VIEW",
    )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["state"] == "FORBIDDEN"
    assert result["payload"] == {}
    assert result["actions"] == []
    assert store.identities == []


def test_storage_failure_is_retryable_unavailable_without_data_or_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _post(_client(monkeypatch, _Store(unavailable=True)), _request())

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["state"] == "UNAVAILABLE"
    assert result["source"]["retryable"] is True
    assert result["payload"] == {}
    assert result["actions"] == []


def test_projection_activation_gate_fails_closed_without_storage_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_DWAION_HOME_TITLE_PROJECTION_READY", "false")
    store = _Store([_artifact("hidden", minutes_ago=1)])
    response = _post(_client(monkeypatch, store), _request())

    assert response.status_code == 200
    assert response.json()["results"][0]["state"] == "UNAVAILABLE"
    assert store.identities == []


def test_transport_rejects_ambient_authority_wrong_owner_and_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, _Store())
    ambient = _post(
        client, _request(), extra_headers={"Authorization": "Bearer browser-token"}
    )
    wrong_owner = _post(client, _request(definition_key="space.change-feed"))
    command = _post(
        client,
        {},
        path="/internal/home/v1/widget-actions:execute",
    )

    assert ambient.status_code == 403
    assert wrong_owner.status_code == 400
    assert command.status_code == 422


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("definitionVersion", "1.0.1"),
        ("definitionManifestHash", "b" * 64),
    ),
)
def test_provider_rejects_unapproved_immutable_definition_bindings(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    request = _request()
    request["widgets"][0][field] = value

    response = _post(_client(monkeypatch, _Store()), request)

    assert response.status_code == 400
    assert response.json()["detail"]["reasonCode"] == (
        "HOME_PROVIDER_DEFINITION_NOT_SUPPORTED"
    )


def test_provider_requires_a_canonical_catalog_binding_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    request["widgets"][0]["rendererBindingRevision"] = "unapproved-binding"

    response = _post(_client(monkeypatch, _Store()), request)

    assert response.status_code == 422


def test_home_profile_rejects_gateway_assertion_and_service_token_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    raw = _body(request)
    home_headers = _headers(body=raw)
    gateway_only = {
        key: value
        for key, value in home_headers.items()
        if key != "X-DWP-Home-Assertion"
    }
    gateway_only["X-DWP-Delegated-Identity"] = home_headers["X-DWP-Home-Assertion"]
    client = _client(monkeypatch, _Store())

    gateway = client.post(
        "/internal/home/v1/widget-data:batch", headers=gateway_only, content=raw
    )
    service_token = _post(
        client,
        _request(),
        extra_headers={
            "X-DWP-Service-Identity": "dwp-platform-server",
            "X-DWP-Service-Token": "purpose-token-must-not-be-accepted",
        },
    )

    assert gateway.status_code == 401
    assert service_token.status_code == 403


def test_home_profile_rejects_a_validly_signed_meeting_dwp1_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    raw = _body(request)
    headers = _headers(body=raw)
    now = int(time.time())
    # This is the canonical Meeting workload claim shape, signed with the same key to
    # prove that claim-profile separation is enforced independently of the MAC.
    headers["X-DWP-Home-Assertion"] = _signed_claims(
        {
            "v": 1,
            "kid": "meeting-workload-v1",
            "method": "POST",
            "path": "/internal/home/v1/widget-data:batch",
            "tenantId": 71,
            "meetingId": str(uuid4()),
            "runId": str(uuid4()),
            "iat": now,
            "exp": now + 4,
            "jti": str(uuid4()),
            "bodySha256": hashlib.sha256(raw).hexdigest(),
        }
    )
    response = _client(monkeypatch, _Store()).post(
        "/internal/home/v1/widget-data:batch", headers=headers, content=raw
    )

    assert response.status_code == 401
    assert response.json()["detail"]["reasonCode"] == (
        "HOME_PROVIDER_DELEGATED_IDENTITY_INVALID"
    )


def test_home_profile_bounds_assertion_and_fails_closed_for_invalid_key_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    raw = _body(request)
    oversized = _headers(body=raw)
    oversized["X-DWP-Home-Assertion"] = "dwp1." + "a" * 20_000 + ".a"
    client = _client(monkeypatch, _Store())
    rejected = client.post(
        "/internal/home/v1/widget-data:batch", headers=oversized, content=raw
    )
    monkeypatch.setenv("DWP_DWAION_HOME_IDENTITY_KEY_ID", "bad key id")
    misconfigured = _post(client, _request())

    assert rejected.status_code == 401
    assert misconfigured.status_code == 503
    assert misconfigured.json()["detail"]["reasonCode"] == (
        "HOME_PROVIDER_DELEGATED_IDENTITY_NOT_CONFIGURED"
    )


def test_previous_signing_key_is_accepted_only_during_a_valid_rotation_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_key_id = "platform-dwaion-home-v0"
    previous_secret = "previous-dwaion-home-signing-secret-0001"
    monkeypatch.setenv("DWP_DWAION_HOME_IDENTITY_PREVIOUS_KEY_ID", previous_key_id)
    monkeypatch.setenv(
        "DWP_DWAION_HOME_IDENTITY_PREVIOUS_SIGNING_SECRET", previous_secret
    )
    request = _request()
    raw = _body(request)
    headers = _headers(
        body=raw,
        key_id=previous_key_id,
        signing_secret=previous_secret,
    )
    client = _client(monkeypatch, _Store())

    accepted = client.post(
        "/internal/home/v1/widget-data:batch", headers=headers, content=raw
    )
    monkeypatch.setenv(
        "DWP_DWAION_HOME_IDENTITY_PREVIOUS_SIGNING_SECRET", SIGNING_SECRET
    )
    unsafe_rotation = _post(client, _request())

    assert accepted.status_code == 200
    assert unsafe_rotation.status_code == 503
    assert unsafe_rotation.json()["detail"]["reasonCode"] == (
        "HOME_PROVIDER_DELEGATED_IDENTITY_NOT_CONFIGURED"
    )


def test_transport_rejects_declared_oversized_body_before_identity_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _client(
        monkeypatch, _Store(), install_body_limit=True
    ).post(
        "/internal/home/v1/widget-data:batch",
        headers={"Content-Length": str(HOME_WIDGET_REQUEST_LIMIT_BYTES + 1)},
        content=b"{}",
    )

    assert response.status_code == 413
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["detail"]["reasonCode"] == (
        "HOME_PROVIDER_BODY_OUT_OF_BOUNDS"
    )


def test_transport_stops_streaming_body_at_the_bound_before_downstream_buffering() -> None:
    downstream_called = False
    sent: list[dict[str, object]] = []
    chunks = iter(
        (
            {
                "type": "http.request",
                "body": b"a" * (HOME_WIDGET_REQUEST_LIMIT_BYTES // 2 + 1),
                "more_body": True,
            },
            {
                "type": "http.request",
                "body": b"b" * (HOME_WIDGET_REQUEST_LIMIT_BYTES // 2 + 1),
                "more_body": False,
            },
        )
    )

    async def downstream(scope, receive, send) -> None:
        nonlocal downstream_called
        downstream_called = True

    async def receive() -> dict[str, object]:
        return next(chunks)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    asyncio.run(
        HomeWidgetBodyLimitMiddleware(downstream)(
            {
                "type": "http",
                "method": "POST",
                "path": "/internal/home/v1/widget-data:batch",
                "headers": [],
            },
            receive,
            send,
        )
    )

    assert downstream_called is False
    assert sent[0]["status"] == 413


def test_transport_rejects_tampered_recipient_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    raw = _body(request)
    headers = _headers(body=raw)
    headers["X-DWP-User-ID"] = "83"
    response = _client(monkeypatch, _Store()).post(
        "/internal/home/v1/widget-data:batch",
        headers=headers,
        content=raw,
    )

    assert response.status_code == 401
    assert response.json()["detail"]["reasonCode"] == (
        "HOME_PROVIDER_DELEGATED_IDENTITY_INVALID"
    )


def test_transport_binds_trace_context_without_accepting_unsigned_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    raw = _body(request)
    headers = _headers(body=raw)
    headers["traceparent"] = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"

    response = _client(monkeypatch, _Store()).post(
        "/internal/home/v1/widget-data:batch", headers=headers, content=raw
    )

    assert response.status_code == 401
    assert response.json()["detail"]["reasonCode"] == (
        "HOME_PROVIDER_DELEGATED_IDENTITY_INVALID"
    )


def test_transport_accepts_trace_context_only_when_it_is_signed_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    raw = _body(request)
    headers = _headers(body=raw)
    headers["traceparent"] = "00-" + "3" * 32 + "-" + "4" * 16 + "-01"
    headers["tracestate"] = "vendor=value"
    headers["X-DWP-Home-Assertion"] = _assertion(
        headers,
        path="/internal/home/v1/widget-data:batch",
        body=raw,
    )

    response = _client(monkeypatch, _Store()).post(
        "/internal/home/v1/widget-data:batch", headers=headers, content=raw
    )

    assert response.status_code == 200


def test_transport_binds_exact_body_and_rejects_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, _Store())
    original = _request(item_limit=1)
    raw = _body(original)
    headers = _headers(body=raw)
    first = client.post(
        "/internal/home/v1/widget-data:batch", headers=headers, content=raw
    )
    replay = client.post(
        "/internal/home/v1/widget-data:batch", headers=headers, content=raw
    )
    changed = _body({**original, "schemaVersion": 2})
    tampered = client.post(
        "/internal/home/v1/widget-data:batch", headers=_headers(body=raw), content=changed
    )

    assert first.status_code == 200
    assert replay.status_code == 401
    assert tampered.status_code == 401


def test_replay_store_outage_is_unavailable_not_invalid_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable():
        raise home_widget_identity.HomeIdentityReplayUnavailable("database unavailable")

    monkeypatch.setattr(home_widget_api, "get_artifact_store", lambda: _Store())
    monkeypatch.setattr(
        "dwp_agent.home_widget_security.home_identity_replay_store", unavailable
    )
    app = FastAPI()
    app.include_router(home_widget_api.router)
    response = _post(TestClient(app), _request())

    assert response.status_code == 503
    assert response.json()["detail"]["reasonCode"] == (
        "HOME_PROVIDER_REPLAY_PROTECTION_UNAVAILABLE"
    )


def test_batch_reads_once_and_scopes_partial_state_to_each_requested_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = [_artifact(f"draft-{index}", minutes_ago=index) for index in range(50)]
    store = _Store(artifacts)
    store.unreadable_positions = {49}
    request = _request_many([1, *range(1, 51), *range(1, 50)])
    response = _post(_client(monkeypatch, store), request)

    assert response.status_code == 200
    assert store.limits == [50]
    results = response.json()["results"]
    assert len(results) == 100
    assert results[0]["state"] == "AVAILABLE"
    assert len(results[0]["payload"]["items"]) == 1
    assert results[50]["state"] == "PARTIAL"
    assert results[51]["state"] == "AVAILABLE"


def test_transport_fails_closed_for_unconfigured_or_expired_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, _Store())
    request = _request()
    raw = _body(request)
    expired_headers = _headers(body=raw)
    expired_headers["X-DWP-Home-Deadline-At"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    expired = client.post(
        "/internal/home/v1/widget-data:batch",
        headers=expired_headers,
        content=raw,
    )
    monkeypatch.delenv("DWP_DWAION_HOME_IDENTITY_SIGNING_SECRET")
    unconfigured = _post(client, _request())

    assert expired.status_code == 401
    assert unconfigured.status_code == 503
