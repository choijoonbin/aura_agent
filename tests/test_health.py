import asyncio
import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from main import app


SERVICE_TOKEN = "test-gateway-service-token"


@pytest.fixture(autouse=True)
def configured_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)


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
    }


def test_openapi_contains_system_and_plan_preview_api() -> None:
    response = asyncio.run(get("/openapi.json"))

    assert response.status_code == 200
    assert set(response.json()["paths"]) == {"/health", "/v1/plans/preview"}
    parameters = response.json()["paths"]["/v1/plans/preview"]["post"]["parameters"]
    assert "X-DWP-Service-Token" not in {parameter["name"] for parameter in parameters}


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
