import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import dwp_agent.system_api as system_api_module
from dwp_agent.main import app
from dwp_agent.registry import resolve_agent


SERVICE_TOKEN = "test-gateway-service-token"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def configured_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("DWP_AGENT_REGISTRY_MODE", "optional")
    monkeypatch.delenv("SERVICE_PLATFORM_URL", raising=False)
    monkeypatch.delenv("DWP_PLATFORM_RUNTIME_SERVICE_TOKEN", raising=False)


async def get(path: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def post(path: str, *, json: dict, headers: dict[str, str] | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=json, headers=headers)


def test_health() -> None:
    response = asyncio.run(get("/health"))

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "DWP Agent Runtime",
        "version": "0.2.0",
        "components": {"database": "DISABLED"},
    }


def test_liveness_and_local_readiness_are_distinct() -> None:
    live = asyncio.run(get("/livez"))
    ready = asyncio.run(get("/readyz"))

    assert live.status_code == 200
    assert live.json()["status"] == "alive"
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["components"] == {"database": "DISABLED"}


def test_readiness_reports_a_runtime_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_api_module, "probe_database_status", lambda: "FAILED")

    live = asyncio.run(get("/livez"))
    ready = asyncio.run(get("/readyz"))
    health = asyncio.run(get("/health"))

    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready"
    assert health.status_code == 503
    assert health.json()["status"] == "unavailable"


def test_public_openapi_contains_system_and_plan_preview_api_only() -> None:
    response = asyncio.run(get("/openapi.json"))

    assert response.status_code == 200
    assert set(response.json()["paths"]) == {
        "/health",
        "/livez",
        "/readyz",
        "/v1/ask",
        "/v1/ask/stream",
        "/v1/plans/preview",
        "/v1/proposals",
        "/v1/proposals/analyze",
        "/v1/proposals/preferences",
        "/v1/proposals/clear",
        "/v1/proposals/{proposal_id}/decisions",
        "/v1/question-launches",
        "/v1/question-launches/consume",
        "/v1/conversations",
        "/v1/conversations/{conversation_id}",
        "/v1/runs",
        "/v1/runs/{run_id}/feedback",
        "/v1/voice/transcriptions",
        "/v1/voice/speech",
        "/v1/actions",
        "/v1/actions/{action_key}/preview",
        "/v1/admin/overview",
        "/v1/admin/proposals",
        "/v1/admin/retention",
        "/v1/admin/retention/bootstrap",
        "/v1/admin/sources",
        "/v1/admin/sources/bootstrap",
        "/v1/admin/sources/{source_key}",
        "/v1/admin/actions",
        "/v1/admin/actions/bootstrap",
        "/v1/admin/actions/{action_key}",
        "/v1/admin/safety",
        "/v1/admin/safety/bootstrap",
        "/v1/admin/evaluations",
        "/v1/admin/evaluations/{evaluation_set_id}",
        "/v1/admin/evaluations/{evaluation_set_id}/cases",
        "/v1/admin/evaluations/{evaluation_set_id}/lifecycle",
        "/v1/admin/evaluations/{evaluation_set_id}/runs",
        "/v1/admin/evaluations/{evaluation_set_id}/runs/{evaluation_run_id}",
        "/v1/admin/evaluations/{evaluation_set_id}/runs/{evaluation_run_id}/export",
        "/v1/admin/audit",
        "/v1/admin/audit/export",
        "/v1/admin/gates",
        "/v1/admin/gates/bootstrap",
        "/v1/admin/gates/{gate_key}",
        "/v1/admin/gates/{gate_key}/evidence",
        "/v1/admin/gates/{gate_key}/validation",
        "/v1/admin/gates/{gate_key}/decision",
    }
    parameters = response.json()["paths"]["/v1/plans/preview"]["post"]["parameters"]
    assert "X-DWP-Service-Token" not in {parameter["name"] for parameter in parameters}
    assert not any(path.startswith("/internal/") for path in response.json()["paths"])


def test_openapi_snapshot_matches_runtime_contract() -> None:
    snapshot = json.loads(
        (ROOT / "contracts" / "openapi" / "agent-public.json").read_text(
            encoding="utf-8"
        )
    )

    assert snapshot == app.openapi()


def test_plan_preview_is_deterministic_and_never_mutates() -> None:
    payload = {
        "requestId": "request-1042",
        "intent": "Request remote work next Friday",
        "action": "flexible work request",
        "target": "employee-services/flexible-work",
        "sourceReferences": ["policy-flex-3.2", "guide-remote-2.1"],
    }
    headers = {
        "X-DWP-Service-Token": SERVICE_TOKEN,
        "X-DWP-User-ID": "1",
        "X-DWP-Tenant-ID": "1",
        "X-DWP-Roles": "EMPLOYEE",
        "X-Correlation-ID": "correlation-1042",
        "X-DWP-Permissions": "APP.ASK:VIEW",
        "X-DWP-Identity-Plane": "TENANT",
    }

    first = asyncio.run(post("/v1/plans/preview", json=payload, headers=headers))
    second = asyncio.run(post("/v1/plans/preview", json=payload, headers=headers))
    changed = asyncio.run(
        post(
            "/v1/plans/preview",
            json={**payload, "intent": "Request remote work on Monday"},
            headers=headers,
        )
    )
    changed_role = asyncio.run(
        post(
            "/v1/plans/preview",
            json=payload,
            headers={**headers, "X-DWP-Roles": "EMPLOYEE,APPROVER"},
        )
    )

    assert first.status_code == 200
    assert first.json() == second.json()
    assert changed.status_code == 200
    assert changed.json()["data"]["runId"] != first.json()["data"]["runId"]
    assert changed_role.status_code == 200
    assert changed_role.json()["data"]["planHash"] != first.json()["data"]["planHash"]
    assert first.json()["success"] is True
    plan = first.json()["data"]
    assert plan["riskTier"] == "L2"
    assert len(plan["planHash"]) == 64
    assert plan["correlationId"] == "correlation-1042"
    assert plan["approvalRequired"] is True
    assert plan["mutationAllowed"] is False
    assert plan["referenceMode"] is True
    assert plan["agentRegistry"] == {
        "entryKey": "REFERENCE_PLANNER",
        "revision": 0,
        "artifactVersion": "reference",
        "riskTier": "MEDIUM",
        "resolution": "REFERENCE_FALLBACK",
    }
    assert [step["tool"] for step in plan["steps"]] == [
        "policy.check",
        "tool.preview",
        "workflow.human-approval",
    ]
    assert not ({"reasoning", "chainOfThought", "prompt"} & set(plan))


def test_plan_preview_requires_gateway_verified_identity() -> None:
    response = asyncio.run(
        post(
            "/v1/plans/preview",
            json={
                "requestId": "request-1",
                "intent": "Preview a request",
                "action": "request",
                "target": "service/request",
            },
            headers={"X-DWP-Service-Token": SERVICE_TOKEN},
        )
    )

    assert response.status_code == 422


def test_plan_preview_requires_dwaion_permission() -> None:
    response = asyncio.run(
        post(
            "/v1/plans/preview",
            json={
                "requestId": "request-permission",
                "intent": "Preview a request",
                "action": "request",
                "target": "service/request",
            },
            headers={
                "X-DWP-Service-Token": SERVICE_TOKEN,
                "X-DWP-User-ID": "1",
                "X-DWP-Tenant-ID": "1",
                "X-Correlation-ID": "correlation-permission",
            },
        )
    )

    assert response.status_code == 403


def test_plan_preview_rejects_unknown_contract_fields() -> None:
    response = asyncio.run(
        post(
            "/v1/plans/preview",
            json={
                "requestId": "request-1",
                "intent": "Preview a request",
                "action": "request",
                "target": "service/request",
                "executeNow": True,
            },
            headers={
                "X-DWP-Service-Token": SERVICE_TOKEN,
                "X-DWP-User-ID": "1",
                "X-DWP-Tenant-ID": "1",
                "X-Correlation-ID": "correlation-1",
            },
        )
    )

    assert response.status_code == 422


def test_plan_preview_rejects_missing_or_invalid_service_identity() -> None:
    payload = {
        "requestId": "request-1",
        "intent": "Preview a request",
        "action": "request",
        "target": "service/request",
    }
    identity_headers = {
        "X-DWP-User-ID": "1",
        "X-DWP-Tenant-ID": "1",
        "X-Correlation-ID": "correlation-1",
    }

    missing = asyncio.run(post("/v1/plans/preview", json=payload, headers=identity_headers))
    invalid = asyncio.run(
        post(
            "/v1/plans/preview",
            json=payload,
            headers={**identity_headers, "X-DWP-Service-Token": "spoofed"},
        )
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401


def test_plan_preview_fails_closed_without_server_service_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DWP_AGENT_SERVICE_TOKEN")

    response = asyncio.run(
        post(
            "/v1/plans/preview",
            json={
                "requestId": "request-1",
                "intent": "Preview a request",
                "action": "request",
                "target": "service/request",
            },
            headers={
                "X-DWP-Service-Token": SERVICE_TOKEN,
                "X-DWP-User-ID": "1",
                "X-DWP-Tenant-ID": "1",
                "X-Correlation-ID": "correlation-1",
            },
        )
    )

    assert response.status_code == 503


def test_audit_event_excludes_raw_intent_sources_and_service_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    intent = "Confidential acquisition planning"
    source = "secret-board-document"
    caplog.set_level(logging.INFO, logger="uvicorn.error.dwp.audit")

    response = asyncio.run(
        post(
            "/v1/plans/preview",
            json={
                "requestId": "request-audit",
                "intent": intent,
                "action": "document review",
                "target": "knowledge/review",
                "sourceReferences": [source],
            },
            headers={
                "X-DWP-Service-Token": SERVICE_TOKEN,
                "X-DWP-User-ID": "user-7",
                "X-DWP-Tenant-ID": "tenant-1",
                "X-DWP-Roles": "EMPLOYEE,REVIEWER",
                "X-Correlation-ID": "correlation-audit",
                "X-DWP-Permissions": "APP.ASK:VIEW",
                "X-DWP-Identity-Plane": "TENANT",
            },
        )
    )

    assert response.status_code == 200
    event = json.loads(caplog.records[-1].message)
    assert event["type"] == "agent.plan.previewed"
    assert event["correlationId"] == "correlation-audit"
    assert event["data"]["planHash"] == response.json()["data"]["planHash"]
    assert event["data"]["roleCount"] == 2
    assert event["data"]["sourceCount"] == 1
    assert intent not in caplog.text
    assert source not in caplog.text
    assert SERVICE_TOKEN not in caplog.text


def test_agent_registry_resolves_active_tenant_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERVICE_PLATFORM_URL", "http://platform.local")
    monkeypatch.setenv("DWP_PLATFORM_RUNTIME_SERVICE_TOKEN", "platform-token")

    def get_registry(url: str, **kwargs) -> httpx.Response:
        assert url.endswith("/v1/catalog/registry-entries/AGENT/REFERENCE_PLANNER")
        assert kwargs["headers"]["X-DWP-Service-Token"] == "platform-token"
        assert kwargs["headers"]["X-DWP-Tenant-ID"] == "7"
        return httpx.Response(
            200,
            json={
                "data": {
                    "registryType": "AGENT",
                    "entryKey": "REFERENCE_PLANNER",
                    "revision": 3,
                    "artifactVersion": "2.1.0",
                    "riskTier": "HIGH",
                }
            },
        )

    monkeypatch.setattr("dwp_agent.registry.httpx.get", get_registry)

    resolution = resolve_agent(
        "reference_planner",
        tenant_id="7",
        user_id="11",
        correlation_id="corr-registry",
    )

    assert resolution.model_dump(mode="json", by_alias=True) == {
        "entryKey": "REFERENCE_PLANNER",
        "revision": 3,
        "artifactVersion": "2.1.0",
        "riskTier": "HIGH",
        "resolution": "ACTIVE",
    }


def test_enforced_registry_mode_fails_closed_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DWP_AGENT_REGISTRY_MODE", "enforced")

    response = asyncio.run(
        post(
            "/v1/plans/preview",
            json={
                "requestId": "request-registry",
                "intent": "Preview a request",
                "action": "request",
                "target": "service/request",
                "agentKey": "REFERENCE_PLANNER",
            },
            headers={
                "X-DWP-Service-Token": SERVICE_TOKEN,
                "X-DWP-User-ID": "1",
                "X-DWP-Tenant-ID": "1",
                "X-Correlation-ID": "correlation-registry",
                "X-DWP-Permissions": "APP.ASK:VIEW",
                "X-DWP-Identity-Plane": "TENANT",
            },
        )
    )

    assert response.status_code == 503
