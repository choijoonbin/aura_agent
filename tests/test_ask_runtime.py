from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from dwp_agent import ask_runtime as ask_runtime_module
from dwp_agent.approval_context import approval_tasks
from dwp_agent.ask_runtime import AskRuntime
from dwp_agent.context_broker import GroundedContext, GroundedSource, WorkspaceContextBroker
from dwp_agent.conversation_store import InMemoryConversationStore
from dwp_agent.contracts import (
    AgentRegistryResolution,
    AnswerConfidence,
    AskCitation,
    AskRequest,
    CitationSourceType,
    RegistryResolutionStatus,
    RegistryRiskTier,
)
from dwp_agent.model_gateway import (
    GroundingViolation,
    ModelAnswer,
    ModelCallFailed,
    OpenAIResponsesGateway,
    _system_instruction,
)
from dwp_agent.policy import AskIdentity, evaluate_ask_policy
from dwp_agent.run_store import (
    InMemoryRunStore,
    PayloadCipher,
    RequestIdConflict,
    RunStart,
    RunInProgress,
    RunStoreUnavailable,
)


ACTIVE_AGENT = AgentRegistryResolution(
    entry_key="DWP_ASSISTANT",
    revision=1,
    artifact_version="ask-runtime-v1",
    risk_tier=RegistryRiskTier.MEDIUM,
    resolution=RegistryResolutionStatus.ACTIVE,
)


def identity(*permissions: str) -> AskIdentity:
    return AskIdentity(
        tenant_id="1",
        user_id="7",
        roles=("WORKSPACE_MEMBER",),
        permissions=tuple(permissions),
        correlation_id="correlation-ask-1",
    )


def context() -> GroundedContext:
    return GroundedContext(
        sources=(
            GroundedSource(
                citation=AskCitation(
                    source_id="src-01",
                    source_type=CitationSourceType.WORK_ITEM,
                    title="Approve software access request",
                    source_system="IT Service",
                    route="/work?item=WK-1042",
                    occurred_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
                ),
                evidence="Priority high. The request blocks a new team member.",
                rank=1,
            ),
        ),
        attempted_sources=("WORK_ITEM",),
        unavailable_sources=(),
    )


class FakeBroker:
    def __init__(self) -> None:
        self.calls = 0
        self.agent_key: str | None = None

    def collect(self, *_args, **_kwargs) -> GroundedContext:
        self.calls += 1
        self.agent_key = _kwargs.get("agent_key")
        return context()


class FakeModel:
    model = "gpt-test"

    def __init__(self) -> None:
        self.calls = 0
        self.agent_key: str | None = None

    def generate(self, *_args, **_kwargs) -> ModelAnswer:
        self.calls += 1
        self.agent_key = _kwargs.get("agent_key")
        return ModelAnswer(
            answer="The software access approval is blocking a new team member.",
            cited_source_ids=("src-01",),
            confidence=AnswerConfidence.HIGH,
            abstain_reason=None,
            provider="OPENAI",
            model="gpt-test-2026-08-01",
            input_tokens=120,
            output_tokens=28,
            total_tokens=148,
            latency_ms=240,
            provider_request_hash="a" * 64,
        )


class FailingAzureModel:
    model = "gpt-5"
    provider_label = "AZURE_OPENAI"

    def generate(self, *_args, **_kwargs) -> ModelAnswer:
        raise ModelCallFailed("MODEL_PROVIDER_UNAVAILABLE")


class AlwaysContendedRunStore(InMemoryRunStore):
    def begin(self, _start: RunStart):
        return None


@pytest.fixture(autouse=True)
def runtime_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_PRIVACY_HASH_SECRET", "test-privacy-secret")
    monkeypatch.setenv("DWP_AGENT_SAFETY_SECRET", "test-safety-secret")
    monkeypatch.setattr(ask_runtime_module, "resolve_agent", lambda *_args, **_kwargs: ACTIVE_AGENT)


def test_grounded_answer_is_idempotent_and_citation_scoped() -> None:
    broker = FakeBroker()
    model = FakeModel()
    runtime = AskRuntime(
        context_broker=broker,
        model_gateway=model,
        run_store=InMemoryRunStore(),
    )
    request = AskRequest(
        request_id="request-ask-1",
        query="What is blocking my urgent work?",
        locale="en",
    )
    verified_identity = identity("APP.ASK:VIEW", "APP.WORK:VIEW")

    first = runtime.answer(request, identity=verified_identity)
    replay = runtime.answer(request, identity=verified_identity)

    assert first == replay
    assert first.state == "COMPLETED"
    assert first.status_code == "ANSWER_GROUNDED"
    assert first.answer is not None
    assert [citation.source_id for citation in first.citations] == ["src-01"]
    assert first.model_route.total_tokens == 148
    assert broker.calls == 1
    assert model.calls == 1


def test_contended_request_does_not_create_an_orphan_conversation() -> None:
    run_store = AlwaysContendedRunStore()
    conversation_store = InMemoryConversationStore(run_store)
    runtime = AskRuntime(
        context_broker=FakeBroker(),
        model_gateway=FakeModel(),
        run_store=run_store,
        conversation_store=conversation_store,
    )

    with pytest.raises(RunInProgress):
        runtime.answer(
            AskRequest(
                request_id="request-conversation-contended",
                query="What is blocking my urgent work?",
                locale="en",
            ),
            identity=identity("APP.ASK:VIEW", "APP.WORK:VIEW"),
        )

    assert conversation_store.list(tenant_id="1", user_id="7") == []


def test_model_failure_preserves_azure_provider_in_audit_route() -> None:
    runtime = AskRuntime(
        context_broker=FakeBroker(),
        model_gateway=FailingAzureModel(),
        run_store=InMemoryRunStore(),
    )

    response = runtime.answer(
        AskRequest(
            request_id="request-azure-failure",
            query="What is blocking my urgent work?",
            locale="en",
        ),
        identity=identity("APP.ASK:VIEW", "APP.WORK:VIEW"),
    )

    assert response.state == "ABSTAINED"
    assert response.status_code == "MODEL_PROVIDER_UNAVAILABLE"
    assert response.model_route.provider == "AZURE_OPENAI"


def test_approval_expert_is_permission_gated_and_forwarded_to_runtime_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expert = ACTIVE_AGENT.model_copy(update={"entry_key": "DWP_APPROVAL_EXPERT"})
    monkeypatch.setattr(ask_runtime_module, "resolve_agent", lambda *_args, **_kwargs: expert)
    broker = FakeBroker()
    model = FakeModel()
    runtime = AskRuntime(
        context_broker=broker,
        model_gateway=model,
        run_store=InMemoryRunStore(),
    )

    denied = runtime.answer(
        AskRequest(
            request_id="request-approval-expert-denied",
            query="Explain my pending approvals",
            locale="en",
            agent_key="DWP_APPROVAL_EXPERT",
        ),
        identity=identity("APP.ASK:VIEW"),
    )
    allowed = runtime.answer(
        AskRequest(
            request_id="request-approval-expert-allowed",
            query="Explain my pending approvals",
            locale="en",
            agent_key="DWP_APPROVAL_EXPERT",
        ),
        identity=identity(
            "APP.ASK:VIEW",
            "APP.APPROVALS:VIEW",
            "ACTION.APPROVAL_TASK:VIEW",
        ),
    )

    assert denied.status_code == "APPROVAL_EXPERT_PERMISSION_REQUIRED"
    assert denied.model_route.state == "NOT_INVOKED"
    assert allowed.state == "COMPLETED"
    assert allowed.conversation_id is None
    assert broker.agent_key == "DWP_APPROVAL_EXPERT"
    assert model.agent_key == "DWP_APPROVAL_EXPERT"


def test_reused_request_id_with_another_query_fails_closed() -> None:
    broker = FakeBroker()
    model = FakeModel()
    runtime = AskRuntime(
        context_broker=broker,
        model_gateway=model,
        run_store=InMemoryRunStore(),
    )
    verified_identity = identity("APP.ASK:VIEW", "APP.WORK:VIEW")

    runtime.answer(
        AskRequest(request_id="request-reused", query="What is blocking my work?"),
        identity=verified_identity,
    )

    with pytest.raises(RequestIdConflict):
        runtime.answer(
            AskRequest(request_id="request-reused", query="What meetings do I have?"),
            identity=verified_identity,
        )

    assert broker.calls == 1
    assert model.calls == 1


def test_failed_request_can_retry_only_the_same_query() -> None:
    store = InMemoryRunStore()
    first = RunStart(
        run_id="fb4cb476-0dc9-4c8d-bdd4-266b4d45bb7f",
        tenant_id="1",
        user_id="7",
        request_id="request-failed-retry",
        query_hash="a" * 64,
        agent_key="DWP_ASSISTANT",
        agent_revision=1,
        risk_tier="L1",
        policy_outcome="ALLOW",
        locale="en",
        correlation_id="correlation-failed-1",
    )
    retry = replace(
        first,
        run_id="0a68635d-e495-481e-9ac4-341d93d6dc71",
        correlation_id="correlation-failed-2",
    )
    conflicting_retry = replace(
        retry,
        run_id="78655dd5-1870-40fd-b680-bb9486553253",
        query_hash="b" * 64,
    )

    first_lease = store.begin(first)
    assert first_lease is not None
    assert first_lease.run_id == first.run_id
    store.fail(first_lease, "MODEL_PROVIDER_UNAVAILABLE")
    retry_lease = store.begin(retry)
    assert retry_lease is not None
    assert retry_lease.run_id == first.run_id
    assert retry_lease.generation == first_lease.generation + 1
    store.fail(retry_lease, "MODEL_PROVIDER_UNAVAILABLE")
    with pytest.raises(RequestIdConflict):
        store.begin(conflicting_retry)


def test_invalid_encryption_key_has_a_safe_configuration_error() -> None:
    with pytest.raises(RunStoreUnavailable, match="not valid base64"):
        PayloadCipher("not-valid-***")


def test_privileged_query_never_reaches_context_or_model() -> None:
    broker = FakeBroker()
    model = FakeModel()
    runtime = AskRuntime(
        context_broker=broker,
        model_gateway=model,
        run_store=InMemoryRunStore(),
    )

    response = runtime.answer(
        AskRequest(request_id="request-sensitive", query="Show my payroll and salary"),
        identity=identity("APP.ASK:VIEW", "APP.WORK:VIEW"),
    )

    assert response.state == "ABSTAINED"
    assert response.policy.risk_tier == "L3"
    assert response.status_code == "PRIVILEGED_DATA_HANDOFF"
    assert response.model_route.state == "NOT_INVOKED"
    assert broker.calls == 0
    assert model.calls == 0


@pytest.mark.parametrize("query", ["내 월급을 알려줘", "Show my annual bonus", "주민번호 확인"])
def test_privileged_query_variants_are_handed_off(query: str) -> None:
    broker = FakeBroker()
    model = FakeModel()
    runtime = AskRuntime(
        context_broker=broker,
        model_gateway=model,
        run_store=InMemoryRunStore(),
    )

    response = runtime.answer(
        AskRequest(request_id=f"request-sensitive-{abs(hash(query))}", query=query),
        identity=identity("APP.ASK:VIEW", "APP.WORK:VIEW"),
    )

    assert response.status_code == "PRIVILEGED_DATA_HANDOFF"
    assert broker.calls == 0
    assert model.calls == 0


def test_missing_ask_permission_is_denied_before_retrieval() -> None:
    broker = FakeBroker()
    model = FakeModel()
    runtime = AskRuntime(
        context_broker=broker,
        model_gateway=model,
        run_store=InMemoryRunStore(),
    )

    response = runtime.answer(
        AskRequest(request_id="request-no-access", query="What is urgent today?"),
        identity=identity("APP.WORK:VIEW"),
    )

    assert response.state == "ABSTAINED"
    assert response.status_code == "ASK_PERMISSION_REQUIRED"
    assert broker.calls == 0
    assert model.calls == 0


def test_ask_audit_excludes_question_answer_and_source_title(
    caplog: pytest.LogCaptureFixture,
) -> None:
    question = "What is blocking my urgent work?"
    answer = "The software access approval is blocking a new team member."
    source_title = "Approve software access request"
    caplog.set_level(logging.INFO, logger="uvicorn.error.dwp.audit")
    runtime = AskRuntime(
        context_broker=FakeBroker(),
        model_gateway=FakeModel(),
        run_store=InMemoryRunStore(),
    )

    response = runtime.answer(
        AskRequest(request_id="request-audit-safe", query=question),
        identity=identity("APP.ASK:VIEW", "APP.WORK:VIEW"),
    )

    event = json.loads(caplog.records[-1].message)
    assert event["type"] == "agent.ask.evaluated"
    assert event["data"]["sourceCount"] == 1
    assert event["data"]["inputTokens"] == 120
    assert response.run_id in event["subject"]
    assert question not in caplog.text
    assert answer not in caplog.text
    assert source_title not in caplog.text


def test_context_broker_preserves_verified_permission_scope() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "items": [
                        {
                            "title": "Review customer briefing",
                            "summary": "Three questions are unanswered.",
                            "status": "IN_PROGRESS",
                            "priority": "HIGH",
                            "sourceSystem": "Microsoft 365",
                            "sourceRoute": "/work?item=WK-1045",
                            "updatedAt": "2026-08-12T01:00:00Z",
                        }
                    ]
                }
            },
        )

    broker = WorkspaceContextBroker(
        platform_url="http://platform.test",
        service_token="runtime-service-token",
        transport=httpx.MockTransport(handler),
    )

    grounded = broker.collect(
        "What is blocking the customer briefing?",
        identity=identity("APP.ASK:VIEW", "APP.WORK:VIEW"),
        locale="en",
    )

    assert len(captured) == 1
    assert captured[0].url.path == "/v1/workspace/work-items"
    assert captured[0].headers["x-dwp-user-id"] == "7"
    assert captured[0].headers["x-dwp-permissions"] == "APP.ASK:VIEW,APP.WORK:VIEW"
    assert len(grounded.sources) == 1
    assert grounded.sources[0].citation.source_id == "src-01"


def test_context_broker_reads_only_permission_scoped_mail_summaries() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "items": [
                        {
                            "threadId": "8a0b5388-8765-4c97-a95c-4037285416d8",
                            "accountName": "SKAX Mail",
                            "subject": "Project review scheduling",
                            "preview": "Could we meet Thursday at 15:00?",
                            "importance": "HIGH",
                            "triageLane": "NEEDS_REPLY",
                            "workflowState": "OPEN",
                            "unread": True,
                            "latestMessageAt": "2026-08-18T09:00:00Z",
                        }
                    ]
                }
            },
        )

    broker = WorkspaceContextBroker(
        platform_url="http://platform.test",
        service_token="runtime-service-token",
        transport=httpx.MockTransport(handler),
    )

    grounded = broker.collect(
        "When is the project review?",
        identity=identity("APP.ASK:VIEW", "APP.MAIL:VIEW"),
        locale="en",
    )

    assert len(captured) == 1
    assert captured[0].url.path == "/v1/mail/threads"
    assert captured[0].url.params["pageSize"] == "50"
    assert grounded.attempted_sources == ("MAIL",)
    assert grounded.sources[0].citation.source_type == CitationSourceType.MAIL
    assert grounded.sources[0].citation.route == (
        "/mail/inbox?thread=8a0b5388-8765-4c97-a95c-4037285416d8"
    )


def test_approval_expert_reads_only_permission_scoped_approval_sources() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/v1/tasks":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "taskId": "3ee1f7ef-eb34-44d5-a153-237f79bb81cc",
                            "requestId": "0fd4362f-1a3f-40b9-8f5a-2791e08e02eb",
                            "requestNumber": "APR-2026-0012",
                            "title": "Software access approval",
                            "summary": "Approve access for a new team member.",
                            "status": "PENDING",
                            "priority": "HIGH",
                            "stepName": "Line manager review",
                            "requesterName": "Mina Kim",
                            "dataClassification": "INTERNAL",
                            "submittedAt": "2026-08-19T01:00:00Z",
                            "dueAt": "2026-08-19T06:00:00Z",
                        }
                    ]
                },
            )
        if request.url.path == "/v1/requests":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "requestId": "0fd4362f-1a3f-40b9-8f5a-2791e08e02eb",
                            "requestNumber": "APR-2026-0012",
                            "title": "Software access approval",
                            "summary": "Waiting for line manager review.",
                            "status": "IN_REVIEW",
                            "priority": "HIGH",
                            "currentStepName": "Line manager review",
                            "dataClassification": "INTERNAL",
                            "submittedAt": "2026-08-19T01:00:00Z",
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected approval path: {request.url.path}")

    broker = WorkspaceContextBroker(
        approval_url="http://approval.test",
        approval_service_token="approval-runtime-token",
        transport=httpx.MockTransport(handler),
    )
    grounded = broker.collect(
        "Which approval needs attention?",
        identity=identity(
            "APP.ASK:VIEW",
            "APP.APPROVALS:VIEW",
            "ACTION.APPROVAL_TASK:VIEW",
            "ACTION.APPROVAL_REQUEST:VIEW",
        ),
        locale="en",
        agent_key="DWP_APPROVAL_EXPERT",
        source_scopes=[
            CitationSourceType.APPROVAL_TASK,
            CitationSourceType.APPROVAL_REQUEST,
        ],
    )

    assert {request.url.path for request in captured} == {"/v1/tasks", "/v1/requests"}
    assert all(
        request.headers["x-dwp-service-token"] == "approval-runtime-token"
        for request in captured
    )
    assert grounded.attempted_sources == ("APPROVAL_TASK", "APPROVAL_REQUEST")
    assert {source.citation.source_type for source in grounded.sources} == {
        CitationSourceType.APPROVAL_TASK,
        CitationSourceType.APPROVAL_REQUEST,
    }
    assert all(
        source.citation.route and source.citation.route.startswith("/approvals/")
        for source in grounded.sources
    )
    request_source = next(
        source
        for source in grounded.sources
        if source.citation.source_type == CitationSourceType.APPROVAL_REQUEST
    )
    assert request_source.citation.route == (
        "/approvals/requests/submitted?request=0fd4362f-1a3f-40b9-8f5a-2791e08e02eb"
    )


def test_approval_expert_excludes_sensitive_or_unclassified_work_items() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "http://approval.test/v1/tasks"),
        json={
            "data": [
                {
                    "taskId": "3ee1f7ef-eb34-44d5-a153-237f79bb81cc",
                    "title": "Internal purchase request",
                    "summary": "Review supplier choice.",
                    "dataClassification": "INTERNAL",
                },
                {
                    "taskId": "0fd4362f-1a3f-40b9-8f5a-2791e08e02eb",
                    "title": "Restricted personnel matter",
                    "summary": "Sensitive employee context.",
                    "dataClassification": "RESTRICTED",
                },
                {
                    "taskId": "de44ba2c-1b37-41c1-b162-e157b68b04bc",
                    "title": "Legacy item without classification",
                    "summary": "Classification is missing.",
                },
            ]
        },
    )

    parsed = approval_tasks(response)

    assert [item["title"] for item in parsed] == ["Internal purchase request"]


def test_approval_expert_instruction_keeps_decisions_human_only() -> None:
    instruction = _system_instruction("ko", "DWP_APPROVAL_EXPERT")

    assert "DWP Approval Expert" in instruction
    assert "Never approve" in instruction
    assert "human-only" in instruction


def test_approval_expert_policy_requires_both_ask_and_approval_access() -> None:
    decision = evaluate_ask_policy(
        "Explain my pending approvals",
        identity("APP.ASK:VIEW"),
        agent_key="DWP_APPROVAL_EXPERT",
    )

    assert decision.outcome == "DENY"
    assert decision.code == "APPROVAL_EXPERT_PERMISSION_REQUIRED"


def test_context_broker_filters_privileged_source_titles_before_model_context() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "items": [
                        {
                            "title": "Annual salary and bonus review",
                            "summary": "Confidential compensation statement.",
                            "sourceSystem": "HR",
                        },
                        {
                            "title": "Customer briefing review",
                            "summary": "Three questions need an owner.",
                            "sourceSystem": "Microsoft 365",
                        },
                    ]
                }
            },
        )

    broker = WorkspaceContextBroker(
        platform_url="http://platform.test",
        service_token="runtime-service-token",
        transport=httpx.MockTransport(handler),
    )

    grounded = broker.collect(
        "What needs my attention?",
        identity=identity("APP.ASK:VIEW", "APP.WORK:VIEW"),
        locale="en",
    )

    assert [source.citation.title for source in grounded.sources] == [
        "Customer briefing review"
    ]
    assert "salary" not in grounded.model_evidence().lower()


def test_model_gateway_uses_non_persistent_structured_output_and_rejects_fake_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"x-request-id": "provider-request-secret"},
            json={
                "model": "gpt-test-2026-08-01",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "answer": "Unsupported answer",
                                        "citedSourceIds": ["src-99"],
                                        "confidence": "HIGH",
                                        "abstainReason": None,
                                    }
                                ),
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        )

    monkeypatch.setenv("DWP_AGENT_ALLOW_TEST_MODEL_URL", "true")
    gateway = OpenAIResponsesGateway(
        api_key="test-key",
        model="gpt-test",
        base_url="http://model.test/v1",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(GroundingViolation, match="MODEL_CITATION_OUT_OF_SCOPE"):
        gateway.generate(
            "What is blocking my work?",
            context=context(),
            locale="en",
            run_id="7ba70ea1-2586-4a28-8e9f-7f320815d380",
            safety_identifier="dwp_test",
        )

    assert captured[0]["store"] is False
    assert captured[0]["max_output_tokens"] == 900
    assert captured[0]["text"] == {
        "format": {
            "type": "json_schema",
            "name": "dwp_grounded_answer",
            "strict": True,
            "schema": captured[0]["text"]["format"]["schema"],
        }
    }


def test_model_gateway_retries_within_one_bounded_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions["timeout"]
        request_timeouts.append(float(timeout["read"]))
        if len(request_timeouts) == 1:
            return httpx.Response(503, headers={"retry-after": "0"}, json={})
        return httpx.Response(400, json={"error": {"code": "invalid_request"}})

    monkeypatch.setenv("DWP_AGENT_ALLOW_TEST_MODEL_URL", "true")
    monkeypatch.setenv("DWP_OPENAI_TIMEOUT_SECONDS", "99")
    gateway = OpenAIResponsesGateway(
        api_key="test-key",
        model="gpt-test",
        base_url="http://model.test/v1",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ModelCallFailed):
        gateway.generate(
            "What is blocking my work?",
            context=context(),
            locale="en",
            run_id="7ba70ea1-2586-4a28-8e9f-7f320815d380",
            safety_identifier="dwp_test",
        )

    assert gateway.timeout_seconds == 24.0
    assert len(request_timeouts) == 2
    assert 0 < request_timeouts[1] <= request_timeouts[0] <= 24.0


def test_azure_model_gateway_uses_v1_endpoint_and_api_key_header() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"apim-request-id": "azure-provider-request-secret"},
            json={
                "model": "dwp-gpt-deployment-2026-08-01",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "answer": "The grounded item is ready [src-01].",
                                        "citedSourceIds": ["src-01"],
                                        "confidence": "HIGH",
                                        "abstainReason": None,
                                    }
                                ),
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        )

    gateway = OpenAIResponsesGateway(
        provider="azure_openai",
        api_key="azure-test-key",
        model="dwp-gpt-deployment",
        base_url="https://dwp-model.openai.azure.com/",
        transport=httpx.MockTransport(handler),
    )

    answer = gateway.generate(
        "What is ready?",
        context=context(),
        locale="en",
        run_id="2ca9b2ac-bdd8-4f62-9e1a-acde76646c98",
        safety_identifier="dwp_test",
    )

    assert str(requests[0].url) == (
        "https://dwp-model.openai.azure.com/openai/v1/responses"
    )
    assert requests[0].headers["api-key"] == "azure-test-key"
    assert "authorization" not in requests[0].headers
    assert answer.provider == "AZURE_OPENAI"
    assert answer.provider_request_hash
    assert "azure-provider-request-secret" not in answer.provider_request_hash


def test_model_gateway_rejects_answer_without_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-test-2026-08-01",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "answer": "Grounded but incomplete contract",
                                        "citedSourceIds": ["src-01"],
                                        "confidence": None,
                                        "abstainReason": None,
                                    }
                                ),
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        )

    monkeypatch.setenv("DWP_AGENT_ALLOW_TEST_MODEL_URL", "true")
    gateway = OpenAIResponsesGateway(
        api_key="test-key",
        model="gpt-test",
        base_url="http://model.test/v1",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(GroundingViolation, match="MODEL_ANSWER_WITHOUT_CONFIDENCE"):
        gateway.generate(
            "What is blocking my work?",
            context=context(),
            locale="en",
            run_id="f319265d-338d-4715-8da2-b3fd93f34537",
            safety_identifier="dwp_test",
        )
