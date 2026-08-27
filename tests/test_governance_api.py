import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import dwp_agent.governance_api as governance_api_module
from dwp_agent.contracts import CitationSourceType
from dwp_agent.governance_contracts import (
    BootstrapGovernancePoliciesRequest,
    ConnectionState,
    DataClassification,
    DataSourcePolicy,
    EvaluationOutcome,
    EvaluationResult,
    EvaluationRun,
    EvaluationRunState,
    EvaluationRunSummary,
    GovernanceAuditEvent,
    GovernanceAuditPage,
    SourceAccessMode,
    UpdateDataSourcePolicyRequest,
)
from dwp_agent.evaluation_store import EvaluationRunAlreadyActive, EvaluationRunLeaseLost
from dwp_agent.evaluation_evidence_store import EvaluationEvidenceStoreMixin
from dwp_agent.governance_store import GovernancePolicyNotInitialized
from dwp_agent.main import app


SERVICE_TOKEN = "test-gateway-service-token"


@pytest.fixture(autouse=True)
def configured_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)


class FakeGovernanceStore:
    def __init__(self) -> None:
        self.bootstrap_calls = 0
        self.policy = DataSourcePolicy(
            source_key=CitationSourceType.WORK_ITEM,
            display_name="Work",
            description="Verified work items",
            provider_type="DWP_PLATFORM",
            classification=DataClassification.INTERNAL,
            access_mode=SourceAccessMode.SOURCE_PERMISSIONS,
            enabled=True,
            connection_state=ConnectionState.CONNECTED,
            policy_version=1,
            updated_at=datetime.now(timezone.utc),
        )

    def source_policies(self, *, tenant_id: str, actor_user_id: str):
        assert (tenant_id, actor_user_id) == ("1", "7")
        return [self.policy]

    def bootstrap_source_policies(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        request: BootstrapGovernancePoliciesRequest,
    ):
        assert (tenant_id, actor_user_id, correlation_id) == ("1", "7", "corr-1")
        assert request.expected_existing_count == 0
        self.bootstrap_calls += 1
        return [self.policy]

    def update_source_policy(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        source_key: CitationSourceType,
        request: UpdateDataSourcePolicyRequest,
    ):
        assert (tenant_id, actor_user_id, correlation_id) == ("1", "7", "corr-1")
        assert source_key == CitationSourceType.WORK_ITEM
        self.policy = self.policy.model_copy(
            update={
                "classification": request.classification,
                "access_mode": request.access_mode,
                "enabled": request.enabled,
                "policy_version": 2,
            }
        )
        return self.policy

    def audit_events(
        self,
        *,
        tenant_id: str,
        category: str | None,
        query: str | None,
        page: int,
        size: int,
    ):
        assert tenant_id == "1"
        event = GovernanceAuditEvent(
            event_id=uuid4(),
            category=category or "SOURCE",
            event_type="source-policy.updated",
            target_type="DATA_SOURCE",
            target_key="WORK_ITEM",
            actor_user_id="7",
            correlation_id="corr-1",
            change_reason="Align source handling with tenant policy.",
            created_at=datetime.now(timezone.utc),
        )
        return GovernanceAuditPage(
            content=[event], page=page, size=size, total_elements=1, total_pages=1
        )

    def audit_csv(self, *, tenant_id: str, category: str | None, query: str | None):
        assert tenant_id == "1"
        return "eventId,category\r\n1,SOURCE\r\n", False


class FakeEvaluationStore:
    def __init__(self) -> None:
        self.evaluation_set_id = uuid4()
        self.evaluation_run_id = uuid4()
        self.result = EvaluationResult(
            evaluation_case_id=uuid4(),
            case_name="Grounded work summary",
            outcome=EvaluationOutcome.PASS,
            status_code="COMPLETED",
            grounded=True,
            expected_terms_matched=2,
            expected_terms_total=2,
            latency_ms=240,
        )
        self.run = EvaluationRun(
            evaluation_run_id=self.evaluation_run_id,
            evaluation_set_id=self.evaluation_set_id,
            run_state=EvaluationRunState.COMPLETED,
            case_count=1,
            passed_count=1,
            failed_count=0,
            configuration_required_count=0,
            model_ref="provider/model",
            results=[self.result],
            created_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )

    def list_runs(self, *, tenant_id: str, evaluation_set_id, limit: int):
        assert tenant_id == "1"
        assert evaluation_set_id == self.evaluation_set_id
        assert limit == 20
        return [
            EvaluationRunSummary(
                evaluation_run_id=self.run.evaluation_run_id,
                evaluation_set_id=self.run.evaluation_set_id,
                run_state=self.run.run_state,
                case_count=self.run.case_count,
                passed_count=self.run.passed_count,
                failed_count=self.run.failed_count,
                configuration_required_count=self.run.configuration_required_count,
                pass_rate=100,
                model_ref=self.run.model_ref,
                created_at=self.run.created_at,
                completed_at=self.run.completed_at,
            )
        ]

    def run_detail(self, *, tenant_id: str, evaluation_set_id, evaluation_run_id):
        assert tenant_id == "1"
        assert evaluation_set_id == self.evaluation_set_id
        assert evaluation_run_id == self.evaluation_run_id
        return self.run

    def run_csv(self, *, tenant_id: str, evaluation_set_id, evaluation_run_id):
        self.run_detail(
            tenant_id=tenant_id,
            evaluation_set_id=evaluation_set_id,
            evaluation_run_id=evaluation_run_id,
        )
        return "evaluationRunId,outcome\r\n1,PASS\r\n"


async def request(
    method: str,
    path: str,
    *,
    permissions: str,
    json: dict | None = None,
) -> httpx.Response:
    headers = {
        "X-DWP-Service-Token": SERVICE_TOKEN,
        "X-DWP-User-ID": "7",
        "X-DWP-Tenant-ID": "1",
        "X-Correlation-ID": "corr-1",
        "X-DWP-Permissions": permissions,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, json=json)


def test_source_governance_requires_granular_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeGovernanceStore()
    monkeypatch.setattr(governance_api_module, "get_governance_store", lambda: store)

    legacy_scope = asyncio.run(
        request("GET", "/v1/admin/sources", permissions="ADMIN.DWAION:VIEW")
    )
    allowed = asyncio.run(
        request("GET", "/v1/admin/sources", permissions="ADMIN.DWAION_SOURCES:VIEW")
    )
    updated = asyncio.run(
        request(
            "PATCH",
            "/v1/admin/sources/WORK_ITEM",
            permissions="ADMIN.DWAION_SOURCES:UPDATE",
            json={
                "enabled": True,
                "accessMode": "SOURCE_PERMISSIONS",
                "classification": "CONFIDENTIAL",
                "expectedVersion": 1,
                "changeReason": "Align source handling with tenant policy.",
            },
        )
    )

    assert legacy_scope.status_code == 403
    assert allowed.status_code == 200
    assert updated.status_code == 200
    assert updated.json()["data"]["policyVersion"] == 2


def test_source_policy_bootstrap_requires_manage_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeGovernanceStore()
    monkeypatch.setattr(governance_api_module, "get_governance_store", lambda: store)

    payload = {
        "idempotencyKey": str(uuid4()),
        "expectedExistingCount": 0,
        "changeReason": "Initialize blocked source policies for this tenant.",
    }
    denied = asyncio.run(
        request(
            "POST",
            "/v1/admin/sources/bootstrap",
            permissions="ADMIN.DWAION_SOURCES:VIEW",
            json=payload,
        )
    )
    allowed = asyncio.run(
        request(
            "POST",
            "/v1/admin/sources/bootstrap",
            permissions="ADMIN.DWAION_SOURCES:MANAGE",
            json=payload,
        )
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert store.bootstrap_calls == 1


def test_policy_updates_require_explicit_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeGovernanceStore()

    def reject_uninitialized(**_kwargs):
        raise GovernancePolicyNotInitialized(
            "GOVERNANCE_BOOTSTRAP_REQUIRED: The policy set has not been initialized."
        )

    monkeypatch.setattr(store, "update_source_policy", reject_uninitialized)
    monkeypatch.setattr(
        store, "update_action_policy", reject_uninitialized, raising=False
    )
    monkeypatch.setattr(governance_api_module, "get_governance_store", lambda: store)

    source = asyncio.run(
        request(
            "PATCH",
            "/v1/admin/sources/WORK_ITEM",
            permissions="ADMIN.DWAION_SOURCES:UPDATE",
            json={
                "enabled": False,
                "accessMode": "BLOCKED",
                "classification": "INTERNAL",
                "expectedVersion": 1,
                "changeReason": "Reject the source update before explicit initialization.",
            },
        )
    )
    action = asyncio.run(
        request(
            "PATCH",
            "/v1/admin/actions/CALENDAR.EVENT.CREATE",
            permissions="ADMIN.DWAION_ACTIONS:UPDATE",
            json={
                "enabled": False,
                "confirmationRequired": True,
                "executionPolicy": "BLOCKED",
                "expectedVersion": 1,
                "changeReason": "Reject the action update before explicit initialization.",
            },
        )
    )

    assert source.status_code == 409
    assert action.status_code == 409
    assert source.json()["detail"].startswith("GOVERNANCE_BOOTSTRAP_REQUIRED")
    assert action.json()["detail"].startswith("GOVERNANCE_BOOTSTRAP_REQUIRED")


def test_audit_export_requires_export_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeGovernanceStore()
    monkeypatch.setattr(governance_api_module, "get_governance_store", lambda: store)

    denied = asyncio.run(
        request("GET", "/v1/admin/audit/export", permissions="ADMIN.DWAION_AUDIT:VIEW")
    )
    allowed = asyncio.run(
        request("GET", "/v1/admin/audit/export", permissions="ADMIN.DWAION_AUDIT:EXPORT")
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.headers["x-dwp-export-limit"] == "10000"
    assert allowed.headers["x-dwp-export-truncated"] == "false"
    assert allowed.text.startswith("eventId,category")


def test_evaluation_run_history_and_export_use_separate_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeEvaluationStore()
    monkeypatch.setattr(governance_api_module, "get_evaluation_store", lambda: store)
    base = f"/v1/admin/evaluations/{store.evaluation_set_id}/runs"

    history = asyncio.run(
        request("GET", base, permissions="ADMIN.DWAION_EVALUATION:VIEW")
    )
    detail = asyncio.run(
        request(
            "GET",
            f"{base}/{store.evaluation_run_id}",
            permissions="ADMIN.DWAION_EVALUATION:VIEW",
        )
    )
    denied_export = asyncio.run(
        request(
            "GET",
            f"{base}/{store.evaluation_run_id}/export",
            permissions="ADMIN.DWAION_EVALUATION:VIEW",
        )
    )
    allowed_export = asyncio.run(
        request(
            "GET",
            f"{base}/{store.evaluation_run_id}/export",
            permissions="ADMIN.DWAION_EVALUATION:EXPORT",
        )
    )

    assert history.status_code == 200
    assert history.json()["data"][0]["passRate"] == 100
    assert detail.status_code == 200
    assert detail.json()["data"]["results"][0]["grounded"] is True
    assert denied_export.status_code == 403
    assert allowed_export.status_code == 200
    assert allowed_export.headers["x-dwp-export-content"] == "metrics-only"


def test_concurrent_evaluation_run_returns_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(governance_api_module, "get_evaluation_store", lambda: object())
    monkeypatch.setattr(
        governance_api_module,
        "run_evaluation",
        lambda **_: (_ for _ in ()).throw(
            EvaluationRunAlreadyActive("An evaluation run is already active for this test set.")
        ),
    )

    response = asyncio.run(
        request(
            "POST",
            f"/v1/admin/evaluations/{uuid4()}/runs",
            permissions="APP.ASK:VIEW,ADMIN.DWAION_EVALUATION:EXECUTE",
            json={},
        )
    )

    assert response.status_code == 409


def test_expired_evaluation_run_completion_returns_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(governance_api_module, "get_evaluation_store", lambda: object())
    monkeypatch.setattr(
        governance_api_module,
        "run_evaluation",
        lambda **_: (_ for _ in ()).throw(
            EvaluationRunLeaseLost("The evaluation run lease expired or was already recovered.")
        ),
    )

    response = asyncio.run(
        request(
            "POST",
            f"/v1/admin/evaluations/{uuid4()}/runs",
            permissions="APP.ASK:VIEW,ADMIN.DWAION_EVALUATION:EXECUTE",
            json={},
        )
    )

    assert response.status_code == 409


def test_evaluation_csv_neutralizes_spreadsheet_formulas() -> None:
    assert EvaluationEvidenceStoreMixin._safe_csv_cell("=SUM(A1:A2)") == "'=SUM(A1:A2)"
    assert EvaluationEvidenceStoreMixin._safe_csv_cell("@malicious") == "'@malicious"
    assert EvaluationEvidenceStoreMixin._safe_csv_cell("  =SUM(A1:A2)") == "'  =SUM(A1:A2)"
    assert EvaluationEvidenceStoreMixin._safe_csv_cell("PASS") == "PASS"


def test_running_evaluation_summary_does_not_report_zero_pass_rate() -> None:
    now = datetime.now(timezone.utc)
    summary = EvaluationEvidenceStoreMixin._run_summary(
        (uuid4(), uuid4(), "RUNNING", 3, 0, 0, 0, None, now, None)
    )

    assert summary.pass_rate is None
