from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

import dwp_agent.main as main_module
import dwp_agent.activity_api as activity_api_module
import dwp_agent.user_run_api as user_run_api_module
from dwp_agent.activity_contracts import ActivityRunSnapshot
from dwp_agent.contracts import (
    AgentRegistryResolution,
    AnswerConfidence,
    AskCitation,
    AskModelRoute,
    AskPolicyDecision,
    AskResponse,
    CitationSourceType,
    ConversationDetail,
    ConversationMessage,
    ConversationRole,
    ConversationSummary,
    ModelRouteState,
    PolicyOutcome,
    RegistryResolutionStatus,
    RegistryRiskTier,
    RiskTier,
)
from dwp_agent.policy import SafetyControls
from dwp_agent.product_surface_pep import (
    ACCESS_POLICY_KEY,
    ACTION_ROUTE,
    ASK_STREAM_ROUTE,
    ACTIVITY_EVENTS_ROUTE,
    ACTIVITY_EVENT_ROUTE,
    ACTIVITY_SUMMARY_ROUTE,
    CONVERSATION_DELETE_ROUTE,
    CONVERSATION_RENAME_ROUTE,
    DATA_ROUTE,
    EXPECTED_REVISION_HEADER,
    OWNER_SERVICE,
    PAGE_ROUTE,
    PRODUCT_KEY,
    RESPONSE_REVISION_HEADER,
    RUNS_ROUTE,
    RUN_DETAIL_ROUTE,
    SURFACE_KEY,
    owns_candidate,
    resolve_binding,
    scope_key,
    self_scope_key,
)
from dwp_agent.product_surface_pep_bindings import ROUTE_BINDING_SPECS
from dwp_agent.user_run_contracts import AgentRunState, UserAgentRunSummary


ROOT = Path(__file__).resolve().parents[1]
SERVICE_TOKEN = "test-agent-service-token"
TENANT_ID = 3
USER_ID = 17
CONVERSATION_ID = UUID("20000000-0000-4000-8000-000000000017")
RUN_ID = UUID("40000000-0000-4000-8000-000000000017")
ROLLOUT_REVISION = "rollout-" + "a" * 64
DECISION_REVISION = "psr-" + "b" * 64
STALE_REVISION = "psr-" + "c" * 64
CONTEXT_KEY = "psc-" + "d" * 64


class RuntimeSpy:
    def __init__(self) -> None:
        self.calls = 0
        self.workspace_authorization = None

    def answer(
        self,
        request,
        *,
        identity,
        safety_controls,
        workspace_authorization,
        on_progress=None,
    ) -> AskResponse:
        self.calls += 1
        if on_progress is not None:
            on_progress("AUTHORIZING")
        self.workspace_authorization = workspace_authorization
        return _ask_response(request.request_id, identity.correlation_id)


@pytest.fixture(autouse=True)
def product_surface_runtime(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V4_ENABLED", "true")
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V5_ENABLED", "true")
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V6_ENABLED", "true")
    monkeypatch.delenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", raising=False)
    store = ConversationStoreStub()
    monkeypatch.setattr(main_module, "get_conversation_store", lambda: store)
    monkeypatch.setattr(main_module, "require_operational_delivery", lambda **_: None)
    monkeypatch.setattr(
        main_module,
        "runtime_safety_controls",
        lambda _request, _identity: SafetyControls(),
    )
    main_module.app.dependency_overrides.clear()
    yield store
    main_module.app.dependency_overrides.clear()


class ConversationStoreStub:
    def __init__(self) -> None:
        self.rename_calls = 0
        self.delete_calls = 0
        timestamp = datetime.now(timezone.utc)
        self.summary = ConversationSummary(
            conversation_id=CONVERSATION_ID,
            title="오늘 업무",
            locale="ko",
            message_count=1,
            agent_key=None,
            source_systems=[],
            evidence_count=0,
            summary_excerpt=None,
            last_answer_status=None,
            retention_until=timestamp,
            legal_hold=False,
            created_at=timestamp,
            updated_at=timestamp,
            last_message_at=timestamp,
        )
        self.detail = ConversationDetail(
            summary=self.summary,
            messages=[
                ConversationMessage(
                    message_id=UUID("30000000-0000-4000-8000-000000000017"),
                    role=ConversationRole.USER,
                    content="오늘 업무를 정리해 주세요.",
                    created_at=timestamp,
                )
            ],
        )

    def list(self, *, tenant_id: str, user_id: str, limit: int = 30):
        assert tenant_id == str(TENANT_ID)
        assert user_id == str(USER_ID)
        assert limit == 30
        return [self.summary]

    def get(self, *, tenant_id: str, user_id: str, conversation_id: UUID):
        assert tenant_id == str(TENANT_ID)
        assert user_id == str(USER_ID)
        assert conversation_id == CONVERSATION_ID
        return self.detail

    def rename(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID,
        title: str,
    ):
        assert tenant_id == str(TENANT_ID)
        assert user_id == str(USER_ID)
        assert conversation_id == CONVERSATION_ID
        assert title == "변경한 제목"
        self.rename_calls += 1
        return self.detail

    def delete(self, *, tenant_id: str, user_id: str, conversation_id: UUID):
        assert tenant_id == str(TENANT_ID)
        assert user_id == str(USER_ID)
        assert conversation_id == CONVERSATION_ID
        self.delete_calls += 1


class ActivityStoreStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.snapshot = ActivityRunSnapshot(
            run_id=RUN_ID,
            tenant_id=str(TENANT_ID),
            user_id=str(USER_ID),
            agent_key="DWP_ASSISTANT",
            agent_revision=2,
            run_state="COMPLETED",
            risk_tier="L0",
            policy_outcome="ALLOW",
            created_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )

    def page(self, *, tenant_id, user_id, filters, snapshot_at, now, after, limit):
        self.calls.append(("page", tenant_id, user_id))
        return [self.snapshot], False

    def detail(self, *, tenant_id, user_id, run_id):
        self.calls.append(("detail", tenant_id, user_id))
        return self.snapshot if run_id == RUN_ID else None

    def counts(self, *, tenant_id, user_id, filters, now):
        self.calls.append(("summary", tenant_id, user_id))
        return {"COMPLETED": 1}


class UserRunStoreStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.run = UserAgentRunSummary(
            run_id=RUN_ID,
            agent_key="DWP_ASSISTANT",
            agent_revision=2,
            run_state=AgentRunState.COMPLETED,
            risk_tier="L0",
            policy_outcome="ALLOW",
            source_count=0,
            latency_ms=0,
            created_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )

    def list(self, *, tenant_id, user_id, limit, run_state):
        self.calls.append(("list", tenant_id, user_id))
        return [self.run]

    def get(self, *, tenant_id, user_id, run_id):
        self.calls.append(("detail", tenant_id, user_id))
        return self.run if run_id == RUN_ID else None


def test_rejects_cross_tenant_scope_at_agent_owner_pep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fail_if_store_is_reached(monkeypatch)
    response = _request(
        "GET",
        "/v1/conversations",
        headers=_headers(PAGE_ROUTE, scope=self_scope_key(4, USER_ID)),
    )

    assert response.status_code == 403
    assert calls == []


def test_rejects_canonical_opaque_scope_escape_at_agent_owner_pep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fail_if_store_is_reached(monkeypatch)
    response = _request(
        "GET",
        "/v1/conversations",
        headers=_headers(PAGE_ROUTE, scope=_foreign_scope()),
    )

    assert response.status_code == 403
    assert calls == []


def test_rejects_stale_authority_revision_at_agent_owner_pep() -> None:
    runtime = RuntimeSpy()
    main_module.app.dependency_overrides[main_module.get_ask_runtime] = lambda: runtime
    response = _request(
        "POST",
        "/v1/ask",
        headers=_headers(ACTION_ROUTE, expected_revision=STALE_REVISION),
        json_body={"requestId": "request-stale", "query": "오늘 할 일을 알려 주세요."},
    )

    assert response.status_code == 409
    assert runtime.calls == 0


def test_v6_state_changes_reject_stale_authority_before_runtime_or_store(
    product_surface_runtime: ConversationStoreStub,
) -> None:
    runtime = RuntimeSpy()
    main_module.app.dependency_overrides[main_module.get_ask_runtime] = lambda: runtime

    stream = _request(
        "POST",
        "/v1/ask/stream",
        headers=_headers(ASK_STREAM_ROUTE, expected_revision=STALE_REVISION),
        json_body={"requestId": "request-stream-stale", "query": "오늘 할 일을 알려 주세요."},
    )
    rename = _request(
        "PATCH",
        f"/v1/conversations/{CONVERSATION_ID}",
        headers=_headers(CONVERSATION_RENAME_ROUTE, expected_revision=STALE_REVISION),
        json_body={"title": "변경한 제목"},
    )
    delete = _request(
        "DELETE",
        f"/v1/conversations/{CONVERSATION_ID}",
        headers=_headers(CONVERSATION_DELETE_ROUTE, expected_revision=STALE_REVISION),
    )

    assert [stream.status_code, rename.status_code, delete.status_code] == [409, 409, 409]
    assert runtime.calls == 0
    assert product_surface_runtime.rename_calls == 0
    assert product_surface_runtime.delete_calls == 0


def test_v6_state_changes_execute_only_with_exact_route_and_current_revision(
    product_surface_runtime: ConversationStoreStub,
) -> None:
    runtime = RuntimeSpy()
    main_module.app.dependency_overrides[main_module.get_ask_runtime] = lambda: runtime

    stream = _request(
        "POST",
        "/v1/ask/stream",
        headers=_headers(ASK_STREAM_ROUTE),
        json_body={"requestId": "request-stream-exact", "query": "오늘 할 일을 알려 주세요."},
    )
    rename = _request(
        "PATCH",
        f"/v1/conversations/{CONVERSATION_ID}",
        headers=_headers(CONVERSATION_RENAME_ROUTE),
        json_body={"title": "변경한 제목"},
    )
    delete = _request(
        "DELETE",
        f"/v1/conversations/{CONVERSATION_ID}",
        headers=_headers(CONVERSATION_DELETE_ROUTE),
    )

    assert [stream.status_code, rename.status_code, delete.status_code] == [200, 200, 204]
    assert runtime.calls == 1
    assert product_surface_runtime.rename_calls == 1
    assert product_surface_runtime.delete_calls == 1
    for response in (stream, rename, delete):
        assert response.headers[RESPONSE_REVISION_HEADER] == DECISION_REVISION


def test_rejects_normal_support_confused_deputy_at_agent_owner_pep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fail_if_store_is_reached(monkeypatch)
    normal_with_support = _request(
        "GET",
        "/v1/conversations",
        headers={
            **_headers(PAGE_ROUTE),
            "X-DWP-Support-Session-ID": "support-session-1",
        },
    )
    support_without_session = _request(
        "GET",
        "/v1/conversations",
        headers=_headers(PAGE_ROUTE, access_mode="PROVIDER_SUPPORT"),
    )
    provider_role_as_normal = _request(
        "GET",
        "/v1/conversations",
        headers={**_headers(PAGE_ROUTE), "X-DWP-Roles": "PROVIDER_SUPPORT"},
    )

    assert normal_with_support.status_code == 403
    assert support_without_session.status_code == 403
    assert provider_role_as_normal.status_code == 403
    assert calls == []


def test_rejects_internal_header_spoof_at_agent_owner_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fail_if_store_is_reached(monkeypatch)
    response = _request(
        "GET",
        "/v1/conversations",
        headers={**_headers(PAGE_ROUTE), "X-DWP-Service-Token": "attacker-token"},
    )

    assert response.status_code == 401
    assert calls == []


def test_executes_page_data_action_through_agent_owner_pep(
    product_surface_runtime: ConversationStoreStub,
) -> None:
    runtime = RuntimeSpy()
    main_module.app.dependency_overrides[main_module.get_ask_runtime] = lambda: runtime

    page = _request("GET", "/v1/conversations", headers=_headers(PAGE_ROUTE))
    data = _request(
        "GET",
        f"/v1/conversations/{CONVERSATION_ID}",
        headers=_headers(DATA_ROUTE),
    )
    action = _request(
        "POST",
        "/v1/ask",
        headers={
            **_headers(ACTION_ROUTE),
            "Cookie": "DWP_SESSION=product-surface-session",
        },
        json_body={"requestId": "request-exact", "query": "오늘 할 일을 알려 주세요."},
    )

    assert page.status_code == 200
    assert data.status_code == 200
    assert action.status_code == 200
    assert page.json()["data"][0] == product_surface_runtime.summary.model_dump(
        mode="json", by_alias=True
    )
    assert runtime.calls == 1
    assert runtime.workspace_authorization.available is True
    assert runtime.workspace_authorization.outbound_headers() == {
        "Cookie": "DWP_SESSION=product-surface-session"
    }
    for response in (page, data, action):
        assert response.headers[RESPONSE_REVISION_HEADER] == DECISION_REVISION


def test_executes_registered_activity_and_run_reads_through_owner_pep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = ActivityStoreStub()
    runs = UserRunStoreStub()
    monkeypatch.setattr(activity_api_module, "get_agent_activity_store", lambda: activity)
    monkeypatch.setattr(user_run_api_module, "get_user_run_store", lambda: runs)

    responses = (
        _request("GET", "/v1/activity/events", headers=_headers(ACTIVITY_EVENTS_ROUTE)),
        _request(
            "GET",
            f"/v1/activity/events/{RUN_ID}",
            headers=_headers(ACTIVITY_EVENT_ROUTE),
        ),
        _request(
            "GET",
            "/v1/activity/executions/summary",
            headers=_headers(ACTIVITY_SUMMARY_ROUTE),
        ),
        _request("GET", "/v1/runs", headers=_headers(RUNS_ROUTE)),
        _request("GET", f"/v1/runs/{RUN_ID}", headers=_headers(RUN_DETAIL_ROUTE)),
    )

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200]
    assert activity.calls == [
        ("page", str(TENANT_ID), str(USER_ID)),
        ("detail", str(TENANT_ID), str(USER_ID)),
        ("summary", str(TENANT_ID), str(USER_ID)),
    ]
    assert runs.calls == [
        ("list", str(TENANT_ID), str(USER_ID)),
        ("detail", str(TENANT_ID), str(USER_ID)),
    ]
    for response in responses:
        assert response.headers[RESPONSE_REVISION_HEADER] == DECISION_REVISION


def test_activity_route_requires_both_dwaion_and_activity_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = ActivityStoreStub()
    monkeypatch.setattr(activity_api_module, "get_agent_activity_store", lambda: activity)

    missing_activity = _request(
        "GET",
        "/v1/activity/events",
        headers={
            **_headers(ACTIVITY_EVENTS_ROUTE),
            "X-DWP-Permissions": "APP.ASK:VIEW",
        },
    )
    missing_ask = _request(
        "GET",
        "/v1/activity/events",
        headers={
            **_headers(ACTIVITY_EVENTS_ROUTE),
            "X-DWP-Permissions": "APP.ACTIVITY:VIEW",
        },
    )

    assert missing_activity.status_code == 403
    assert missing_ask.status_code == 403
    assert activity.calls == []


def test_activity_route_key_mismatch_fails_before_source_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = ActivityStoreStub()
    monkeypatch.setattr(activity_api_module, "get_agent_activity_store", lambda: activity)

    response = _request(
        "GET",
        "/v1/activity/events",
        headers=_headers(ACTIVITY_SUMMARY_ROUTE),
    )

    assert response.status_code == 503
    assert activity.calls == []


def test_activity_route_rejects_foreign_owner_scope_before_source_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = ActivityStoreStub()
    monkeypatch.setattr(activity_api_module, "get_agent_activity_store", lambda: activity)

    response = _request(
        "GET",
        "/v1/activity/events",
        headers=_headers(
            ACTIVITY_EVENTS_ROUTE,
            scope=self_scope_key(TENANT_ID + 1, USER_ID),
        ),
    )

    assert response.status_code == 403
    assert activity.calls == []


def test_v4_readiness_cannot_open_v5_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = ActivityStoreStub()
    monkeypatch.setattr(activity_api_module, "get_agent_activity_store", lambda: activity)
    monkeypatch.delenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V5_ENABLED")
    monkeypatch.delenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V6_ENABLED")

    response = _request(
        "GET",
        "/v1/activity/events",
        headers=_headers(ACTIVITY_EVENTS_ROUTE),
    )

    assert response.status_code == 503
    assert "authorization v5" in response.text
    assert activity.calls == []


def test_v5_readiness_cannot_open_v6_state_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V6_ENABLED")

    response = _request(
        "POST",
        "/v1/ask/stream",
        headers=_headers(ASK_STREAM_ROUTE),
        json_body={"requestId": "request-v6-disabled", "query": "오늘 할 일을 알려 주세요."},
    )

    assert response.status_code == 503
    assert "authorization v6" in response.text


def test_v4_draft_contract_exposes_exact_dwaion_consumer_metadata() -> None:
    projection = json.loads(
        (ROOT / "contracts/product-authorization/dwaion-pep-v4.draft.json").read_text()
    )

    assert projection["registryRef"] == {
        "bundleKey": "product-surfaces",
        "version": 4,
        "bundleStatus": "DRAFT",
        "checksum": "a9cd08260fd9a11dd7c612f2db6f03bb312f1e7843a2eb10b4082660da151137",
    }
    assert projection["activation"]["enabledByDefault"] is False
    assert projection["ownerServiceModule"] == OWNER_SERVICE
    assert projection["productId"] == PRODUCT_KEY
    assert projection["surfaceKey"] == SURFACE_KEY
    assert projection["accessPolicy"]["accessPolicyKey"] == ACCESS_POLICY_KEY
    assert "activityCapability" not in projection
    assert {route["routeKind"] for route in projection["routes"]} == {
        "PAGE",
        "DATA",
        "ACTION",
    }
    assert {route["routeContractKey"] for route in projection["routes"]} == {
        PAGE_ROUTE,
        DATA_ROUTE,
        ACTION_ROUTE,
    }


def test_v5_draft_contract_exposes_exact_dwaion_read_consumer_metadata() -> None:
    projection = json.loads(
        (ROOT / "contracts/product-authorization/dwaion-pep-v5.draft.json").read_text()
    )

    assert projection["registryRef"] == {
        "bundleKey": "product-surfaces",
        "version": 5,
        "bundleStatus": "DRAFT",
        "checksum": "c69816a06349fcbd45a0d946debfbce1d67e09b3ed87a8b056ec8a43f852109f",
    }
    assert projection["activation"] == {
        "enabledByDefault": False,
        "readinessEnvironment": "DWP_AGENT_PRODUCT_AUTHORIZATION_V5_ENABLED",
    }
    assert projection["ownerServiceModule"] == OWNER_SERVICE
    assert projection["productId"] == PRODUCT_KEY
    assert projection["surfaceKey"] == SURFACE_KEY
    assert projection["accessPolicy"]["accessPolicyKey"] == ACCESS_POLICY_KEY
    assert projection["activityCapability"] == {
        "capabilityContractKey": "dwaion.work.activity.read",
        "resolvedAuthority": "APP.ACTIVITY:VIEW",
        "requiresProductEntitlement": "APP.ASK:VIEW",
        "scopeResolver": "SELF",
    }
    assert {route["routeKind"] for route in projection["routes"]} == {
        "PAGE",
        "DATA",
        "ACTION",
    }
    assert {route["routeContractKey"] for route in projection["routes"]} == {
        PAGE_ROUTE,
        DATA_ROUTE,
        ACTION_ROUTE,
        RUNS_ROUTE,
        RUN_DETAIL_ROUTE,
        ACTIVITY_EVENTS_ROUTE,
        ACTIVITY_EVENT_ROUTE,
        ACTIVITY_SUMMARY_ROUTE,
    }
    routes = {route["routeContractKey"]: route for route in projection["routes"]}
    assert {
        key: (
            routes[key]["gatewayBinding"]["path"],
            routes[key]["servicePepBinding"]["path"],
        )
        for key in (
            ACTIVITY_EVENTS_ROUTE,
            ACTIVITY_EVENT_ROUTE,
            ACTIVITY_SUMMARY_ROUTE,
            RUNS_ROUTE,
            RUN_DETAIL_ROUTE,
        )
    } == {
        ACTIVITY_EVENTS_ROUTE: (
            "/api/agent/v1/activity/events",
            "/v1/activity/events",
        ),
        ACTIVITY_EVENT_ROUTE: (
            "/api/agent/v1/activity/events/{eventId}",
            "/v1/activity/events/{eventId}",
        ),
        ACTIVITY_SUMMARY_ROUTE: (
            "/api/agent/v1/activity/executions/summary",
            "/v1/activity/executions/summary",
        ),
        RUNS_ROUTE: ("/api/agent/v1/runs", "/v1/runs"),
        RUN_DETAIL_ROUTE: ("/api/agent/v1/runs/{runId}", "/v1/runs/{runId}"),
    }


def test_v6_draft_contract_closes_actual_dwaion_state_change_routes() -> None:
    projection = json.loads(
        (ROOT / "contracts/product-authorization/dwaion-pep-v6.draft.json").read_text()
    )

    assert projection["registryRef"] == {
        "bundleKey": "product-surfaces",
        "version": 6,
        "bundleStatus": "DRAFT",
        "checksum": "7cf8602aa2da5f7a0464b23cfd84a8f381e2d3eb85333ed8a8e483865b2b0abe",
    }
    assert projection["activation"] == {
        "enabledByDefault": False,
        "readinessEnvironment": "DWP_AGENT_PRODUCT_AUTHORIZATION_V6_ENABLED",
    }
    routes = {route["routeContractKey"]: route for route in projection["routes"]}
    assert {
        key: (
            routes[key]["gatewayBinding"]["method"],
            routes[key]["gatewayBinding"]["path"],
            routes[key]["servicePepBinding"]["path"],
            routes[key]["accessContractKey"],
        )
        for key in (
            ASK_STREAM_ROUTE,
            CONVERSATION_RENAME_ROUTE,
            CONVERSATION_DELETE_ROUTE,
        )
    } == {
        ASK_STREAM_ROUTE: (
            "POST",
            "/api/agent/v1/ask/stream",
            "/v1/ask/stream",
            "dwaion.work.ask.execute",
        ),
        CONVERSATION_RENAME_ROUTE: (
            "PATCH",
            "/api/agent/v1/conversations/{conversationId}",
            "/v1/conversations/{conversationId}",
            "dwaion.work.ask.execute",
        ),
        CONVERSATION_DELETE_ROUTE: (
            "DELETE",
            "/api/agent/v1/conversations/{conversationId}",
            "/v1/conversations/{conversationId}",
            "dwaion.work.ask.execute",
        ),
    }
    assert len(routes) == 88
    assert len([route for route in routes.values() if route["routeKind"] == "ACTION"]) == 52
    assert set(routes) == {spec["route_contract_key"] for spec in ROUTE_BINDING_SPECS}


def test_openapi_documents_conditional_revision_for_governed_state_changes() -> None:
    schema = main_module.app.openapi()

    for path, method in (
        ("/v1/ask", "post"),
        ("/v1/ask/stream", "post"),
        ("/v1/conversations/{conversation_id}", "patch"),
        ("/v1/conversations/{conversation_id}", "delete"),
    ):
        matching = [
            parameter
            for parameter in schema["paths"][path][method]["parameters"]
            if parameter.get("in") == "header"
            and parameter.get("name") == EXPECTED_REVISION_HEADER
        ]
        assert len(matching) == 1
        assert matching[0]["required"] is False
        assert "110/111" in matching[0]["description"]
        assert "000/100" in matching[0]["description"]
        nullable_string = next(
            item for item in matching[0]["schema"]["anyOf"] if item.get("type") == "string"
        )
        assert nullable_string["minLength"] == 1
        assert nullable_string["maxLength"] == 200


def test_openapi_documents_every_registered_dwaion_action() -> None:
    schema = main_module.app.openapi()
    for spec in ROUTE_BINDING_SPECS:
        if spec["route_kind"] != "ACTION":
            continue
        expected_shape = _path_shape(spec["path_template"])
        paths = [path for path in schema["paths"] if _path_shape(path) == expected_shape]
        assert len(paths) == 1, spec
        operation = schema["paths"][paths[0]][spec["method"].lower()]
        matching = [
            parameter
            for parameter in operation["parameters"]
            if parameter.get("in") == "header"
            and parameter.get("name") == EXPECTED_REVISION_HEADER
        ]
        assert len(matching) == 1, spec


def test_v6_high_risk_mutation_bindings_reject_stale_and_wrong_scopes() -> None:
    proposal_id = "50000000-0000-4000-8000-000000000017"
    artifact_id = "60000000-0000-4000-8000-000000000017"
    evaluation_id = "70000000-0000-4000-8000-000000000017"
    management_scope = scope_key(
        TENANT_ID,
        USER_ID,
        surface_key="dwaion.management",
        source="APP_RESOURCE_SET:RS_DWAION",
        kind="RESOURCE_SET",
    )
    cases = (
        (
            "POST",
            f"/v1/proposals/{proposal_id}/decisions",
            "route.dwaion.work.proposal-decision.action",
            self_scope_key(TENANT_ID, USER_ID),
            "APP.ASK:VIEW",
        ),
        (
            "POST",
            f"/v1/artifacts/{artifact_id}/exports",
            "route.dwaion.work.artifact-export.action",
            self_scope_key(TENANT_ID, USER_ID),
            "APP.ASK:VIEW,APP.DWAION_ARTIFACTS:EXPORT",
        ),
        (
            "PATCH",
            f"/v1/admin/evaluations/{evaluation_id}/lifecycle",
            "route.dwaion.management.evaluation-lifecycle.action",
            management_scope,
            "ADMIN.DWAION_EVALUATION:MANAGE",
        ),
        (
            "POST",
            "/v1/admin/gates/PRODUCTION_READINESS/decision",
            "route.dwaion.management.gate-decision.action",
            management_scope,
            "ADMIN.DWAION_GATES:APPROVE",
        ),
    )
    for method, path, route, selected_scope, permissions in cases:
        headers = {
            **_headers(route, scope=selected_scope, expected_revision=STALE_REVISION),
            "X-DWP-Permissions": permissions,
        }
        response = _request(method, path, headers=headers, json_body={})
        assert response.status_code == 409, (method, path, response.text)

    wrong_work_scope = _request(
        "POST",
        f"/v1/proposals/{proposal_id}/decisions",
        headers={
            **_headers(
                "route.dwaion.work.proposal-decision.action",
                scope=management_scope,
            ),
            "X-DWP-Permissions": "APP.ASK:VIEW",
        },
        json_body={},
    )
    wrong_management_scope = _request(
        "POST",
        "/v1/admin/gates/PRODUCTION_READINESS/decision",
        headers={
            **_headers(
                "route.dwaion.management.gate-decision.action",
                scope=self_scope_key(TENANT_ID, USER_ID),
            ),
            "X-DWP-Permissions": "ADMIN.DWAION_GATES:APPROVE",
        },
        json_body={},
    )
    assert wrong_work_scope.status_code == 403
    assert wrong_management_scope.status_code == 403


def _path_shape(path: str) -> str:
    import re

    return re.sub(r"\{[^{}]+\}", "{}", path)


def test_malformed_candidate_and_rollout_evidence_fail_closed() -> None:
    malformed_candidate = _request(
        "GET",
        "/v1/conversations/not-a-canonical-uuid",
        headers=_headers(DATA_ROUTE),
    )
    malformed_rollout = _request(
        "GET",
        "/v1/conversations",
        headers={**_headers(PAGE_ROUTE), "X-DWP-Rollout-State": " 110"},
    )
    malformed_permissions = _request(
        "GET",
        "/v1/conversations",
        headers={
            **_headers(PAGE_ROUTE),
            "X-DWP-Permissions": "APP.ASK:VIEW, APP.CALENDAR:VIEW",
        },
    )

    assert malformed_candidate.status_code == 503
    assert malformed_rollout.status_code == 503
    assert malformed_permissions.status_code == 403


def test_candidate_scope_is_exact_and_leaves_legacy_siblings_ungoverned() -> None:
    assert owns_candidate("POST", "/v1/ask") is True
    assert owns_candidate("GET", "/v1/conversations") is True
    assert owns_candidate("GET", "/v1/conversations/not-a-uuid") is True
    assert resolve_binding("GET", "/v1/conversations/not-a-uuid") is None
    assert owns_candidate("GET", "/v1/runs") is True
    assert owns_candidate("GET", f"/v1/runs/{RUN_ID}") is True
    assert owns_candidate("GET", "/v1/activity/events") is True
    assert owns_candidate("GET", f"/v1/activity/events/{RUN_ID}") is True
    assert owns_candidate("GET", "/v1/activity/executions/summary") is True
    assert resolve_binding("GET", "/v1/activity/events/not-a-uuid") is None
    assert resolve_binding("GET", "/v1/runs/not-a-uuid") is None
    assert owns_candidate("POST", "/v1/ask/stream") is True
    assert resolve_binding("POST", "/v1/ask/stream").route_contract_key == ASK_STREAM_ROUTE
    assert owns_candidate("PATCH", f"/v1/conversations/{CONVERSATION_ID}") is True
    assert (
        resolve_binding("PATCH", f"/v1/conversations/{CONVERSATION_ID}").route_contract_key
        == CONVERSATION_RENAME_ROUTE
    )
    assert owns_candidate("DELETE", f"/v1/conversations/{CONVERSATION_ID}") is True
    assert (
        resolve_binding("DELETE", f"/v1/conversations/{CONVERSATION_ID}").route_contract_key
        == CONVERSATION_DELETE_ROUTE
    )
    assert owns_candidate("PATCH", "/v1/conversations/not-a-uuid") is True
    assert resolve_binding("PATCH", "/v1/conversations/not-a-uuid") is None


def _headers(
    route: str,
    *,
    scope: str | None = None,
    access_mode: str = "NORMAL",
    expected_revision: str = DECISION_REVISION,
) -> dict[str, str]:
    return {
        "X-DWP-Service-Token": SERVICE_TOKEN,
        "X-DWP-User-ID": str(USER_ID),
        "X-DWP-Tenant-ID": str(TENANT_ID),
        "X-DWP-Correlation-ID": "correlation-product-surface",
        "X-Correlation-ID": "correlation-product-surface",
        "X-DWP-Permissions": "APP.ASK:VIEW,APP.ACTIVITY:VIEW,APP.CALENDAR:VIEW",
        "X-DWP-Roles": "WORKSPACE_MEMBER",
        "X-DWP-Identity-Plane": "TENANT",
        "X-DWP-Rollout-State": "110",
        "X-DWP-Rollout-Revision": ROLLOUT_REVISION,
        "X-DWP-Rollout-Cohort": "full",
        "X-DWP-Route-Contract-Key": route,
        "X-DWP-Context-Key": CONTEXT_KEY,
        "X-DWP-Context-Scope-Key": scope or self_scope_key(TENANT_ID, USER_ID),
        "X-DWP-Active-Access-Mode": access_mode,
        "X-DWP-Current-Decision-Revision": DECISION_REVISION,
        "X-DWP-Current-Revalidate-At": "2099-01-01T00:00:00Z",
        "X-DWP-Expected-Decision-Revision": expected_revision,
    }


def _request(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    json_body: dict | None = None,
):
    async def execute():
        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, headers=headers, json=json_body)

    return asyncio.run(execute())


def _fail_if_store_is_reached(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def reached():
        calls.append("store")
        raise AssertionError("The public controller must not run after owner PEP denial.")

    monkeypatch.setattr(main_module, "get_conversation_store", reached)
    return calls


def _foreign_scope() -> str:
    material = (
        f"{TENANT_ID}\n{USER_ID}\n{PRODUCT_KEY}\n{SURFACE_KEY}"
        "\nTARGET_POPULATION\nTARGET_POPULATION"
    ).encode()
    import hashlib

    return "scope-" + hashlib.sha256(material).hexdigest()[:32]


def _ask_response(request_id: str, correlation_id: str) -> AskResponse:
    return AskResponse(
        run_id="d6df3a1c-326d-45d1-a3ac-4dcdd8ed694a",
        audit_id="audit-product-surface",
        request_id=request_id,
        correlation_id=correlation_id,
        state="COMPLETED",
        answer="오늘의 우선순위를 정리했습니다.",
        confidence=AnswerConfidence.HIGH,
        citations=[
            AskCitation(
                source_id="src-01",
                source_type=CitationSourceType.CALENDAR,
                title="Work queue",
                source_system="DWP Work",
            )
        ],
        source_count=1,
        policy=AskPolicyDecision(
            outcome=PolicyOutcome.ALLOW,
            risk_tier=RiskTier.L1,
            code="ASK_READ_ALLOWED",
            explanation="Read-only query is permitted.",
            model_allowed=True,
        ),
        model_route=AskModelRoute(
            state=ModelRouteState.COMPLETED,
            provider="OPENAI",
            model="gpt-test",
        ),
        agent_registry=AgentRegistryResolution(
            entry_key="DWP_ASSISTANT",
            revision=1,
            artifact_version="test",
            risk_tier=RegistryRiskTier.MEDIUM,
            resolution=RegistryResolutionStatus.ACTIVE,
        ),
        status_code="ANSWER_GROUNDED",
        completed_at=datetime.now(timezone.utc),
    )
