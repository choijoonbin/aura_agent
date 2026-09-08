import base64
import hashlib
import hmac
import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from dwp_agent import main as main_module
from dwp_agent.policy import SafetyControls
from dwp_agent.stream_runtime import shutdown_ask_stream_pool

from test_selected_work_context import AUTHORIZATION, IDENTITY, selected_request
from test_selected_work_runtime import runtime_with_owner, setup  # noqa: F401


SECRET = "selected-work-test-signing-secret-only"


def signed_headers(path, *, method="POST", permissions=IDENTITY.permissions):
    headers = {
        "X-DWP-Service-Token": "selected-work-test-service",
        "X-DWP-User-ID": IDENTITY.user_id, "X-DWP-Tenant-ID": IDENTITY.tenant_id,
        "X-Correlation-ID": IDENTITY.correlation_id, "X-DWP-Roles": "WORKSPACE_MEMBER",
        "X-DWP-Permissions": ",".join(permissions), "X-DWP-Identity-Plane": "TENANT",
        "X-DWP-Auth-Session-ID": AUTHORIZATION.session_family_id,
        "Cookie": AUTHORIZATION.cookie_header,
    }
    now = int(time.time())
    claims = {
        "iss": "dwp-gateway", "aud": "dwp-agent", "sub": IDENTITY.user_id,
        "tid": IDENTITY.tenant_id, "cid": IDENTITY.correlation_id,
        "htm": method, "htu": path, "roles": ["WORKSPACE_MEMBER"],
        "permissions": sorted(permissions), "ip": "TENANT", "sid": AUTHORIZATION.session_family_id,
        "iat": now, "nbf": now - 1, "exp": now + 15, "jti": str(uuid4()),
    }
    def encode(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")
    protected = {"alg": "HS256", "kid": "gateway-agent-v1", "typ": "dwp-identity+jwt"}
    body = ".".join(encode(json.dumps(value, separators=(",", ":")).encode()) for value in (protected, claims))
    headers["X-DWP-Delegated-Identity"] = body + "." + encode(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest())
    return headers


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", "selected-work-test-service")
    monkeypatch.setenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V4_ENABLED", "false")
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V5_ENABLED", "false")
    monkeypatch.setattr(main_module, "require_operational_delivery", lambda **_: None)
    monkeypatch.setattr(main_module, "runtime_safety_controls", lambda *_: SafetyControls())
    runtime, model, state = runtime_with_owner("APPROVAL_TASK")
    main_module.app.dependency_overrides[main_module.get_ask_runtime] = lambda: runtime
    monkeypatch.setattr(main_module, "get_conversation_store", lambda: runtime.conversation_store)
    yield TestClient(main_module.app), model, state
    main_module.app.dependency_overrides.pop(main_module.get_ask_runtime, None)
    shutdown_ask_stream_pool()


def test_signed_public_stream_to_selected_owner_and_persisted_conversation(api):
    client, model, state = api
    response = client.post(
        "/v1/ask/stream", headers=signed_headers("/v1/ask/stream"),
        json=selected_request("APPROVAL_TASK").model_dump(mode="json", by_alias=True),
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert "event: progress" in response.text and "event: result" in response.text
    frame = response.text.split("event: result\ndata: ")[1].split("\n\n")[0]
    result = json.loads(frame)["data"]
    assert result["state"] == "COMPLETED" and result["conversationId"]
    assert model.calls == 1 and len(state["reads"]) == 4
    path = f"/v1/conversations/{result['conversationId']}"
    detail = client.get(path, params={"agentKey": "DWP_APPROVAL_EXPERT"},
                        headers=signed_headers(path, method="GET"))
    assert detail.status_code == 200
    assert detail.json()["data"]["messages"][-1]["content"] == result["answer"]
    assert detail.json()["data"]["messages"][-1]["agentKey"] == "DWP_APPROVAL_EXPERT"
    mismatch = client.get(path, params={"agentKey": "DWP_ASSISTANT"},
                          headers=signed_headers(path, method="GET"))
    assert mismatch.status_code == 404
    assert result["answer"] not in mismatch.text


def test_selected_public_route_rejects_forged_identity_before_source_or_model(api):
    client, model, state = api
    headers = signed_headers("/v1/ask/stream")
    headers["X-DWP-User-ID"] = "8"
    response = client.post("/v1/ask/stream", headers=headers,
        json=selected_request("APPROVAL_TASK").model_dump(mode="json", by_alias=True))
    assert response.status_code == 401
    assert state["reads"] == [] and model.calls == 0


def test_selected_public_route_requires_ask_permission_even_with_valid_signature(api):
    client, model, state = api
    response = client.post("/v1/ask/stream", headers=signed_headers(
        "/v1/ask/stream", permissions=("APP.APPROVALS:VIEW",)),
        json=selected_request("APPROVAL_TASK").model_dump(mode="json", by_alias=True))
    assert response.status_code == 403
    assert state["reads"] == [] and model.calls == 0
