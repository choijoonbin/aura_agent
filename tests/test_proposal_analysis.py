from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
from uuid import uuid4

import pytest

from dwp_agent.context_broker import (
    ContextBrokerUnavailable,
    GroundedContext,
    GroundedSource,
)
from dwp_agent.contracts import AskCitation, CitationSourceType
from dwp_agent.policy import AskIdentity
from dwp_agent.proposal_analysis import ProposalAnalysisService
from dwp_agent.proposal_analysis_commands import (
    InMemoryProposalAnalysisCommandStore,
    ProposalAnalysisInProgress,
    ProposalAnalysisReplay,
)
from dwp_agent.proposal_analysis_contracts import ProposalAnalysisReceipt
from dwp_agent.proposal_analysis_control import InMemoryProposalAnalysisControl
from dwp_agent.proposal_analysis_fingerprints import ProposalAnalysisFingerprints
from dwp_agent.proposal_contracts import ProposalInboxView
from dwp_agent.proposal_privacy import InMemoryProposalPrivacyService
from dwp_agent.proposal_store import InMemoryProposalStore, set_proposal_store_for_tests


class FakeBroker:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls = 0
        self.fail_once = fail_once

    def collect(self, *_args, **_kwargs) -> GroundedContext:
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise ContextBrokerUnavailable("Context broker is temporarily unavailable.")
        return _context()


class BlockingBroker:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def collect(self, *_args, **_kwargs) -> GroundedContext:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("Timed out waiting to release the proposal analysis broker.")
        return _context()


@pytest.fixture
def proposal_store():
    store = InMemoryProposalStore()
    set_proposal_store_for_tests(store)
    yield store
    set_proposal_store_for_tests(None)


def test_analysis_replays_canonical_receipt_without_recollecting_context(
    proposal_store: InMemoryProposalStore,
) -> None:
    broker = FakeBroker()
    service = _service(broker)
    command_id = uuid4()

    first = service.analyze(
        identity=_identity(),
        command_id=command_id,
        locale="en",
        auth_session_id="session-1",
    )
    replay = service.analyze(
        identity=_identity(),
        command_id=command_id,
        locale="en",
        auth_session_id="session-1",
    )

    assert replay == first
    assert first.actionable_proposals == 1
    assert broker.calls == 1
    assert _active_count(proposal_store) == 1


def test_analysis_retry_after_broker_failure_keeps_one_command_and_proposal(
    proposal_store: InMemoryProposalStore,
) -> None:
    broker = FakeBroker(fail_once=True)
    service = _service(broker)
    command_id = uuid4()

    with pytest.raises(ContextBrokerUnavailable):
        service.analyze(
            identity=_identity(),
            command_id=command_id,
            locale="en",
            auth_session_id="session-1",
        )
    receipt = service.analyze(
        identity=_identity(),
        command_id=command_id,
        locale="en",
        auth_session_id="session-1",
    )

    assert receipt.actionable_proposals == 1
    assert broker.calls == 2
    assert _active_count(proposal_store) == 1


def test_source_identity_is_locale_independent(
    proposal_store: InMemoryProposalStore,
) -> None:
    broker = FakeBroker()
    service = _service(broker)

    english = service.analyze(
        identity=_identity(),
        command_id=uuid4(),
        locale="en",
        auth_session_id="session-1",
    )
    korean = service.analyze(
        identity=_identity(),
        command_id=uuid4(),
        locale="ko",
        auth_session_id="session-1",
    )

    assert english.actionable_proposals == 1
    assert korean.actionable_proposals == 0
    assert _active_count(proposal_store) == 1


def test_command_lease_fences_parallel_and_drifted_retries() -> None:
    store = InMemoryProposalAnalysisCommandStore(
        ProposalAnalysisFingerprints.ephemeral()
    )
    command_id = uuid4()
    first = store.begin(
        tenant_id="42",
        user_id="member-1",
        auth_session_id="session-1",
        command_id=command_id,
        locale="en",
    )
    assert first.lease is not None

    with pytest.raises(ProposalAnalysisInProgress):
        store.begin(
            tenant_id="42",
            user_id="member-1",
            auth_session_id="session-1",
            command_id=command_id,
            locale="en",
        )
    with pytest.raises(ProposalAnalysisReplay):
        store.begin(
            tenant_id="42",
            user_id="member-1",
            auth_session_id="session-2",
            command_id=command_id,
            locale="en",
        )

    store.fail(tenant_id="42", user_id="member-1", lease=first.lease)
    with pytest.raises(ProposalAnalysisReplay):
        store.begin(
            tenant_id="42",
            user_id="member-1",
            auth_session_id="session-1",
            command_id=command_id,
            locale="ko",
        )
    second = store.begin(
        tenant_id="42",
        user_id="member-1",
        auth_session_id="session-1",
        command_id=command_id,
        locale="en",
    )
    assert second.lease is not None
    assert second.lease.generation == 2
    with pytest.raises(ProposalAnalysisReplay):
        store.complete(
            tenant_id="42",
            user_id="member-1",
            lease=first.lease,
            receipt=_empty_receipt(),
        )

    canonical = store.complete(
        tenant_id="42",
        user_id="member-1",
        lease=second.lease,
        receipt=_empty_receipt(),
    )
    replay = store.begin(
        tenant_id="42",
        user_id="member-1",
        auth_session_id="session-1",
        command_id=command_id,
        locale="en",
    )
    assert replay.replay == canonical


def test_clear_fences_completed_receipt_before_redacting_inbox(
    proposal_store: InMemoryProposalStore,
) -> None:
    fingerprints = ProposalAnalysisFingerprints.ephemeral()
    commands = InMemoryProposalAnalysisCommandStore(fingerprints)
    broker = FakeBroker()
    service = ProposalAnalysisService(
        broker=broker,
        fingerprints=fingerprints,
        control=InMemoryProposalAnalysisControl(fingerprints),
        commands=commands,
    )
    command_id = uuid4()
    first = service.analyze(
        identity=_identity(),
        command_id=command_id,
        locale="en",
        auth_session_id="session-1",
    )

    cleared = InMemoryProposalPrivacyService(
        proposal_store,
        commands,
    ).clear(
        tenant_id="42",
        user_id="member-1",
        correlation_id="proposal-clear",
        command_id=uuid4(),
    )
    retried = service.analyze(
        identity=_identity(),
        command_id=command_id,
        locale="en",
        auth_session_id="session-1",
    )

    assert first.actionable_proposals == 1
    assert cleared.hidden_count == 1
    assert broker.calls == 2
    assert retried.analyzed_at != first.analyzed_at


def test_clear_fences_inflight_analysis_before_it_can_repopulate_the_inbox(
    proposal_store: InMemoryProposalStore,
) -> None:
    fingerprints = ProposalAnalysisFingerprints.ephemeral()
    commands = InMemoryProposalAnalysisCommandStore(fingerprints)
    broker = BlockingBroker()
    service = ProposalAnalysisService(
        broker=broker,
        fingerprints=fingerprints,
        control=InMemoryProposalAnalysisControl(fingerprints),
        commands=commands,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        analysis = executor.submit(
            service.analyze,
            identity=_identity(),
            command_id=uuid4(),
            locale="en",
            auth_session_id="session-1",
        )
        assert broker.started.wait(timeout=5)
        InMemoryProposalPrivacyService(proposal_store, commands).clear(
            tenant_id="42",
            user_id="member-1",
            correlation_id="proposal-clear",
            command_id=uuid4(),
        )
        broker.release.set()
        with pytest.raises(ProposalAnalysisReplay):
            analysis.result(timeout=5)

    assert _active_count(proposal_store) == 0


def _service(broker: FakeBroker) -> ProposalAnalysisService:
    fingerprints = ProposalAnalysisFingerprints.ephemeral()
    return ProposalAnalysisService(
        broker=broker,
        fingerprints=fingerprints,
        control=InMemoryProposalAnalysisControl(fingerprints),
        commands=InMemoryProposalAnalysisCommandStore(fingerprints),
    )


def _identity() -> AskIdentity:
    return AskIdentity(
        tenant_id="42",
        user_id="member-1",
        roles=("WORKSPACE_MEMBER",),
        permissions=("APP.ASK:VIEW",),
        correlation_id="proposal-analysis-test",
    )


def _context() -> GroundedContext:
    occurred_at = datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc)
    return GroundedContext(
        sources=(
            GroundedSource(
                citation=AskCitation(
                    source_id="src-42",
                    source_type=CitationSourceType.WORK_ITEM,
                    title="Review a priority work item",
                    source_system="Work",
                    route="/work/items/42",
                    occurred_at=occurred_at,
                ),
                evidence="priority=HIGH|status=OPEN|dueAt=2026-08-28T14:00:00+00:00",
                rank=1,
            ),
        ),
        attempted_sources=("WORK_ITEM",),
        unavailable_sources=(),
    )


def _active_count(store: InMemoryProposalStore) -> int:
    return store.list(
        tenant_id="42",
        user_id="member-1",
        view=ProposalInboxView.ACTIVE,
        limit=50,
        cursor=None,
    ).summary.active


def _empty_receipt() -> ProposalAnalysisReceipt:
    return ProposalAnalysisReceipt(
        analyzed_at=datetime.now(timezone.utc),
        sources_analyzed=0,
        actionable_proposals=0,
        attempted_sources=[],
        unavailable_sources=[],
        proposals=[],
    )
