from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from dwp_agent import ask_runtime as ask_runtime_module
from dwp_agent.ask_runtime import AskRuntime
from dwp_agent.context_broker import GroundedContext, GroundedSource, WorkspaceContextBroker
from dwp_agent.contracts import (
    AgentRegistryResolution,
    AnswerConfidence,
    AskCitation,
    AskRequest,
    CitationSourceType,
    RegistryResolutionStatus,
    RegistryRiskTier,
)
from dwp_agent.model_gateway import GroundingViolation, ModelAnswer, OpenAIResponsesGateway
from dwp_agent.policy import AskIdentity
from dwp_agent.run_store import (
    InMemoryRunStore,
    PayloadCipher,
    RequestIdConflict,
    RunStart,
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

    def collect(self, *_args, **_kwargs) -> GroundedContext:
        self.calls += 1
        return context()


class FakeModel:
    model = "gpt-test"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, *_args, **_kwargs) -> ModelAnswer:
        self.calls += 1
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

    assert store.begin(first) is True
    store.fail(first.run_id, "MODEL_PROVIDER_UNAVAILABLE")
    assert store.begin(retry) is True
    store.fail(retry.run_id, "MODEL_PROVIDER_UNAVAILABLE")
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
