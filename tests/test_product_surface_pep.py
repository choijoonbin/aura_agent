from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

import dwp_agent.main as main_module
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
    DATA_ROUTE,
    OWNER_SERVICE,
    PAGE_ROUTE,
    PRODUCT_KEY,
    RESPONSE_REVISION_HEADER,
    SURFACE_KEY,
    owns_candidate,
    resolve_binding,
    self_scope_key,
)


ROOT = Path(__file__).resolve().parents[1]
SERVICE_TOKEN = "test-agent-service-token"
TENANT_ID = 3
USER_ID = 17
CONVERSATION_ID = UUID("20000000-0000-4000-8000-000000000017")
ROLLOUT_REVISION = "rollout-" + "a" * 64
DECISION_REVISION = "psr-" + "b" * 64
STALE_REVISION = "psr-" + "c" * 64
CONTEXT_KEY = "psc-" + "d" * 64


class RuntimeSpy:
    def __init__(self) -> None:
        self.calls = 0

    def answer(self, request, *, identity, safety_controls) -> AskResponse:
        self.calls += 1
        return _ask_response(request.request_id, identity.correlation_id)


@pytest.fixture(autouse=True)
def product_surface_runtime(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V4_ENABLED", "true")
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
        timestamp = datetime.now(timezone.utc)
        self.summary = ConversationSummary(
            conversation_id=CONVERSATION_ID,
            title="오늘 업무",
            locale="ko",
            message_count=1,
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


def test_executes_page_data_action_through_agent_owner_pep() -> None:
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
        headers=_headers(ACTION_ROUTE),
        json_body={"requestId": "request-exact", "query": "오늘 할 일을 알려 주세요."},
    )

    assert page.status_code == 200
    assert data.status_code == 200
    assert action.status_code == 200
    assert runtime.calls == 1
    for response in (page, data, action):
        assert response.headers[RESPONSE_REVISION_HEADER] == DECISION_REVISION


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
    assert owns_candidate("POST", "/v1/ask/stream") is False
    assert owns_candidate("PATCH", f"/v1/conversations/{CONVERSATION_ID}") is False
    assert owns_candidate("DELETE", f"/v1/conversations/{CONVERSATION_ID}") is False


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
        "X-DWP-Permissions": "APP.ASK:VIEW,APP.CALENDAR:VIEW",
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
