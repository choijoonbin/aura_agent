from __future__ import annotations

import hashlib
import os
from urllib.parse import quote, urlparse
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.dwaion_workflow_contracts import (
    AttachmentCitation,
    CreateResearchPlanRequest,
    ExecuteResearchRunRequest,
    ResearchBudget,
    ResearchPlanDefinition,
    ResearchProgress,
    ResearchResult,
    ResearchRunCommandRequest,
    ResearchRunState,
    ResearchSourcePolicy,
    StartResearchRunRequest,
)
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.governed_worker_runtime import (
    register_governed_worker_heartbeat,
    remove_governed_worker_heartbeat,
)
from dwp_agent.research_executor import ResearchExecutionOutcome
from dwp_agent.research_plan_store import ResearchPlanStore
from dwp_agent.research_run_runtime import (
    ResearchRunDeadlineExceeded,
    ResearchRunLeaseLost,
)
from dwp_agent.research_run_store import ResearchRunStore
from dwp_agent.research_run_worker import PostgresResearchRunWorker
from dwp_agent.workspace_authorization import WorkspaceRequestAuthorization


DATABASE_URL = os.getenv("DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL is not configured.",
)


@pytest.fixture(scope="module")
def isolated_url() -> str:
    database_name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Research worker tests require a dedicated test database.")
    schema = "test_research_worker_" + uuid4().hex
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in DATABASE_URL else "?"
    url = DATABASE_URL + separator + "options=" + quote(f"-csearch_path={schema}")
    try:
        apply_migrations(url)
        yield url
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.fixture(autouse=True)
def research_worker_heartbeat():
    register_governed_worker_heartbeat("RESEARCH_RUN")
    try:
        yield
    finally:
        remove_governed_worker_heartbeat("RESEARCH_RUN")


class _CompletingExecutor:
    def __init__(self) -> None:
        self.controls = []

    def perform(self, identity, run, plan, controls, authorization, checkpoint):
        assert identity.tenant_id > 0
        assert authorization.available
        controls = checkpoint("BEFORE_CONTEXT")
        self.controls.append(controls)
        checkpoint("AFTER_CONTEXT")
        checkpoint("AFTER_MODEL")
        report = "Verified research result with a citation."
        evidence = "Approved evidence from the governed work source."
        return ResearchExecutionOutcome(
            state=ResearchRunState.COMPLETED,
            progress=ResearchProgress(
                completed_steps=run.progress.total_steps,
                total_steps=run.progress.total_steps,
                discovered_sources=1,
                verified_citations=1,
            ),
            result=ResearchResult(
                report_markdown=report,
                result_sha256=hashlib.sha256(report.encode()).hexdigest(),
                citations=[
                    AttachmentCitation(
                        citation_id="work-item-1",
                        locator="/work/items/1",
                        label="Work item 1",
                        evidence=evidence,
                        content_sha256=hashlib.sha256(evidence.encode()).hexdigest(),
                    )
                ],
            ),
        )


def _identity() -> PersonalDomainIdentity:
    return PersonalDomainIdentity(
        tenant_id=700_000_000 + uuid4().int % 90_000_000,
        user_id=f"research-user-{uuid4()}",
        correlation_id=f"research-correlation-{uuid4()}",
        auth_session_id=f"research-session-{uuid4()}",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset({"APP.ASK:VIEW", "APP.WORK:VIEW"}),
    )


def _queued_run(database_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DWP_DEEP_RESEARCH_WORKER_ENABLED", "true")
    identity = _identity()
    plans = ResearchPlanStore(database_url)
    plan = plans.create(
        identity,
        CreateResearchPlanRequest(
            command_id=uuid4(),
            definition=ResearchPlanDefinition(
                goal="Verify the reviewed evidence for the operational decision.",
                question="Which approved evidence supports this operational decision?",
                success_criteria=["Every material claim includes a verified citation."],
                deliverable_types=["REPORT", "SOURCE_MAP"],
                source_policies=[
                    ResearchSourcePolicy(
                        source_key="WORK_ITEM", allowed=True, scope="SELF"
                    ),
                    ResearchSourcePolicy(
                        source_key="MAIL", allowed=True, scope="SELF"
                    ),
                ],
                require_all_allowed_sources=True,
                budget=ResearchBudget(
                    maximum_minutes=20,
                    maximum_sources=10,
                    maximum_tokens=4_096,
                ),
            ),
        ),
    )
    runs = ResearchRunStore(database_url, plan_store=plans)
    run = runs.start(
        identity,
        plan.plan_id,
        StartResearchRunRequest(
            command_id=uuid4(),
            expected_plan_revision=plan.revision,
            idempotency_key=uuid4(),
        ),
    )
    queued = runs.request_execution(
        identity,
        run.run_id,
        ExecuteResearchRunRequest(command_id=uuid4(), expected_version=run.version),
        WorkspaceRequestAuthorization(
            authorization_header="Bearer research-worker-test-token"
        ),
    )
    return identity, runs, queued


def test_research_execution_is_leased_checkpointed_and_seals_only_worker_result(
    isolated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, runs, queued = _queued_run(isolated_url, monkeypatch)
    executor = _CompletingExecutor()
    worker = PostgresResearchRunWorker(isolated_url, executor=executor)

    assert queued.state == ResearchRunState.QUEUED
    assert worker.process_once() is True
    completed = runs.get(identity, queued.run_id)
    assert completed.state == ResearchRunState.COMPLETED
    assert completed.result is not None and completed.receipt_id is not None
    assert executor.controls[0].excluded_source_keys == []

    with psycopg.connect(isolated_url) as connection:
        row = connection.execute(
            """SELECT generation, checkpoint_sequence, execution_attempt_count,
                      lease_token, execution_authorization_envelope
                 FROM ai_research_runs WHERE run_id = %s""",
            (queued.run_id,),
        ).fetchone()
    assert row[0] >= 2
    assert row[1] >= 4
    assert row[2] == 1
    assert row[3] is None
    assert row[4] is None


def test_runtime_commands_fence_stale_workers_and_change_effective_controls(
    isolated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, runs, queued = _queued_run(isolated_url, monkeypatch)
    worker = PostgresResearchRunWorker(isolated_url, executor=_CompletingExecutor())
    first = worker.claim()
    assert first is not None
    assert worker.claim() is None

    paused = runs.command(
        identity,
        queued.run_id,
        ResearchRunCommandRequest(
            command_id=uuid4(), expected_version=first.run.version,
            action="PAUSE", reason="Pause while the evidence scope is reviewed.",
        ),
    )
    with pytest.raises(ResearchRunLeaseLost):
        worker.checkpoint(first, "AFTER_CONTEXT")

    resumed = runs.command(
        identity,
        queued.run_id,
        ResearchRunCommandRequest(
            command_id=uuid4(), expected_version=paused.version,
            action="RESUME", reason="Resume with the reviewed execution scope.",
        ),
        WorkspaceRequestAuthorization(
            authorization_header="Bearer refreshed-research-worker-token"
        ),
    )
    with psycopg.connect(isolated_url) as connection:
        authorization_envelope = connection.execute(
            "SELECT execution_authorization_envelope FROM ai_research_runs WHERE run_id = %s",
            (queued.run_id,),
        ).fetchone()[0]
    refreshed = runs.codec.decrypt_json(
        authorization_envelope,
        tenant_id=identity.tenant_id,
        resource_type="research-run",
        resource_id=str(queued.run_id),
        field="execution-authorization",
    )
    assert refreshed["authorizationHeader"] == "Bearer refreshed-research-worker-token"
    second = worker.claim()
    assert second is not None and second.run.version > resumed.version
    excluded = runs.command(
        identity,
        queued.run_id,
        ResearchRunCommandRequest(
            command_id=uuid4(), expected_version=second.run.version,
            action="EXCLUDE_SOURCE_AND_CONTINUE", source_key="MAIL",
            reason="Exclude the unavailable mail source and continue safely.",
        ),
    )
    with pytest.raises(ResearchRunLeaseLost):
        worker.finalize(
            second,
            ResearchExecutionOutcome(
                state=ResearchRunState.PARTIAL,
                progress=second.run.progress,
                safe_error_code="STALE_WORKER_RESULT",
            ),
        )
    third = worker.claim()
    assert third is not None and third.controls.excluded_source_keys == ["MAIL"]

    reprobed = runs.command(
        identity,
        queued.run_id,
        ResearchRunCommandRequest(
            command_id=uuid4(), expected_version=third.run.version,
            action="REPROBE_SOURCE", source_key="MAIL",
            reason="Reprobe mail after restoring its delegated authorization.",
        ),
    )
    fourth = worker.claim()
    assert fourth is not None and fourth.run.version > reprobed.version
    assert fourth.controls.excluded_source_keys == []
    assert fourth.controls.reprobe_source_keys == ["MAIL"]
    old_deadline = fourth.controls.deadline_at
    extended = runs.command(
        identity,
        queued.run_id,
        ResearchRunCommandRequest(
            command_id=uuid4(), expected_version=fourth.run.version,
            action="EXTEND", extension_minutes=15,
            reason="Extend the reviewed runtime budget for source verification.",
        ),
    )
    latest_controls = worker.checkpoint(fourth, "AFTER_CONTEXT")
    assert latest_controls.deadline_at > old_deadline
    assert latest_controls.extension_minutes == 15

    cancelling = runs.command(
        identity,
        queued.run_id,
        ResearchRunCommandRequest(
            command_id=uuid4(), expected_version=extended.version,
            action="SAFE_CANCEL", reason="Cancel safely before any result is sealed.",
        ),
    )
    assert cancelling.state == ResearchRunState.CANCELLING
    with pytest.raises(ResearchRunLeaseLost):
        worker.checkpoint(fourth, "AFTER_MODEL")
    assert worker.process_once() is True
    assert runs.get(identity, queued.run_id).state == ResearchRunState.CANCELLED


def test_elapsed_runtime_deadline_blocks_checkpoint_and_result_sealing(
    isolated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, queued = _queued_run(isolated_url, monkeypatch)
    worker = PostgresResearchRunWorker(isolated_url, executor=_CompletingExecutor())
    lease = worker.claim()
    assert lease is not None
    with psycopg.connect(isolated_url) as connection:
        connection.execute(
            """UPDATE ai_research_runs
                  SET runtime_deadline_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                WHERE run_id = %s""",
            (queued.run_id,),
        )
    with pytest.raises(ResearchRunDeadlineExceeded):
        worker.checkpoint(lease, "AFTER_CONTEXT")
    with pytest.raises(ResearchRunDeadlineExceeded):
        worker.finalize(
            lease,
            _CompletingExecutor().perform(
                lease.identity,
                lease.run,
                worker.plan_store.get(lease.identity, lease.run.plan_id),
                lease.controls,
                lease.authorization.workspace_authorization(),
                lambda _phase: lease.controls,
            ),
        )
