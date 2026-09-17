from fastapi.testclient import TestClient

from dwp_agent.main import app


PATH = "/internal/v1/proposal-handoffs/00000000-0000-4000-8000-000000000011/observations"
BODY = {
    "commandId": "00000000-0000-4000-8000-000000000012",
    "expectedVersion": 1,
    "state": "HANDED_OFF",
}


def _identity_headers() -> dict[str, str]:
    return {
        "X-DWP-Workflow-Worker-Token": "worker-secret",
        "X-DWP-Service-Token": "service-secret",
        "X-DWP-Tenant-ID": "42",
        "X-DWP-User-ID": "99",
        "X-Correlation-ID": "correlation-1",
        "X-DWP-Auth-Session-ID": "session-1",
        "X-DWP-Identity-Plane": "TENANT",
        "X-DWP-Access-Mode": "NORMAL",
        "X-DWP-Roles": "EMPLOYEE",
        "X-DWP-Permissions": "APP.ASK:VIEW",
    }


def test_internal_observation_requires_worker_and_gateway_service_identities(monkeypatch) -> None:
    monkeypatch.setenv("DWP_WORKFLOW_WORKER_TOKEN", "worker-secret")
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", "service-secret")
    monkeypatch.delenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", raising=False)
    client = TestClient(app)
    headers = _identity_headers()

    no_service = dict(headers)
    no_service.pop("X-DWP-Service-Token")
    assert client.post(PATH, json=BODY, headers=no_service).status_code == 401

    no_worker = dict(headers)
    no_worker.pop("X-DWP-Workflow-Worker-Token")
    assert client.post(PATH, json=BODY, headers=no_worker).status_code == 401

    # Both identities cross the transport boundary; storage is intentionally
    # unavailable in this unit test and therefore fails later with 503.
    assert client.post(PATH, json=BODY, headers=headers).status_code == 503
