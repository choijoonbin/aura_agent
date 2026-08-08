import asyncio

from httpx import ASGITransport, AsyncClient

from main import app


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
        "version": "0.1.0",
    }


def test_openapi_contains_system_and_plan_preview_api() -> None:
    response = asyncio.run(get("/openapi.json"))

    assert response.status_code == 200
    assert set(response.json()["paths"]) == {"/health", "/v1/plans/preview"}


def test_plan_preview_is_deterministic_and_never_mutates() -> None:
    payload = {
        "requestId": "request-1042",
        "intent": "Request remote work next Friday",
        "action": "flexible work request",
        "target": "employee-services/flexible-work",
        "sourceReferences": ["policy-flex-3.2", "guide-remote-2.1"],
    }
    headers = {
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

    assert first.status_code == 200
    assert first.json() == second.json()
    assert changed.status_code == 200
    assert changed.json()["data"]["runId"] != first.json()["data"]["runId"]
    assert first.json()["success"] is True
    plan = first.json()["data"]
    assert plan["riskTier"] == "L2"
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
                "X-DWP-User-ID": "1",
                "X-DWP-Tenant-ID": "1",
                "X-Correlation-ID": "correlation-1",
            },
        )
    )

    assert response.status_code == 422
