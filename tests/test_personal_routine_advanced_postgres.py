from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect
from psycopg.rows import dict_row

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.domain_retention_store import PostgresDomainRetentionStore
from dwp_agent.dwaion_workflow_contracts import WorkflowCapability
from dwp_agent.governed_domain_contracts import DomainKey, UpsertRetentionPolicyRequest
from dwp_agent.governed_domain_core import GovernedDomainConflict
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.personal_memory_contracts import UpdateAiSourcePreferenceRequest
from dwp_agent.personal_memory_postgres_store import PostgresPersonalMemoryStore
from dwp_agent.personal_routine_advanced_contracts import (
    CreateRoutineAdvancedCommandRequest,
    DecideRoutineAdvancedCommandRequest,
    RoutineAdvancedCommandState,
)
from dwp_agent.personal_routine_advanced_provider import (
    RoutineAdvancedProviderOutcome,
    RoutineAdvancedProviderResult,
    provider_result_digest,
)
from dwp_agent.personal_routine_advanced_effects import (
    load_routine_runtime_policy,
    reserve_monthly_run_budget,
)
from dwp_agent.personal_routine_advanced_store import PersonalRoutineAdvancedCommandStore
from dwp_agent.personal_routine_contracts import (
    CreateRoutineRequest,
    RoutineBudget,
    RoutineDefinition,
    UpdateRoutineRequest,
)
from dwp_agent.personal_routine_execution_provider import (
    RoutineExecutionRuntimeControls,
    RoutineProviderResult,
    runtime_controls_digest,
)
from dwp_agent.personal_routine_execution_worker import (
    PostgresPersonalRoutineExecutionWorker,
    RoutineExecutionLease,
)
from dwp_agent.personal_routine_postgres_store import PostgresPersonalRoutineStore


DATABASE_URL = os.getenv("DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL is not configured.",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    if not DATABASE_URL:
        return
    name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Advanced routine tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)


def test_change_approval_is_independent_replay_safe_encrypted_and_audited() -> None:
    tenant_id = 860_000_000 + uuid4().int % 100_000_000
    maker = _identity(tenant_id, "routine-maker", "maker-session")
    checker = _identity(tenant_id, "routine-checker", "checker-session")
    routine = _create_routine(maker, "Original priorities")
    store = PersonalRoutineAdvancedCommandStore(DATABASE_URL)
    command_id = uuid4()
    create_request = CreateRoutineAdvancedCommandRequest(
        commandId=command_id,
        expectedRevision=routine.revision,
        reasonCode="USER_ROUTINE_CHANGE_APPROVAL",
        changeReason="Require independent approval for this routine definition change.",
        payload={
            "kind": "CHANGE_APPROVAL",
            "definition": _definition("Approved priorities"),
        },
    )

    requested = store.create(maker, routine.routine_id, create_request)
    initial_replay = store.create(maker, routine.routine_id, create_request)

    assert requested.state == RoutineAdvancedCommandState.AWAITING_APPROVAL
    assert requested.can_approve is False
    assert initial_replay == requested
    assert store.list(maker, routine.routine_id) == [requested]

    decision = DecideRoutineAdvancedCommandRequest(
        commandId=uuid4(),
        expectedRevision=requested.version,
        reasonCode="ROUTINE_CHANGE_CHECKER_DECISION",
        changeReason="Approve the reviewed routine definition and its governed sources.",
        decision="APPROVE",
        evidenceRefs=["ticket:DWAI-71", "review:definition-v2"],
    )
    with pytest.raises(GovernedDomainConflict, match="Maker and checker"):
        store.decide(maker, command_id, decision)

    completed = store.decide(checker, command_id, decision)
    decision_replay = store.decide(checker, command_id, decision)
    create_replay_after_revision_change = store.create(
        maker, routine.routine_id, create_request
    )
    updated = PostgresPersonalRoutineStore(DATABASE_URL).get(
        maker, routine.routine_id
    )

    assert completed.state == RoutineAdvancedCommandState.SUCCEEDED
    assert completed.checker_user_id == checker.user_id
    assert completed.receipt is not None
    assert completed.receipt.provider_receipt_id == f"internal-maker-checker:{command_id}"
    assert completed.receipt.applied_revision == updated.revision
    assert updated.definition.name == "Approved priorities"
    assert decision_replay == completed
    assert create_replay_after_revision_change == completed
    reconnected_checker = _identity(
        tenant_id, checker.user_id, "checker-reconnected-session"
    )
    assert store.decide(reconnected_checker, command_id, decision) == completed
    for changed_request in (
        decision.model_copy(update={"command_id": uuid4()}),
        decision.model_copy(update={"evidence_refs": ["ticket:mutated-evidence"]}),
        decision.model_copy(
            update={"change_reason": "A different checker rationale must not replay."}
        ),
    ):
        with pytest.raises(GovernedDomainConflict, match="bound to another"):
            store.decide(checker, command_id, changed_request)

    with connect(DATABASE_URL) as connection:
        payload_envelope, receipt_envelope = connection.execute(
            """SELECT payload_envelope, receipt_envelope
                 FROM ai_personal_routine_advanced_commands
                WHERE command_id = %s""",
            (command_id,),
        ).fetchone()
        events = connection.execute(
            """SELECT event_type, actor_user_id, current_state
                 FROM ai_personal_routine_advanced_events
                WHERE command_id = %s ORDER BY occurred_at, event_id""",
            (command_id,),
        ).fetchall()
        decision_row = connection.execute(
            """SELECT decision_id, checker_user_id, decision, decision_envelope
                 FROM ai_personal_routine_advanced_decisions
                WHERE command_id = %s""",
            (command_id,),
        ).fetchone()

    assert payload_envelope.startswith("dwp2.")
    assert receipt_envelope.startswith("dwp2.")
    assert "Approved priorities" not in payload_envelope
    assert decision_row[:3] == (
        decision.command_id,
        checker.user_id,
        "APPROVE",
    )
    assert "ticket:DWAI-71" not in decision_row[3]
    decision_evidence = store.codec.decrypt_json(
        decision_row[3],
        tenant_id=tenant_id,
        resource_type="routine-advanced-decision",
        resource_id=str(command_id),
        field="decision",
    )
    assert decision_evidence["changeReason"].startswith("Approve the reviewed")
    assert decision_evidence["evidenceRefs"] == [
        "ticket:DWAI-71",
        "review:definition-v2",
    ]
    assert events == [
        ("REQUESTED", maker.user_id, "AWAITING_APPROVAL"),
        ("COMPLETED", checker.user_id, "SUCCEEDED"),
    ]


def test_provider_command_persists_bound_receipt_and_terminal_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = 870_000_000 + uuid4().int % 100_000_000
    owner = _identity(tenant_id, "routine-owner", "owner-session")
    routine = _create_routine(owner, "Evidence delivery")
    digests: list[str] = []

    class _Provider:
        def __init__(self, kind: object) -> None:
            self.kind = kind

        def execute(self, context: object) -> RoutineAdvancedProviderResult:
            outcome = RoutineAdvancedProviderOutcome(
                routineId=context.routine_id,
                expectedRevision=context.expected_revision,
                kind=context.kind,
                outcome="APPLIED",
                appliedPayload=context.payload,
                evidenceRef="worm-evidence:archive-87",
            )
            result_sha256 = provider_result_digest(outcome)
            digests.append(result_sha256)
            return RoutineAdvancedProviderResult(
                commandId=context.command_id,
                kind=context.kind,
                state="SUCCEEDED",
                providerReceiptId="worm-provider-receipt-87",
                resultSha256=result_sha256,
                appliedRevision=context.expected_revision,
                result=outcome,
            )

    monkeypatch.setattr(
        "dwp_agent.personal_routine_advanced_store.routine_advanced_capability",
        lambda _kind: WorkflowCapability(available=True, configured=True),
    )
    monkeypatch.setattr(
        "dwp_agent.personal_routine_advanced_store.HttpRoutineAdvancedProvider",
        _Provider,
    )
    store = PersonalRoutineAdvancedCommandStore(DATABASE_URL)
    request = CreateRoutineAdvancedCommandRequest(
        commandId=uuid4(),
        expectedRevision=routine.revision,
        reasonCode="USER_ROUTINE_WORM_EVIDENCE",
        changeReason="Deliver the reviewed routine evidence to governed WORM storage.",
        payload={
            "kind": "WORM_EVIDENCE_DELIVERY",
            "evidenceScope": "FULL_AUDIT",
            "retentionDays": 365,
            "legalHold": True,
        },
    )

    completed = store.create(owner, routine.routine_id, request)
    replay = store.create(owner, routine.routine_id, request)

    assert replay == completed
    assert completed.state == RoutineAdvancedCommandState.SUCCEEDED
    assert completed.receipt is not None
    assert completed.receipt.provider_receipt_id == "worm-provider-receipt-87"
    assert completed.receipt.result_sha256 == digests[0]
    assert completed.receipt.provider_outcome.evidence_ref == "worm-evidence:archive-87"
    assert completed.receipt.provider_outcome.applied_payload.kind.value == (
        "WORM_EVIDENCE_DELIVERY"
    )
    with connect(DATABASE_URL) as connection:
        states = connection.execute(
            """SELECT event_type, current_state
                 FROM ai_personal_routine_advanced_events
                WHERE command_id = %s ORDER BY occurred_at, event_id""",
            (request.command_id,),
        ).fetchall()
    assert states == [("REQUESTED", "RUNNING"), ("COMPLETED", "SUCCEEDED")]


def test_provider_receipt_with_wrong_routine_binding_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = 875_000_000 + uuid4().int % 100_000_000
    owner = _identity(tenant_id, "routine-owner", "provider-binding-session")
    routine = _create_routine(owner, "Bound evidence delivery")

    def bypass_initial_validation(_provider: object, context: object):
        outcome = RoutineAdvancedProviderOutcome(
            routineId=uuid4(),
            expectedRevision=context.expected_revision,
            kind=context.kind,
            outcome="APPLIED",
            appliedPayload=context.payload,
            evidenceRef="worm-evidence:wrong-routine",
        )
        return RoutineAdvancedProviderResult(
            commandId=context.command_id,
            kind=context.kind,
            state="SUCCEEDED",
            providerReceiptId="worm-provider-wrong-routine",
            resultSha256=provider_result_digest(outcome),
            result=outcome,
        )

    monkeypatch.setattr(
        "dwp_agent.personal_routine_advanced_store.routine_advanced_capability",
        lambda _kind: WorkflowCapability(available=True, configured=True),
    )
    monkeypatch.setattr(
        "dwp_agent.personal_routine_advanced_store.execute_bound_provider",
        bypass_initial_validation,
    )
    failed = PersonalRoutineAdvancedCommandStore(DATABASE_URL).create(
        owner,
        routine.routine_id,
        CreateRoutineAdvancedCommandRequest(
            commandId=uuid4(),
            expectedRevision=routine.revision,
            reasonCode="USER_ROUTINE_WORM_EVIDENCE",
            changeReason="Reject a provider receipt bound to a different routine.",
            payload={
                "kind": "WORM_EVIDENCE_DELIVERY",
                "evidenceScope": "FULL_AUDIT",
                "retentionDays": 365,
                "legalHold": True,
            },
        ),
    )

    assert failed.state == RoutineAdvancedCommandState.FAILED
    assert failed.receipt is None
    assert failed.problem is not None
    assert failed.problem.code == "ROUTINE_ADVANCED_PROVIDER_BINDING_INVALID"


def test_change_rejection_is_terminal_replay_safe_and_does_not_mutate_routine() -> None:
    tenant_id = 880_000_000 + uuid4().int % 100_000_000
    maker = _identity(tenant_id, "rejection-maker", "rejection-maker-session")
    checker = _identity(
        tenant_id, "rejection-checker", "rejection-checker-session"
    )
    routine = _create_routine(maker, "Protected definition")
    store = PersonalRoutineAdvancedCommandStore(DATABASE_URL)
    requested = store.create(
        maker,
        routine.routine_id,
        CreateRoutineAdvancedCommandRequest(
            commandId=uuid4(),
            expectedRevision=routine.revision,
            reasonCode="USER_ROUTINE_CHANGE_APPROVAL",
            changeReason="Require independent review before changing this routine.",
            payload={
                "kind": "CHANGE_APPROVAL",
                "definition": _definition("Rejected definition"),
            },
        ),
    )
    decision = DecideRoutineAdvancedCommandRequest(
        commandId=uuid4(),
        expectedRevision=requested.version,
        reasonCode="ROUTINE_CHANGE_CHECKER_DECISION",
        changeReason="Reject the change because its reviewed scope is too broad.",
        decision="REJECT",
        evidenceRefs=["review:scope-too-broad"],
    )

    rejected = store.decide(checker, requested.command_id, decision)
    replay = store.decide(checker, requested.command_id, decision)
    unchanged = PostgresPersonalRoutineStore(DATABASE_URL).get(
        maker, routine.routine_id
    )

    assert replay == rejected
    assert rejected.state == RoutineAdvancedCommandState.REJECTED
    assert rejected.checker_user_id == checker.user_id
    assert rejected.receipt is None
    assert unchanged.revision == routine.revision
    assert unchanged.definition.name == "Protected definition"
    with pytest.raises(GovernedDomainConflict, match="terminal decision"):
        store.decide(
            checker,
            requested.command_id,
            decision.model_copy(
                update={"command_id": uuid4(), "decision": "APPROVE"}
            ),
        )
    with connect(DATABASE_URL) as connection:
        events = connection.execute(
            """SELECT event_type, current_state
                 FROM ai_personal_routine_advanced_events
                WHERE command_id = %s ORDER BY occurred_at, event_id""",
            (requested.command_id,),
        ).fetchall()
        rejection_audit = connection.execute(
            """SELECT checker_user_id, decision, decision_envelope
                 FROM ai_personal_routine_advanced_decisions
                WHERE command_id = %s""",
            (requested.command_id,),
        ).fetchone()
    assert events == [
        ("REQUESTED", "AWAITING_APPROVAL"),
        ("REJECTED", "REJECTED"),
    ]
    assert rejection_audit[:2] == (checker.user_id, "REJECT")
    assert "scope-too-broad" not in rejection_audit[2]


def test_pending_checker_queue_is_tenant_scoped_and_excludes_the_maker() -> None:
    tenant_id = 890_000_000 + uuid4().int % 50_000_000
    maker = _identity(tenant_id, "queue-maker", "queue-maker-session")
    checker = _identity(tenant_id, "queue-checker", "queue-checker-session")
    other_tenant_checker = _identity(
        tenant_id + 50_000_000, "other-checker", "other-checker-session"
    )
    routine = _create_routine(maker, "Queue protected definition")
    store = PersonalRoutineAdvancedCommandStore(DATABASE_URL)
    requested = store.create(
        maker,
        routine.routine_id,
        CreateRoutineAdvancedCommandRequest(
            commandId=uuid4(),
            expectedRevision=routine.revision,
            reasonCode="USER_ROUTINE_CHANGE_APPROVAL",
            changeReason="Queue this definition for an independent tenant checker.",
            payload={
                "kind": "CHANGE_APPROVAL",
                "definition": _definition("Queue reviewed definition"),
            },
        ),
    )

    checker_queue = store.list_pending_for_checker(checker, limit=25)

    assert [item.command_id for item in checker_queue] == [requested.command_id]
    assert checker_queue[0].can_approve is True
    assert checker_queue[0].owner_user_id == maker.user_id
    assert checker_queue[0].proposed_definition is not None
    assert checker_queue[0].proposed_definition.name == "Queue reviewed definition"
    assert store.list_pending_for_checker(maker, limit=25) == []
    assert store.list_pending_for_checker(other_tenant_checker, limit=25) == []


def test_approval_claim_survives_finalize_crash_and_is_recoverable_only_by_claimant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = 810_000_000 + uuid4().int % 40_000_000
    maker = _identity(tenant_id, "crash-maker", "crash-maker-session")
    checker = _identity(tenant_id, "crash-checker", "crash-checker-session")
    other_checker = _identity(tenant_id, "other-checker", "other-checker-session")
    routine = _create_routine(maker, "Crash-safe original")
    store = PersonalRoutineAdvancedCommandStore(DATABASE_URL)
    requested = _request_change(store, maker, routine, "Crash-safe approved")
    decision = _approve(requested, "evidence:crash-recovery")
    finalize = store._finalize

    def crash_before_finalize(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated crash after routine update")

    monkeypatch.setattr(store, "_finalize", crash_before_finalize)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.decide(checker, requested.command_id, decision)

    applied_once = PostgresPersonalRoutineStore(DATABASE_URL).get(
        maker, routine.routine_id
    )
    assert applied_once.revision == routine.revision + 1
    assert applied_once.definition.name == "Crash-safe approved"
    assert [
        item.command_id for item in store.list_pending_for_checker(checker, limit=25)
    ] == [requested.command_id]
    assert store.list_pending_for_checker(other_checker, limit=25) == []
    with connect(DATABASE_URL) as connection:
        state, claimant, audits = connection.execute(
            """SELECT c.state, c.checker_user_id, COUNT(d.command_id)
                 FROM ai_personal_routine_advanced_commands c
                 LEFT JOIN ai_personal_routine_advanced_decisions d
                   ON d.command_id = c.command_id
                WHERE c.command_id = %s
                GROUP BY c.state, c.checker_user_id""",
            (requested.command_id,),
        ).fetchone()
    assert (state, claimant, audits) == ("AWAITING_APPROVAL", checker.user_id, 1)

    monkeypatch.setattr(store, "_finalize", finalize)
    reconnected = _identity(tenant_id, checker.user_id, "checker-new-session")
    completed = store.decide(reconnected, requested.command_id, decision)
    applied_after_retry = PostgresPersonalRoutineStore(DATABASE_URL).get(
        maker, routine.routine_id
    )

    assert completed.state == RoutineAdvancedCommandState.SUCCEEDED
    assert applied_after_retry.revision == applied_once.revision
    with connect(DATABASE_URL) as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM ai_personal_routine_advanced_decisions
                WHERE command_id = %s""",
            (requested.command_id,),
        ).fetchone()[0] == 1


def test_concurrent_checkers_cannot_split_claim_or_apply_routine_twice() -> None:
    tenant_id = 840_000_000 + uuid4().int % 40_000_000
    maker = _identity(tenant_id, "race-maker", "race-maker-session")
    checkers = (
        _identity(tenant_id, "race-checker-a", "race-checker-a-session"),
        _identity(tenant_id, "race-checker-b", "race-checker-b-session"),
    )
    routine = _create_routine(maker, "Race-safe original")
    store = PersonalRoutineAdvancedCommandStore(DATABASE_URL)
    requested = _request_change(store, maker, routine, "Race-safe approved")
    decisions = (
        _approve(requested, "evidence:checker-a"),
        _approve(requested, "evidence:checker-b"),
    )
    barrier = Barrier(2)

    def decide(index: int):
        barrier.wait(timeout=5)
        try:
            return store.decide(checkers[index], requested.command_id, decisions[index])
        except GovernedDomainConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(decide, range(2)))

    completed = [
        item for item in outcomes if not isinstance(item, GovernedDomainConflict)
    ]
    conflicts = [item for item in outcomes if isinstance(item, GovernedDomainConflict)]
    assert len(completed) == 1
    assert len(conflicts) == 1
    assert completed[0].state == RoutineAdvancedCommandState.SUCCEEDED
    updated = PostgresPersonalRoutineStore(DATABASE_URL).get(maker, routine.routine_id)
    assert updated.revision == routine.revision + 1
    assert updated.definition.name == "Race-safe approved"
    with connect(DATABASE_URL) as connection:
        audit_count, checker_user_id = connection.execute(
            """SELECT COUNT(*), MIN(checker_user_id)
                 FROM ai_personal_routine_advanced_decisions
                WHERE command_id = %s""",
            (requested.command_id,),
        ).fetchone()
    assert audit_count == 1
    assert checker_user_id == completed[0].checker_user_id


def test_stale_approved_definition_fails_closed_without_overwriting_routine() -> None:
    tenant_id = 920_000_000 + uuid4().int % 40_000_000
    maker = _identity(tenant_id, "stale-maker", "stale-maker-session")
    checker = _identity(tenant_id, "stale-checker", "stale-checker-session")
    routine = _create_routine(maker, "Stale original")
    store = PersonalRoutineAdvancedCommandStore(DATABASE_URL)
    requested = _request_change(store, maker, routine, "Must not overwrite")
    changed = PostgresPersonalRoutineStore(DATABASE_URL).update(
        maker,
        routine.routine_id,
        UpdateRoutineRequest(
            commandId=uuid4(),
            expectedRevision=routine.revision,
            reasonCode="USER_ROUTINE_UPDATE",
            changeReason="Apply an intervening owner edit before checker approval.",
            definition=_definition("Intervening owner edit"),
        ),
    )
    decision = _approve(requested, "evidence:stale-review")

    failed = store.decide(checker, requested.command_id, decision)
    replay = store.decide(checker, requested.command_id, decision)
    current = PostgresPersonalRoutineStore(DATABASE_URL).get(maker, routine.routine_id)

    assert replay == failed
    assert failed.state == RoutineAdvancedCommandState.FAILED
    assert failed.problem is not None
    assert failed.problem.code == "ROUTINE_APPROVAL_REVISION_CONFLICT"
    assert current.revision == changed.revision
    assert current.definition.name == "Intervening owner edit"
    with connect(DATABASE_URL) as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM ai_personal_routine_advanced_decisions
                WHERE command_id = %s""",
            (requested.command_id,),
        ).fetchone()[0] == 1


def test_engine_override_and_budget_exception_are_consumed_by_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = 950_000_000 + uuid4().int % 20_000_000
    owner = _identity(tenant_id, "runtime-effect-owner", "runtime-effect-session")
    definition = _definition("Runtime effects").model_copy(
        update={
            "budget": RoutineBudget(
                maximumRunsPerMonth=1,
                maximumTokensPerRun=128,
                maximumMinutesPerRun=1,
            )
        }
    )
    routine = _create_routine(owner, "Runtime effects", definition=definition)

    class _Provider:
        def __init__(self, kind: object) -> None:
            self.kind = kind

        def execute(self, context: object) -> RoutineAdvancedProviderResult:
            outcome = RoutineAdvancedProviderOutcome(
                routineId=context.routine_id,
                expectedRevision=context.expected_revision,
                kind=context.kind,
                outcome="APPLIED",
                appliedPayload=context.payload,
                evidenceRef=f"runtime-effect:{context.command_id}",
            )
            return RoutineAdvancedProviderResult(
                commandId=context.command_id,
                kind=context.kind,
                state="SUCCEEDED",
                providerReceiptId=f"runtime-provider:{context.command_id}",
                resultSha256=provider_result_digest(outcome),
                result=outcome,
            )

    monkeypatch.setattr(
        "dwp_agent.personal_routine_advanced_store.routine_advanced_capability",
        lambda _kind: WorkflowCapability(available=True, configured=True),
    )
    monkeypatch.setattr(
        "dwp_agent.personal_routine_advanced_store.HttpRoutineAdvancedProvider",
        _Provider,
    )
    advanced = PersonalRoutineAdvancedCommandStore(DATABASE_URL)
    expires_at = datetime.now(UTC) + timedelta(days=2)
    engine = advanced.create(
        owner,
        routine.routine_id,
        _advanced_request(
            routine.revision,
            {
                "kind": "AGENT_ENGINE_SWITCH",
                "action": "APPLY",
                "agentId": "dwaion-personal-routine-agent",
                "engineId": "dwp-agent-kernel-v2",
                "expiresAt": expires_at.isoformat(),
            },
        ),
    )
    budget = advanced.create(
        owner,
        routine.routine_id,
        _advanced_request(
            routine.revision,
            {
                "kind": "TEMPORARY_BUDGET_INCREASE",
                "additionalRuns": 1,
                "additionalTokensPerRun": 256,
                "additionalMinutesPerRun": 1,
                "expiresAt": expires_at.isoformat(),
            },
        ),
    )
    assert engine.state == budget.state == RoutineAdvancedCommandState.SUCCEEDED

    run_id = uuid4()
    lease_token = uuid4()
    now = datetime.now(UTC)
    with connect(DATABASE_URL, row_factory=dict_row) as connection:
        policy = load_routine_runtime_policy(
            connection,
            tenant_id=tenant_id,
            user_id=owner.user_id,
            routine_id=routine.routine_id,
            reference=now,
        )
        assert policy.engine_id == "dwp-agent-kernel-v2"
        assert policy.engine_state_version == 1
        assert policy.additional_tokens_per_run == 256
        assert policy.additional_minutes_per_run == 1
        _insert_budget_test_runs(
            connection, routine, owner, run_id, now
        )
        consumed = reserve_monthly_run_budget(
            connection,
            routine_run_id=run_id,
            tenant_id=tenant_id,
            user_id=owner.user_id,
            routine_id=routine.routine_id,
            maximum_runs=1,
            reference=now,
        )
        assert consumed == budget.command_id
        connection.execute(
            """UPDATE ai_personal_routine_executions
                  SET run_state = 'RUNNING', version = 2, attempt_count = 1,
                      lease_generation = 1, lease_token = %s,
                      lease_expires_at = %s, started_at = %s
                WHERE routine_run_id = %s""",
            (lease_token, now + timedelta(minutes=5), now, run_id),
        )

    captured: dict[str, object] = {}

    class _ExecutionProvider:
        def execute(self, **kwargs: object) -> RoutineProviderResult:
            captured.update(kwargs)
            applied = RoutineExecutionRuntimeControls.model_validate(
                kwargs["runtime_controls"]
            )
            return RoutineProviderResult(
                routineRunId=run_id,
                routineId=routine.routine_id,
                routineRevision=routine.revision,
                state="COMPLETED",
                providerReceiptId="runtime-execution-provider-receipt",
                resultSha256=hashlib.sha256(b"runtime-result").hexdigest(),
                evidenceCount=1,
                proposalsCreated=1,
                approvalGatedActionsCreated=0,
                externalWritesPerformed=0,
                tokensUsed=200,
                elapsedMs=90_000,
                notificationState="DELIVERED",
                compensationRequired=False,
                authorizationDecisionRevision=1,
                authorizedSources=["WORK_ITEM"],
                appliedRuntimeControls=applied,
                runtimeControlsSha256=runtime_controls_digest(applied),
            )

    worker = PostgresPersonalRoutineExecutionWorker(
        DATABASE_URL, provider=_ExecutionProvider()
    )
    worker._execute(
        RoutineExecutionLease(
            routine_run_id=run_id,
            routine_id=routine.routine_id,
            tenant_id=tenant_id,
            user_id=owner.user_id,
            routine_revision=routine.revision,
            generation=1,
            lease_token=lease_token,
            attempt_count=1,
            maximum_attempts=3,
            correlation_id="runtime-effect-execution",
            compensation_requested=False,
        )
    )
    controls = captured["runtime_controls"]
    assert controls["engineOverride"]["engineId"] == "dwp-agent-kernel-v2"
    assert controls["additionalTokensPerRun"] == 256
    with connect(DATABASE_URL, row_factory=dict_row) as connection:
        completed = connection.execute(
            """SELECT run_state, provider_receipt_envelope
                 FROM ai_personal_routine_executions WHERE routine_run_id = %s""",
            (run_id,),
        ).fetchone()
        assert completed["run_state"] == "COMPLETED"
        receipt = worker.codec.decrypt_json(
            completed["provider_receipt_envelope"],
            tenant_id=tenant_id,
            resource_type="personal-routine-execution",
            resource_id=str(run_id),
            field="provider-receipt",
        )
        assert receipt["runtimeControls"] == controls
        expired = load_routine_runtime_policy(
            connection,
            tenant_id=tenant_id,
            user_id=owner.user_id,
            routine_id=routine.routine_id,
            reference=expires_at + timedelta(seconds=1),
        )
        assert expired.engine_id is None
        assert expired.additional_tokens_per_run == 0

    rollback = advanced.create(
        owner,
        routine.routine_id,
        _advanced_request(
            routine.revision,
            {"kind": "AGENT_ENGINE_SWITCH", "action": "ROLLBACK"},
        ),
    )
    assert rollback.state == RoutineAdvancedCommandState.SUCCEEDED
    with connect(DATABASE_URL, row_factory=dict_row) as connection:
        rolled_back = load_routine_runtime_policy(
            connection,
            tenant_id=tenant_id,
            user_id=owner.user_id,
            routine_id=routine.routine_id,
            reference=datetime.now(UTC),
        )
        versions = connection.execute(
            """SELECT action, state_version
                 FROM ai_personal_routine_engine_override_events
                WHERE routine_id = %s ORDER BY state_version""",
            (routine.routine_id,),
        ).fetchall()
    assert rolled_back.engine_id is None
    assert [(row["action"], row["state_version"]) for row in versions] == [
        ("APPLY", 1),
        ("ROLLBACK", 2),
    ]


def _advanced_request(
    revision: int, payload: dict[str, object]
) -> CreateRoutineAdvancedCommandRequest:
    return CreateRoutineAdvancedCommandRequest(
        commandId=uuid4(),
        expectedRevision=revision,
        reasonCode=f"USER_ROUTINE_{payload['kind']}",
        changeReason="Apply the reviewed, bounded advanced routine runtime control.",
        payload=payload,
    )


def _insert_budget_test_runs(
    connection: object,
    routine: object,
    owner: PersonalDomainIdentity,
    run_id: object,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO ai_personal_routine_executions (
               routine_run_id, routine_id, tenant_id, user_id,
               routine_revision, trigger_type, run_state, scheduled_for,
               maximum_attempts, correlation_id, completed_at)
           VALUES (%s, %s, %s, %s, %s, 'MANUAL', 'CANCELLED', %s, 3, %s, %s)""",
        (
            uuid4(), routine.routine_id, owner.tenant_id, owner.user_id,
            routine.revision, now - timedelta(minutes=2), str(uuid4()), now,
        ),
    )
    connection.execute(
        """INSERT INTO ai_personal_routine_executions (
               routine_run_id, routine_id, tenant_id, user_id,
               routine_revision, trigger_type, run_state, scheduled_for,
               maximum_attempts, correlation_id)
           VALUES (%s, %s, %s, %s, %s, 'MANUAL', 'QUEUED', %s, 3, %s)""",
        (
            run_id, routine.routine_id, owner.tenant_id, owner.user_id,
            routine.revision, now, str(uuid4()),
        ),
    )


def _request_change(
    store: PersonalRoutineAdvancedCommandStore,
    maker: PersonalDomainIdentity,
    routine: object,
    name: str,
):
    return store.create(
        maker,
        routine.routine_id,
        CreateRoutineAdvancedCommandRequest(
            commandId=uuid4(),
            expectedRevision=routine.revision,
            reasonCode="USER_ROUTINE_CHANGE_APPROVAL",
            changeReason="Require an independent checker before changing this routine.",
            payload={"kind": "CHANGE_APPROVAL", "definition": _definition(name)},
        ),
    )


def _approve(command: object, evidence_ref: str) -> DecideRoutineAdvancedCommandRequest:
    return DecideRoutineAdvancedCommandRequest(
        commandId=uuid4(),
        expectedRevision=command.version,
        reasonCode="ROUTINE_CHANGE_CHECKER_DECISION",
        changeReason="Approve the reviewed definition with bound checker evidence.",
        decision="APPROVE",
        evidenceRefs=[evidence_ref],
    )


def _create_routine(
    identity: PersonalDomainIdentity,
    name: str,
    *,
    definition: RoutineDefinition | None = None,
):
    PostgresDomainRetentionStore(DATABASE_URL).upsert_policy(
        identity,
        DomainKey.ROUTINE,
        UpsertRetentionPolicyRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="TENANT_ROUTINE_RETENTION",
            changeReason="Set the governed retention boundary for routine verification.",
            retentionDays=365,
            deletionGraceDays=7,
            legalHold=False,
        ),
    )
    PostgresPersonalMemoryStore(DATABASE_URL).update_source_preference(
        identity,
        "WORK_ITEM",
        UpdateAiSourcePreferenceRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_ROUTINE_SOURCE",
            changeReason="Allow work items for this governed personal routine.",
            enabled=True,
        ),
    )
    return PostgresPersonalRoutineStore(DATABASE_URL).create(
        identity,
        CreateRoutineRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_ROUTINE_CREATE",
            definition=definition or _definition(name),
        ),
    )


def _definition(name: str) -> RoutineDefinition:
    return RoutineDefinition(
        name=name,
        objective="Create approval-gated work proposals with governed evidence.",
        cadence="WEEKDAYS",
        localTime="09:00",
        timeZone="Asia/Seoul",
        locale="ko-KR",
        sources=["WORK_ITEM"],
    )


def _identity(tenant_id: int, user_id: str, session_id: str) -> PersonalDomainIdentity:
    return PersonalDomainIdentity(
        tenant_id=tenant_id,
        user_id=user_id,
        correlation_id=str(uuid4()),
        auth_session_id=session_id,
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset(
            {
                "APP.ASK:VIEW",
                "APP.DWAION_PRIVACY:VIEW",
                "APP.DWAION_PRIVACY:MANAGE",
                "APP.DWAION_MEMORY:VIEW",
                "APP.DWAION_MEMORY:MANAGE",
                "APP.DWAION_ROUTINES:VIEW",
                "APP.DWAION_ROUTINES:MANAGE",
                "APP.WORK:VIEW",
            }
        ),
    )
