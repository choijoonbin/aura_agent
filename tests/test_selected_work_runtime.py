from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest

from dwp_agent import ask_runtime as runtime_module
from dwp_agent.ask_runtime import AskRuntime
from dwp_agent.context_broker import WorkspaceContextBroker
from dwp_agent.contracts import AgentRegistryResolution, AnswerConfidence
from dwp_agent.contracts import AskRequest
from dwp_agent.conversation_store import ConversationNotFound
from dwp_agent.model_gateway import ModelAnswer
from dwp_agent.policy import SafetyControls
from dwp_agent.run_store import InMemoryRunStore
from dwp_agent.stream_runtime import stream_ask_response, shutdown_ask_stream_pool

from test_selected_work_context import AUTHORIZATION, IDENTITY, me, owner, selected_request


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv("DWP_AGENT_PRIVACY_HASH_SECRET", "selected-work-test-only")
    monkeypatch.setenv("DWP_AGENT_SAFETY_SECRET", "selected-work-test-only")
    monkeypatch.setattr(runtime_module, "resolve_agent", lambda key, **_: AgentRegistryResolution(
        entry_key=key, revision=1, artifact_version="selected-work-v1",
        risk_tier="MEDIUM", resolution="ACTIVE",
    ))
    monkeypatch.setattr(runtime_module, "record_ask_run", lambda *_, **__: None)
    monkeypatch.setattr(runtime_module, "record_ask_failure", lambda *_, **__: None)


class Model:
    model = "test-model"

    def __init__(self, after_generate=lambda: None):
        self.calls = 0
        self.after_generate = after_generate

    def generate(self, query, *, context, **_):
        self.calls += 1
        assert len(context.sources) == 1
        assert "OWNER_METADATA_ONLY" in context.model_evidence()
        self.after_generate()
        return ModelAnswer(
            answer="Review the proposed changes. The original document was not supplied [src-01].",
            cited_source_ids=("src-01",), confidence=AnswerConfidence.LOW,
            abstain_reason=None, provider="OPENAI", model=self.model,
            input_tokens=40, output_tokens=15, total_tokens=55, latency_ms=10,
            provider_request_hash="a" * 64,
        )


def runtime_with_owner(source="PERSONAL_TASK", after_generate=lambda state: None):
    state = {"status": 200, "me": me(), "owner": owner(source), "reads": []}

    def handle(request):
        state["reads"].append(request.url.path)
        principal = request.url.path == "/api/auth/me"
        if principal:
            assert request.url.params.get("permissionPrefix", "").startswith("APP.ASK,")
        return httpx.Response(200 if principal else state["status"], json={
            "data": state["me"] if principal else state["owner"],
        })

    model = Model(lambda: after_generate(state))
    runtime = AskRuntime(
        context_broker=WorkspaceContextBroker(
            gateway_url="http://gateway.test", transport=httpx.MockTransport(handle),
        ), model_gateway=model, run_store=InMemoryRunStore(),
    )
    return runtime, model, state


@pytest.mark.parametrize("source", ["PERSONAL_TASK", "SERVICE_REQUEST", "APPROVAL_TASK", "APPROVAL_REQUEST"])
def test_selected_owner_response_persists_and_continues_by_conversation_id(source):
    runtime, model, state = runtime_with_owner(source)
    request = selected_request(source)
    result = runtime.answer(request, identity=IDENTITY, workspace_authorization=AUTHORIZATION)
    assert result.state == "COMPLETED"
    assert result.policy.mutation_allowed is False
    assert result.conversation_id is not None
    detail = runtime.conversation_store.get(
        tenant_id=IDENTITY.tenant_id, user_id=IDENTITY.user_id, conversation_id=result.conversation_id,
    )
    assert len(detail.messages) == 2
    assert detail.messages[-1].content == result.answer
    assert detail.messages[-1].agent_key == request.agent_key
    assert detail.messages[-1].selected_work == request.page_context.selected_work
    assert len(state["reads"]) == 4  # session + exact owner before and after generation
    continued = runtime.answer(
        request.model_copy(update={"request_id": "follow-up", "conversation_id": result.conversation_id}),
        identity=IDENTITY, workspace_authorization=AUTHORIZATION,
    )
    assert continued.conversation_id == result.conversation_id
    assert model.calls == 2


def test_selected_conversation_restores_exact_scope_without_browser_context():
    runtime, model, state = runtime_with_owner("APPROVAL_TASK")
    result = runtime.answer(selected_request("APPROVAL_TASK"), identity=IDENTITY,
                            workspace_authorization=AUTHORIZATION)
    followup = AskRequest(request_id="resume-selected", query="Which original evidence is missing?",
        locale="en", agent_key="DWP_APPROVAL_EXPERT", conversation_id=result.conversation_id,
        source_scopes=["APPROVAL_TASK", "APPROVAL_REQUEST"])
    continued = runtime.answer(followup, identity=IDENTITY, workspace_authorization=AUTHORIZATION)
    assert continued.conversation_id == result.conversation_id
    assert continued.selected_work == result.selected_work
    assert model.calls == 2 and len(state["reads"]) == 8


@pytest.mark.parametrize("drift", ["agent", "source"])
def test_selected_conversation_rejects_agent_or_source_switch_before_model(drift):
    runtime, model, state = runtime_with_owner("APPROVAL_TASK")
    request = selected_request("APPROVAL_TASK")
    result = runtime.answer(request, identity=IDENTITY, workspace_authorization=AUTHORIZATION)
    followup = request.model_copy(update={"request_id": "drift", "conversation_id": result.conversation_id})
    if drift == "agent":
        followup = followup.model_copy(update={"agent_key": "DWP_ASSISTANT"})
    else:
        selection = request.page_context.selected_work.model_copy(update={"source_reference": uuid4()})
        followup = followup.model_copy(update={"page_context": request.page_context.model_copy(
            update={"selected_work": selection})})
    with pytest.raises(ConversationNotFound):
        runtime.answer(followup, identity=IDENTITY, workspace_authorization=AUTHORIZATION)
    assert model.calls == 1 and len(state["reads"]) == 4


@pytest.mark.parametrize(("change", "code"), [
    (lambda state: state["owner"].update(version=4), "STALE"),
    (lambda state: state.update(status=403), "FORBIDDEN"),
    (lambda state: state.update(status=404), "NOT_FOUND"),
    (lambda state: state.update(status=503), "UNAVAILABLE"),
    (lambda state: state["me"].update(permissions=[]), "FORBIDDEN"),
])
def test_changed_source_or_revoked_access_during_model_never_publishes_late_answer(change, code):
    runtime, model, _ = runtime_with_owner(after_generate=change)
    result = runtime.answer(selected_request(), identity=IDENTITY, workspace_authorization=AUTHORIZATION)
    assert model.calls == 1
    assert result.state == "ABSTAINED"
    assert result.status_code == f"SELECTED_WORK_{code}"
    assert result.answer is None and result.citations == []
    assert result.model_route.total_tokens == 55  # truthful accounting of the already-spent call
    detail = runtime.conversation_store.get(
        tenant_id=IDENTITY.tenant_id, user_id=IDENTITY.user_id, conversation_id=result.conversation_id,
    )
    assert all("Review the proposed changes" not in message.content for message in detail.messages)


def test_success_replay_rechecks_authority_without_regenerating_or_restoring_a_revoked_answer():
    runtime, model, state = runtime_with_owner()
    request = selected_request()
    first = runtime.answer(request, identity=IDENTITY, workspace_authorization=AUTHORIZATION)
    assert first.state == "COMPLETED"
    state["status"] = 403
    replay = runtime.answer(request, identity=IDENTITY, workspace_authorization=AUTHORIZATION)
    assert replay.state == "ABSTAINED"
    assert replay.answer is None and replay.citations == []
    assert model.calls == 1


def test_missing_or_stale_source_does_not_invoke_model():
    runtime, model, state = runtime_with_owner()
    state["owner"]["version"] = 4
    result = runtime.answer(selected_request(), identity=IDENTITY, workspace_authorization=AUTHORIZATION)
    assert result.status_code == "SELECTED_WORK_STALE"
    assert model.calls == 0


def test_selected_work_stream_emits_governed_progress_and_one_correlated_result():
    runtime, _, _ = runtime_with_owner()
    response = stream_ask_response(
        request=selected_request(), identity=IDENTITY, runtime=runtime,
        safety_controls=SafetyControls(), workspace_authorization=AUTHORIZATION,
        encode_event=lambda event, payload: (event, payload), error_code=lambda _: "FAILED",
    )

    async def consume():
        return [frame async for frame in response.body_iterator]

    try:
        events = asyncio.run(consume())
    finally:
        shutdown_ask_stream_pool()
    assert not [payload for event, payload in events if event == "error"]
    stages = [payload["stage"] for event, payload in events if event == "progress"]
    assert stages == ["AUTHORIZING", "RETRIEVING", "REASONING", "VERIFYING", "PERSISTING", "COMPLETED"]
    results = [payload["data"] for event, payload in events if event == "result"]
    assert len(results) == 1
    assert results[0]["requestId"] == "selected-work-1"
    assert results[0]["correlationId"] == IDENTITY.correlation_id
    assert results[0]["conversationId"]


@pytest.mark.integration
def test_selected_work_revalidation_and_conversation_are_durable_in_postgres(monkeypatch):
    from psycopg import connect
    from dwp_agent.envelope import load_payload_encryption
    from dwp_agent.postgres_conversation_store import PostgresConversationStore
    from dwp_agent.run_store import PostgresRunStore, _apply_migrations

    database_url = os.getenv("DWP_AGENT_INTEGRATION_DATABASE_URL", "")
    if not database_url:
        pytest.skip("DWP_AGENT_INTEGRATION_DATABASE_URL is not configured.")
    assert urlparse(database_url).path.endswith(("_test", "_integration"))
    monkeypatch.setenv("DWP_AGENT_KEY_PROVIDER", "local-inline")
    monkeypatch.setenv("DWP_AGENT_DATA_KEY", "ZHdwLWxvY2FsLWFnZW50LWRhdGEta2V5LTMyYnl0ZSE=")
    monkeypatch.setenv("DWP_AGENT_DATA_KEY_VERSION", "selected-work-test-v1")
    _apply_migrations(database_url)
    tenant = str(800_000_000 + uuid4().int % 100_000_000)
    identity = replace(IDENTITY, tenant_id=tenant)
    with connect(database_url) as connection:
        connection.execute(
            "INSERT INTO ai_conversation_retention_policies (tenant_id, retention_days) VALUES (%s, 90)",
            (int(tenant),),
        )
    runtime, model, state = runtime_with_owner("APPROVAL_TASK")
    state["me"]["tenantId"] = int(tenant)
    encryption = load_payload_encryption()
    runtime.run_store = PostgresRunStore(database_url, encryption)
    runtime.conversation_store = PostgresConversationStore(database_url, encryption)
    request = selected_request("APPROVAL_TASK")
    first = runtime.answer(request, identity=identity, workspace_authorization=AUTHORIZATION)
    assert first.state == "COMPLETED"
    assert first.conversation_id is not None
    state["status"] = 403
    replay = runtime.answer(request, identity=identity, workspace_authorization=AUTHORIZATION)
    assert replay.answer is None and replay.status_code == "SELECTED_WORK_FORBIDDEN"
    assert model.calls == 1
    detail = runtime.conversation_store.get(
        tenant_id=tenant, user_id=identity.user_id, conversation_id=first.conversation_id,
    )
    assert len(detail.messages) == 2
    assert detail.messages[-1].content == first.answer
    assert detail.messages[-1].selected_work == request.page_context.selected_work
    assert detail.messages[-1].agent_key == "DWP_APPROVAL_EXPERT"
    # Historical authorized evidence remains under the conversation retention policy;
    # replay does not rewrite history or publish it as a currently authorized response.
    with connect(database_url) as connection:
        payload = connection.execute(
            "SELECT response_envelope FROM ai_agent_runs WHERE run_id = %s", (first.run_id,),
        ).fetchone()[0]
        assert payload.startswith("dwp2.") and first.answer not in payload
