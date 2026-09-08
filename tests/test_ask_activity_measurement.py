from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

import dwp_agent.ask_runtime as runtime_module
from dwp_agent.ask_runtime import AskRuntime
from dwp_agent.context_broker import GroundedContext, GroundedSource
from dwp_agent.contracts import (
    AgentRegistryResolution,
    AnswerConfidence,
    AskCitation,
    AskRequest,
    CitationSourceType,
    RegistryResolutionStatus,
    RegistryRiskTier,
)
from dwp_agent.model_gateway import ModelAnswer, ModelConfigurationRequired
from dwp_agent.policy import AskIdentity
from dwp_agent.run_store import InMemoryRunStore
from dwp_agent.user_run_store import InMemoryUserRunStore


ACTIVE = AgentRegistryResolution(
    entry_key="DWP_ASSISTANT", revision=1, artifact_version="test-v1",
    risk_tier=RegistryRiskTier.LOW, resolution=RegistryResolutionStatus.ACTIVE,
)


class Broker:
    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty

    def collect(self, *_args, **_kwargs) -> GroundedContext:
        if self.empty:
            return GroundedContext((), ("WORK_ITEM",), ())
        return GroundedContext(
            sources=(GroundedSource(
                citation=AskCitation(
                    source_id="src-01", source_type=CitationSourceType.WORK_ITEM,
                    title="Safe work title", source_system="DWP Work",
                    occurred_at=datetime.now(timezone.utc),
                ),
                evidence="Safe internal evidence.", rank=1,
            ),),
            attempted_sources=("WORK_ITEM",), unavailable_sources=(),
        )


class ConfigModel:
    model = "gpt-test"

    def generate(self, *_args, **_kwargs):
        raise ModelConfigurationRequired("not configured")


class ExplodingModel:
    model = "gpt-test"

    def generate(self, *_args, **_kwargs):
        raise RuntimeError("raw provider detail must not enter activity")


class SuccessfulModel:
    model = "gpt-test"

    def generate(self, *_args, **_kwargs) -> ModelAnswer:
        return ModelAnswer(
            answer="Grounded answer.", cited_source_ids=("src-01",),
            confidence=AnswerConfidence.HIGH, abstain_reason=None,
            provider="OPENAI", model="gpt-test", input_tokens=1,
            output_tokens=1, total_tokens=2, latency_ms=1,
            provider_request_hash="a" * 64,
        )


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWP_AGENT_PRIVACY_HASH_SECRET", "measurement-privacy-secret")
    monkeypatch.setenv("DWP_AGENT_SAFETY_SECRET", "measurement-safety-secret")
    monkeypatch.setattr(runtime_module, "resolve_agent", lambda *_a, **_k: ACTIVE)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("registry", ["COMPLETED", "SKIPPED", "SKIPPED", "SKIPPED", "COMPLETED", "COMPLETED"]),
        ("policy", ["COMPLETED", "SKIPPED", "SKIPPED", "SKIPPED", "COMPLETED", "COMPLETED"]),
        ("no-source", ["COMPLETED", "COMPLETED", "SKIPPED", "SKIPPED", "COMPLETED", "COMPLETED"]),
        ("model-config", ["COMPLETED", "COMPLETED", "COMPLETED", "SKIPPED", "COMPLETED", "COMPLETED"]),
    ],
)
def test_early_terminal_paths_preserve_the_five_stage_denominator(
    monkeypatch: pytest.MonkeyPatch, case: str, expected: list[str],
) -> None:
    store = InMemoryRunStore()
    broker = Broker(empty=case == "no-source")
    model = ConfigModel() if case == "model-config" else SuccessfulModel()
    if case == "registry":
        inactive = ACTIVE.model_copy(update={"resolution": RegistryResolutionStatus.REFERENCE_FALLBACK})
        monkeypatch.setattr(runtime_module, "resolve_agent", lambda *_a, **_k: inactive)
    query = "Show my payroll and salary" if case == "policy" else "What needs attention?"
    response = AskRuntime(
        context_broker=broker, model_gateway=model, run_store=store,
    ).answer(
        AskRequest(request_id=f"measurement-{case}", query=query),
        identity=_identity(),
    )
    summary = InMemoryUserRunStore(store).get(
        tenant_id="1", user_id="900018", run_id=UUID(response.run_id),
    )
    assert summary is not None
    assert [stage.state for stage in summary.stages] == expected
    assert summary.progress_percent == 100
    assert summary.measurement_status == "MEASURED"


def test_unexpected_model_failure_keeps_actual_partial_progress() -> None:
    store = InMemoryRunStore()
    runtime = AskRuntime(context_broker=Broker(), model_gateway=ExplodingModel(), run_store=store)
    with pytest.raises(RuntimeError):
        runtime.answer(
            AskRequest(request_id="measurement-failed", query="What needs attention?"),
            identity=_identity(),
        )
    summary = InMemoryUserRunStore(store).list(
        tenant_id="1", user_id="900018", limit=1, run_state=None,
    )[0]
    assert summary.run_state == "FAILED"
    assert summary.progress_percent == 40
    assert summary.measurement_status == "PARTIAL"
    assert summary.stages[-2].state == "FAILED"
    assert summary.stages[-1].key == "FAILED"
    assert "raw provider detail" not in summary.model_dump_json()


def _identity() -> AskIdentity:
    return AskIdentity(
        tenant_id="1", user_id="900018", roles=("WORKSPACE_MEMBER",),
        permissions=("APP.ASK:VIEW", "APP.WORK:VIEW"),
        correlation_id="measurement-correlation",
    )
