import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import dwp_agent.operational_gate_api as operational_gate_api_module
from dwp_agent.main import app
from dwp_agent.operational_gate_contracts import (
    ConfigureOperationalGateRequest,
    DecideOperationalGateRequest,
    GateCategory,
    GateApprovalEligibilityReason,
    GateDecision,
    GateEnvironment,
    GateEvidenceType,
    GateStatus,
    OperationalGateDetail,
    OperationalGateApprovalEligibility,
    OperationalGateKey,
    OperationalGateOption,
    OperationalGatePortfolio,
    OperationalGateSummary,
)


SERVICE_TOKEN = "test-gateway-service-token"


@pytest.fixture(autouse=True)
def configured_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)


class FakeOperationalGateStore:
    def __init__(self) -> None:
        self.gate = _gate()
        self.configured_by: str | None = None
        self.decided_by: str | None = None

    def portfolio(self, *, tenant_id: str, actor_user_id: str, environment: GateEnvironment):
        assert (tenant_id, actor_user_id, environment) == ("1", "7", GateEnvironment.PRODUCTION)
        return OperationalGatePortfolio(
            environment=environment,
            total_count=1,
            required_count=1,
            approved_count=0,
            ready_for_approval_count=1,
            blocked_count=0,
            expired_count=0,
            completion_percent=0,
            delivery_ready=False,
            gates=[self.gate],
        )

    def configure(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        environment: GateEnvironment,
        gate_key: OperationalGateKey,
        request: ConfigureOperationalGateRequest,
    ) -> OperationalGateDetail:
        assert (tenant_id, correlation_id, environment, gate_key) == (
            "1",
            "corr-1",
            GateEnvironment.PRODUCTION,
            OperationalGateKey.MODEL_CREDENTIALS,
        )
        assert request.selected_option == "MANAGED_IDENTITY"
        self.configured_by = actor_user_id
        return _detail(self.gate)

    def decide(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        environment: GateEnvironment,
        gate_key: OperationalGateKey,
        request: DecideOperationalGateRequest,
    ) -> OperationalGateDetail:
        assert request.decision == GateDecision.APPROVE
        self.decided_by = actor_user_id
        return _detail(self.gate)


async def request(
    method: str,
    path: str,
    *,
    permissions: str,
    json: dict | None = None,
) -> httpx.Response:
    transport = ASGITransport(app=app)
    headers = {
        "X-DWP-Service-Token": SERVICE_TOKEN,
        "X-DWP-User-ID": "7",
        "X-DWP-Tenant-ID": "1",
        "X-Correlation-ID": "corr-1",
        "X-DWP-Permissions": permissions,
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, json=json)


def test_operational_gates_reject_legacy_aggregate_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeOperationalGateStore()
    monkeypatch.setattr(
        operational_gate_api_module, "get_operational_gate_store", lambda: store
    )

    denied = asyncio.run(
        request("GET", "/v1/admin/gates", permissions="ADMIN.DWAION:MANAGE")
    )
    allowed = asyncio.run(
        request("GET", "/v1/admin/gates", permissions="ADMIN.DWAION_GATES:VIEW")
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["data"]["totalCount"] == 1


def test_gate_configuration_and_approval_use_separate_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeOperationalGateStore()
    monkeypatch.setattr(
        operational_gate_api_module, "get_operational_gate_store", lambda: store
    )
    base = "/v1/admin/gates/MODEL_CREDENTIALS"
    configured = asyncio.run(
        request(
            "PATCH",
            base,
            permissions="ADMIN.DWAION_GATES:UPDATE",
            json={
                "selectedOption": "MANAGED_IDENTITY",
                "ownerUserId": "cloud-security",
                "configurationRef": "kv://tenant/model-identity",
                "expectedVersion": 3,
                "changeReason": "Configure the production model identity.",
            },
        )
    )
    denied_decision = asyncio.run(
        request(
            "POST",
            f"{base}/decision",
            permissions="ADMIN.DWAION_GATES:UPDATE",
            json={
                "decision": "APPROVE",
                "validDays": 365,
                "expectedVersion": 3,
                "changeReason": "Approve independently after evidence review.",
            },
        )
    )
    approved = asyncio.run(
        request(
            "POST",
            f"{base}/decision",
            permissions="ADMIN.DWAION_GATES:APPROVE",
            json={
                "decision": "APPROVE",
                "validDays": 365,
                "expectedVersion": 3,
                "changeReason": "Approve independently after evidence review.",
            },
        )
    )

    assert configured.status_code == 200
    assert store.configured_by == "7"
    assert denied_decision.status_code == 403
    assert approved.status_code == 200
    assert store.decided_by == "7"


def test_gate_permission_errors_use_problem_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeOperationalGateStore()
    monkeypatch.setattr(
        operational_gate_api_module, "get_operational_gate_store", lambda: store
    )

    denied = asyncio.run(
        request("GET", "/v1/admin/gates", permissions="APP.ASK:VIEW")
    )

    assert denied.status_code == 403
    assert denied.headers["content-type"].startswith("application/problem+json")
    assert denied.json() == {
        "type": "urn:dwp:problem:operational-gates:gate_permission_denied",
        "title": "Operational gate permission required",
        "status": 403,
        "detail": "ADMIN.DWAION_GATES:VIEW permission is required.",
        "code": "GATE_PERMISSION_DENIED",
        "instance": "/v1/admin/gates",
        "correlationId": "corr-1",
        "context": {},
    }


def _detail(gate: OperationalGateSummary) -> OperationalGateDetail:
    return OperationalGateDetail(
        gate=gate,
        evidence=[],
        missing_evidence_types=[],
        approval_eligibility=OperationalGateApprovalEligibility(
            eligible=True,
            reason=GateApprovalEligibilityReason.ELIGIBLE,
        ),
        events=[],
    )


def _gate() -> OperationalGateSummary:
    return OperationalGateSummary(
        gate_key=OperationalGateKey.MODEL_CREDENTIALS,
        category=GateCategory.AI_RUNTIME,
        external_owner="CLOUD_SECURITY",
        delivery_critical=True,
        selected_option="MANAGED_IDENTITY",
        options=[OperationalGateOption(code="MANAGED_IDENTITY", recommended=True)],
        required_evidence_types=[GateEvidenceType.SECURITY_REVIEW],
        status=GateStatus.READY_FOR_APPROVAL,
        owner_user_id="cloud-security",
        last_configured_by="6",
        last_validated_by="8",
        evidence_count=1,
        configuration_revision=1,
        policy_version=3,
        updated_at=datetime.now(timezone.utc),
    )
