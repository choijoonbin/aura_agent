from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from dwp_agent.context_broker import GroundedContext, GroundedSource
from dwp_agent.contracts import AnswerConfidence, AskCitation, CitationSourceType
from dwp_agent.dwaion_workflow_contracts import (
    ResearchBudget,
    ResearchPlan,
    ResearchPlanDefinition,
    ResearchPlanState,
    ResearchProgress,
    ResearchRun,
    ResearchRunState,
    ResearchSourcePolicy,
)
from dwp_agent.model_gateway import ModelAnswer
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.research_executor import ResearchExecutor
from dwp_agent.research_run_runtime import ResearchRuntimeControls
from dwp_agent.workspace_authorization import WorkspaceRequestAuthorization


class _Broker:
    def __init__(self) -> None:
        self.scopes = ()

    def collect(self, *_args, **kwargs) -> GroundedContext:
        self.scopes = kwargs["source_scopes"]
        sources = tuple(
            GroundedSource(
                citation=AskCitation(
                    source_id=f"src-{index:02d}",
                    source_type=CitationSourceType.WORK_ITEM,
                    source_system="Work",
                    title=f"Evidence {index}",
                    route=f"/work/{index}",
                ),
                evidence=f"Verified evidence {index}.",
                rank=index,
            )
            for index in range(1, 3)
        )
        return GroundedContext(
            sources=sources,
            attempted_sources=("WORK_ITEM",),
            unavailable_sources=(),
        )


class _Model:
    def __init__(self) -> None:
        self.context_source_count = 0
        self.max_output_tokens = 0

    def generate(self, *_args, **kwargs) -> ModelAnswer:
        self.context_source_count = len(kwargs["context"].sources)
        self.max_output_tokens = kwargs["max_output_tokens"]
        return ModelAnswer(
            answer="The reviewed work evidence supports the decision.",
            cited_source_ids=("src-01",),
            confidence=AnswerConfidence.HIGH,
            abstain_reason=None,
            provider="OPENAI",
            model="gpt-test",
            input_tokens=40,
            output_tokens=12,
            total_tokens=52,
            latency_ms=10,
            provider_request_hash="a" * 64,
        )


def test_executor_applies_excluded_sources_source_budget_and_checkpoints() -> None:
    now = datetime.now(UTC)
    plan_id = uuid4()
    plan = ResearchPlan(
        plan_id=plan_id,
        state=ResearchPlanState.READY,
        revision=1,
        definition=ResearchPlanDefinition(
            goal="Verify the approved operational evidence before deciding.",
            question="Which governed evidence supports the operational decision?",
            success_criteria=["Every material claim has a verified citation."],
            deliverable_types=["REPORT"],
            source_policies=[
                ResearchSourcePolicy(source_key="WORK_ITEM", allowed=True, scope="SELF"),
                ResearchSourcePolicy(source_key="MAIL", allowed=True, scope="SELF"),
            ],
            budget=ResearchBudget(
                maximum_minutes=20, maximum_sources=1, maximum_tokens=1_024
            ),
        ),
        created_at=now,
        updated_at=now,
    )
    run = ResearchRun(
        run_id=uuid4(),
        plan_id=plan_id,
        plan_revision=1,
        state=ResearchRunState.RUNNING,
        version=3,
        progress=ResearchProgress(
            completed_steps=0,
            total_steps=4,
            discovered_sources=0,
            verified_citations=0,
        ),
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    controls = ResearchRuntimeControls(
        excluded_source_keys=["MAIL"], deadline_at=now + timedelta(minutes=20)
    )
    phases: list[str] = []

    def checkpoint(phase: str) -> ResearchRuntimeControls:
        phases.append(phase)
        return controls

    broker = _Broker()
    model = _Model()
    outcome = ResearchExecutor(
        context_broker=broker,  # type: ignore[arg-type]
        model_gateway=model,  # type: ignore[arg-type]
    ).perform(
        PersonalDomainIdentity(
            tenant_id=71,
            user_id="research-user",
            correlation_id="research-correlation",
            auth_session_id="research-session",
            roles=frozenset({"WORKSPACE_MEMBER"}),
            permissions=frozenset({"APP.ASK:VIEW", "APP.WORK:VIEW"}),
        ),
        run,
        plan,
        controls,
        WorkspaceRequestAuthorization(authorization_header="Bearer test-token"),
        checkpoint,
    )

    assert broker.scopes == (CitationSourceType.WORK_ITEM,)
    assert model.context_source_count == 1
    assert model.max_output_tokens == 1_024
    assert phases == ["BEFORE_CONTEXT", "AFTER_CONTEXT", "AFTER_MODEL"]
    assert outcome.state == ResearchRunState.COMPLETED
    assert outcome.result is not None
