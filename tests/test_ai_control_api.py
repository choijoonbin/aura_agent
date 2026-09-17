from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import dwp_agent.ai_control_api as ai_control_api_module
from dwp_agent.ai_control_contracts import (
    AIControlOverview,
    AIUsageObservation,
    BudgetEnforcementMode,
    EnforcementActivationState,
    EvaluationGateStatus,
    MeasurementFreshness,
    ModelRoutePolicy,
    RuntimeControlState,
    TenantAIExecutionPolicy,
)
from dwp_agent.main import app
from dwp_agent.product_surface_pep import RESPONSE_REVISION_HEADER, scope_key


SERVICE_TOKEN = "test-gateway-service-token"
NOW = datetime(2026, 9, 17, 4, 0, tzinfo=timezone.utc)
DECISION_REVISION = "psr-" + "d" * 64
ROUTES = {
    ("GET", "/v1/admin/ai-control"): "route.dwaion.management.ai-control.page",
    ("POST", "/v1/admin/ai-control/bootstrap"):
        "route.dwaion.management.ai-control-bootstrap.action",
    ("PUT", "/v1/admin/ai-control/policy"):
        "route.dwaion.management.ai-control-update.action",
    ("POST", "/v1/admin/ai-control/emergency"):
        "route.dwaion.management.ai-control-emergency.action",
}


@pytest.fixture(autouse=True)
def configured_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V6_ENABLED", "true")
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V21_ENABLED", "true")


class FakeAIControlStore:
    def __init__(self) -> None:
        self.last_tenant: str | None = None
        self.emergency_actor: str | None = None
        self.policy = _policy()

    def overview(self, *, tenant_id: str, now: datetime) -> AIControlOverview:
        self.last_tenant = tenant_id
        return AIControlOverview(
            enforcement_activation_state=EnforcementActivationState.DISABLED,
            runtime_control_state=(
                RuntimeControlState.EMERGENCY_DISABLED
                if self.policy.emergency_disabled
                else RuntimeControlState.ENABLED
            ),
            policy=self.policy,
            usage=AIUsageObservation(
                period_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
                period_end=datetime(2026, 10, 1, tzinfo=timezone.utc),
                measured_input_tokens=100,
                measured_output_tokens=20,
                measured_total_tokens=120,
                reserved_tokens=256,
                measurement_freshness=MeasurementFreshness.CURRENT,
                measurement_observed_at=now,
            ),
            warnings=[
                "PROVIDER_USAGE_UNAVAILABLE",
                "PROVIDER_PRICING_UNAVAILABLE",
                "PROVIDER_BILLING_UNAVAILABLE",
                "AI_TOKEN_BUDGET_ALERT_THRESHOLD_REACHED",
            ],
        )

    def set_emergency_disabled(
        self, *, tenant_id: str, actor_user_id: str, correlation_id: str, request,
    ) -> TenantAIExecutionPolicy:
        assert (tenant_id, correlation_id) == ("7", "corr-7")
        assert request.expected_version == 4
        self.emergency_actor = actor_user_id
        self.policy = self.policy.model_copy(update={
            "emergency_disabled": request.disabled,
            "policy_version": 5,
        })
        return self.policy


def _policy() -> TenantAIExecutionPolicy:
    return TenantAIExecutionPolicy(
        emergency_disabled=False,
        allowed_model_routes=[ModelRoutePolicy(
            provider="OPENAI", model="gpt-configured",
        )],
        allowed_tool_keys=["SEARCH.READ"],
        allowed_knowledge_sources=["WORK_ITEM"],
        max_output_tokens_per_request=256,
        budget_enforcement_mode=BudgetEnforcementMode.ALERT_ONLY,
        period_token_limit=400,
        alert_threshold_percent=80,
        require_evaluation_pass=False,
        evaluation_gate_status=EvaluationGateStatus.NOT_REQUIRED,
        policy_version=4,
        updated_at=NOW,
    )


async def request(
    method: str,
    path: str,
    *,
    permissions: str,
    json: dict | None = None,
    header_overrides: dict[str, str] | None = None,
) -> httpx.Response:
    headers = {
        "X-DWP-Service-Token": SERVICE_TOKEN,
        "X-DWP-Tenant-ID": "7",
        "X-DWP-User-ID": "9",
        "X-Correlation-ID": "corr-7",
        "X-DWP-Permissions": permissions,
        "X-DWP-Roles": "TENANT_ADMIN",
        "X-DWP-Identity-Plane": "TENANT",
        "X-DWP-Rollout-State": "110",
        "X-DWP-Rollout-Revision": "rollout-" + "a" * 64,
        "X-DWP-Rollout-Cohort": "full",
        "X-DWP-Route-Contract-Key": ROUTES[(method, path)],
        "X-DWP-Context-Key": "psc-" + "b" * 64,
        "X-DWP-Context-Scope-Key": scope_key(
            7, 9, surface_key="dwaion.management",
            source="APP_RESOURCE_SET:RS_DWAION", kind="RESOURCE_SET",
        ),
        "X-DWP-Active-Access-Mode": "NORMAL",
        "X-DWP-Current-Decision-Revision": DECISION_REVISION,
        "X-DWP-Current-Revalidate-At": "2099-01-01T00:00:00Z",
        "X-DWP-Expected-Decision-Revision": DECISION_REVISION,
    }
    headers.update(header_overrides or {})
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, path, headers=headers, json=json)


def test_overview_is_tenant_scoped_and_marks_external_cost_data_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeAIControlStore()
    monkeypatch.setattr(ai_control_api_module, "get_ai_control_store", lambda: store)

    denied = asyncio.run(request(
        "GET", "/v1/admin/ai-control", permissions="ADMIN.DWAION:MANAGE",
    ))
    allowed = asyncio.run(request(
        "GET", "/v1/admin/ai-control", permissions="ADMIN.DWAION_SAFETY:VIEW",
    ))

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert store.last_tenant == "7"
    data = allowed.json()["data"]
    assert data["usage"]["providerUsageState"] == "UNAVAILABLE"
    assert data["usage"]["providerPricingState"] == "UNAVAILABLE"
    assert data["usage"]["providerBillingState"] == "UNAVAILABLE"
    assert data["usage"]["estimatedCostMinor"] is None
    assert data["usage"]["billedCostMinor"] is None
    assert data["controlScope"] == "ASK_RUNTIME"
    assert data["enforcementActivationState"] == "DISABLED"
    assert data["toolEnforcementState"] == "NOT_CONNECTED"
    assert "AI_TOKEN_BUDGET_ALERT_THRESHOLD_REACHED" in data["warnings"]
    assert allowed.headers[RESPONSE_REVISION_HEADER] == DECISION_REVISION
    assert allowed.headers.get_list(RESPONSE_REVISION_HEADER) == [DECISION_REVISION]


def test_emergency_disable_requires_safety_manage_and_policy_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeAIControlStore()
    monkeypatch.setattr(ai_control_api_module, "get_ai_control_store", lambda: store)
    body = {
        "disabled": True,
        "expectedVersion": 4,
        "changeReason": "Stop tenant AI execution during incident review.",
    }

    denied = asyncio.run(request(
        "POST", "/v1/admin/ai-control/emergency",
        permissions="ADMIN.DWAION_SAFETY:UPDATE", json=body,
    ))
    stopped = asyncio.run(request(
        "POST", "/v1/admin/ai-control/emergency",
        permissions="ADMIN.DWAION_SAFETY:MANAGE", json=body,
    ))

    assert denied.status_code == 403
    assert stopped.status_code == 200
    assert stopped.json()["data"]["runtimeControlState"] == "EMERGENCY_DISABLED"
    assert store.emergency_actor == "9"


def test_enforced_budget_requires_a_runtime_limit_and_rejects_secret_material() -> None:
    base = {
        "idempotencyKey": "00000000-0000-0000-0000-000000000019",
        "expectedExistingCount": 0,
        "allowedModelRoutes": [{"provider": "OPENAI", "model": "gpt-configured"}],
        "allowedKnowledgeSources": ["WORK_ITEM"],
        "maxOutputTokensPerRequest": 256,
        "budgetEnforcementMode": "ENFORCED",
        "alertThresholdPercent": 80,
        "requireEvaluationPass": False,
        "evaluationGateStatus": "NOT_REQUIRED",
        "changeReason": "Initialize tenant AI execution controls.",
    }
    missing_limit = asyncio.run(request(
        "POST", "/v1/admin/ai-control/bootstrap",
        permissions="ADMIN.DWAION_SAFETY:UPDATE", json=base,
    ))
    secret_reason = asyncio.run(request(
        "POST", "/v1/admin/ai-control/bootstrap",
        permissions="ADMIN.DWAION_SAFETY:UPDATE",
        json={
            **base,
            "periodTokenLimit": 10_000,
            "changeReason": "Rotate api_key=should-never-be-stored in this policy.",
        },
    ))
    secret_model = asyncio.run(request(
        "POST", "/v1/admin/ai-control/bootstrap",
        permissions="ADMIN.DWAION_SAFETY:UPDATE",
        json={
            **base,
            "periodTokenLimit": 10_000,
            "allowedModelRoutes": [{
                "provider": "OPENAI",
                "model": "api_key=should-never-be-stored",
            }],
        },
    ))
    control_character = asyncio.run(request(
        "POST", "/v1/admin/ai-control/bootstrap",
        permissions="ADMIN.DWAION_SAFETY:UPDATE",
        json={
            **base,
            "periodTokenLimit": 10_000,
            "allowedModelRoutes": [{
                "provider": "OPENAI",
                "model": "gpt-configured",
                "region": "kr\ncentral",
            }],
        },
    ))
    forged_availability = asyncio.run(request(
        "POST", "/v1/admin/ai-control/bootstrap",
        permissions="ADMIN.DWAION_SAFETY:UPDATE",
        json={
            **base,
            "periodTokenLimit": 10_000,
            "allowedModelRoutes": [{
                "provider": "OPENAI",
                "model": "gpt-configured",
                "availabilityState": "VERIFIED",
                "availabilityObservedAt": "2026-09-17T04:00:00Z",
            }],
        },
    ))
    forged_evaluation = asyncio.run(request(
        "POST", "/v1/admin/ai-control/bootstrap",
        permissions="ADMIN.DWAION_SAFETY:UPDATE",
        json={
            **base,
            "periodTokenLimit": 10_000,
            "requireEvaluationPass": True,
            "evaluationGateStatus": "PASSED",
            "evaluationObservedAt": "2026-09-17T04:00:00Z",
            "evaluationPolicyVersion": 1,
        },
    ))
    forged_failure = asyncio.run(request(
        "POST", "/v1/admin/ai-control/bootstrap",
        permissions="ADMIN.DWAION_SAFETY:UPDATE",
        json={
            **base,
            "periodTokenLimit": 10_000,
            "requireEvaluationPass": True,
            "evaluationGateStatus": "FAILED",
            "evaluationObservedAt": "2026-09-17T04:00:00Z",
            "evaluationPolicyVersion": 1,
        },
    ))

    assert missing_limit.status_code == 422
    assert secret_reason.status_code == 422
    assert secret_model.status_code == 422
    assert control_character.status_code == 422
    assert forged_availability.status_code == 422
    assert forged_evaluation.status_code == 422
    assert forged_failure.status_code == 422


def test_ai_control_surface_guard_rejects_untrusted_identity_and_stale_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeAIControlStore()
    monkeypatch.setattr(ai_control_api_module, "get_ai_control_store", lambda: store)

    anonymous = asyncio.run(request(
        "GET", "/v1/admin/ai-control", permissions="ADMIN.DWAION_SAFETY:VIEW",
        header_overrides={"X-DWP-Service-Token": "invalid"},
    ))
    wrong_plane = asyncio.run(request(
        "GET", "/v1/admin/ai-control", permissions="ADMIN.DWAION_SAFETY:VIEW",
        header_overrides={"X-DWP-Identity-Plane": "PROVIDER"},
    ))
    support = asyncio.run(request(
        "GET", "/v1/admin/ai-control", permissions="ADMIN.DWAION_SAFETY:VIEW",
        header_overrides={"X-DWP-Support-Session-ID": "support-session-1"},
    ))
    wrong_scope = asyncio.run(request(
        "GET", "/v1/admin/ai-control", permissions="ADMIN.DWAION_SAFETY:VIEW",
        header_overrides={"X-DWP-Context-Scope-Key": "scope-" + "0" * 32},
    ))
    stale = asyncio.run(request(
        "POST", "/v1/admin/ai-control/emergency",
        permissions="ADMIN.DWAION_SAFETY:MANAGE",
        json={
            "disabled": True,
            "expectedVersion": 4,
            "changeReason": "Stop Ask runtime during incident review.",
        },
        header_overrides={"X-DWP-Expected-Decision-Revision": "psr-" + "e" * 64},
    ))

    assert anonymous.status_code == 401
    assert wrong_plane.status_code == 403
    assert support.status_code == 403
    assert wrong_scope.status_code == 403
    assert stale.status_code == 409
    assert store.last_tenant is None
    assert store.emergency_actor is None


def test_v6_readiness_cannot_open_v21_ai_control_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeAIControlStore()
    monkeypatch.setattr(ai_control_api_module, "get_ai_control_store", lambda: store)
    monkeypatch.delenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V21_ENABLED")

    response = asyncio.run(request(
        "GET", "/v1/admin/ai-control", permissions="ADMIN.DWAION_SAFETY:VIEW",
    ))

    assert response.status_code == 503
    assert "authorization v21" in response.text
    assert store.last_tenant is None
