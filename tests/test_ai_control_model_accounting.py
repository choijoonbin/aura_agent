from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from dwp_agent.ai_control_runtime import AIRuntimeControl, AIRuntimePlan, AIUsageReservation
from dwp_agent.context_broker import GroundedContext, GroundedSource
from dwp_agent.contracts import AskCitation, CitationSourceType
from dwp_agent.model_gateway import (
    GroundingViolation,
    ModelCallFailed,
    ModelRefused,
    OpenAIResponsesGateway,
)


NOW = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
RUN_ID = "2b1cdd96-52a2-4faf-ac8a-0359f3173a90"


def context() -> GroundedContext:
    return GroundedContext(
        sources=(GroundedSource(
            citation=AskCitation(
                source_id="src-01",
                source_type=CitationSourceType.WORK_ITEM,
                title="Approved source",
                source_system="DWP",
                route="/work/1",
                occurred_at=NOW,
            ),
            evidence="Approved evidence.",
            rank=1,
        ),),
        attempted_sources=("WORK_ITEM",),
        unavailable_sources=(),
    )


def response_body(*, usage: object = ... , output_text: str | None = None) -> dict:
    body: dict = {
        "model": "gpt-test-2026-09-01",
        "output": [{
            "type": "message",
            "content": [{
                "type": "output_text",
                "text": output_text or json.dumps({
                    "answer": "Approved answer",
                    "citedSourceIds": ["src-01"],
                    "confidence": "HIGH",
                    "abstainReason": None,
                }),
            }],
        }],
    }
    if usage is ...:
        body["usage"] = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    elif usage is not None:
        body["usage"] = usage
    return body


class RecordingStore:
    def __init__(self) -> None:
        self.settlements: list[dict] = []
        self.releases: list[dict] = []

    def reserve(self, **kwargs) -> AIUsageReservation:
        return AIUsageReservation(
            reservation_id=uuid4(),
            tenant_id=kwargs["tenant_id"],
            run_id=kwargs["run_id"],
            attempt_generation=kwargs["attempt_generation"],
            reserved_tokens=kwargs["requested_tokens"],
            policy_version=kwargs["policy_version"],
        )

    def settle(self, **kwargs) -> None:
        self.settlements.append(kwargs)

    def release(self, **kwargs) -> None:
        self.releases.append(kwargs)


def controlled_call(gateway: OpenAIResponsesGateway, store: RecordingStore):
    return AIRuntimeControl(
        store,
        clock=lambda: NOW + timedelta(seconds=2),
    ).invoke_model(
        plan=AIRuntimePlan(
            tenant_id="7",
            provider="OPENAI",
            model="gpt-test",
            policy_version=3,
            max_output_tokens=256,
            warnings=(),
        ),
        run_id=RUN_ID,
        attempt_generation=2,
        input_token_ceiling=900,
        call=lambda limit: gateway.generate(
            "Question",
            context=context(),
            locale="en",
            run_id=RUN_ID,
            safety_identifier="dwp_test",
            max_output_tokens=limit,
            allow_automatic_retry=False,
        ),
        now=NOW,
    )


def gateway(monkeypatch: pytest.MonkeyPatch, handler) -> OpenAIResponsesGateway:
    monkeypatch.setenv("DWP_AGENT_ALLOW_TEST_MODEL_URL", "true")
    return OpenAIResponsesGateway(
        api_key="test-key",
        model="gpt-test",
        base_url="http://model.test/v1",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("usage", [None, {"input_tokens": "10", "output_tokens": 5}])
def test_missing_or_malformed_usage_holds_full_reservation(
    monkeypatch: pytest.MonkeyPatch,
    usage: object,
) -> None:
    model = gateway(
        monkeypatch,
        lambda _request: httpx.Response(200, json=response_body(usage=usage)),
    )
    store = RecordingStore()

    answer = controlled_call(model, store)

    assert answer.usage_observed is False
    assert answer.total_tokens == 0
    assert store.releases == []
    assert len(store.settlements) == 1
    assert store.settlements[0]["usage_observed"] is False


@pytest.mark.parametrize("failure", ["timeout", "reset", "503", "invalid-json"])
def test_indeterminate_provider_failures_remain_unmeasured_and_reserved(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("response timeout", request=request)
        if failure == "reset":
            raise httpx.ConnectError("connection reset", request=request)
        if failure == "503":
            return httpx.Response(503, json={"error": {"code": "unavailable"}})
        return httpx.Response(200, content=b"{not-json")

    store = RecordingStore()
    with pytest.raises(ModelCallFailed):
        controlled_call(gateway(monkeypatch, handler), store)

    assert calls == 1
    assert store.releases == []
    assert len(store.settlements) == 1
    assert store.settlements[0]["usage_observed"] is False


@pytest.mark.parametrize("failure", ["schema", "refusal", "grounding"])
def test_rejected_2xx_response_still_settles_observed_usage(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if failure == "schema":
        body = response_body(output_text='{"answer":42}')
        expected = ModelCallFailed
    elif failure == "refusal":
        body = response_body()
        body["output"][0]["content"] = [{"type": "refusal", "refusal": "blocked"}]
        expected = ModelRefused
    else:
        body = response_body(output_text=json.dumps({
            "answer": "Out of scope",
            "citedSourceIds": ["src-99"],
            "confidence": "HIGH",
            "abstainReason": None,
        }))
        expected = GroundingViolation
    store = RecordingStore()

    with pytest.raises(expected):
        controlled_call(
            gateway(monkeypatch, lambda _request: httpx.Response(200, json=body)),
            store,
        )

    assert store.releases == []
    assert len(store.settlements) == 1
    assert store.settlements[0]["usage_observed"] is True
    assert store.settlements[0]["total_tokens"] == 15
