from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from threading import Barrier, Event
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.proposal_analysis_commands import (
    ProposalAnalysisInProgress,
    ProposalAnalysisReplay,
)
from dwp_agent.proposal_analysis_contracts import ProposalAnalysisReceipt
from dwp_agent.proposal_analysis_postgres_commands import (
    PostgresProposalAnalysisCommandStore,
)
from dwp_agent.proposal_privacy import PostgresProposalPrivacyService


DATABASE_URL = os.getenv("DWP_AGENT_ENVELOPE_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_ENVELOPE_TEST_DATABASE_URL is not configured.",
)


@pytest.mark.integration
def test_analysis_command_receipt_is_encrypted_and_canonically_replayed() -> None:
    _prepare()
    store = PostgresProposalAnalysisCommandStore(DATABASE_URL)
    tenant_id = _tenant_id()
    command_id = uuid4()

    try:
        started = store.begin(
            tenant_id=tenant_id,
            user_id="member-1",
            auth_session_id="session-1",
            command_id=command_id,
            locale="en",
        )
        assert started.lease is not None
        receipt = store.complete(
            tenant_id=tenant_id,
            user_id="member-1",
            lease=started.lease,
            receipt=_receipt(),
        )
        replay = store.begin(
            tenant_id=tenant_id,
            user_id="member-1",
            auth_session_id="session-1",
            command_id=command_id,
            locale="en",
        )
        with connect(DATABASE_URL) as connection:
            row = connection.execute(
                """SELECT status, generation, lease_token, result_envelope
                     FROM ai_agent_proposal_analysis_commands
                    WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
                (int(tenant_id), "member-1", command_id),
            ).fetchone()

        assert replay.replay == receipt
        assert row[:3] == ("COMPLETED", 1, None)
        assert row[3].startswith("dwp2.")
        assert "actionableProposals" not in row[3]
    finally:
        _truncate()


@pytest.mark.integration
def test_analysis_command_failed_generation_can_retry_but_stale_lease_cannot_win() -> None:
    _prepare()
    store = PostgresProposalAnalysisCommandStore(DATABASE_URL)
    tenant_id = _tenant_id()
    command_id = uuid4()

    try:
        first = store.begin(
            tenant_id=tenant_id,
            user_id="member-1",
            auth_session_id="session-1",
            command_id=command_id,
            locale="en",
        )
        assert first.lease is not None
        store.fail(
            tenant_id=tenant_id,
            user_id="member-1",
            lease=first.lease,
        )
        second = store.begin(
            tenant_id=tenant_id,
            user_id="member-1",
            auth_session_id="session-1",
            command_id=command_id,
            locale="en",
        )
        assert second.lease is not None
        assert second.lease.generation == 2

        with pytest.raises(ProposalAnalysisReplay):
            store.complete(
                tenant_id=tenant_id,
                user_id="member-1",
                lease=first.lease,
                receipt=_receipt(),
            )
        store.complete(
            tenant_id=tenant_id,
            user_id="member-1",
            lease=second.lease,
            receipt=_receipt(),
        )
        with connect(DATABASE_URL) as connection:
            assert connection.execute(
                """SELECT status, generation, attempt_count
                     FROM ai_agent_proposal_analysis_commands
                    WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
                (int(tenant_id), "member-1", command_id),
            ).fetchone() == ("COMPLETED", 2, 2)
    finally:
        _truncate()


@pytest.mark.integration
def test_analysis_command_parallel_claim_and_payload_drift_fail_closed() -> None:
    _prepare()
    store = PostgresProposalAnalysisCommandStore(DATABASE_URL)
    tenant_id = _tenant_id()
    command_id = uuid4()
    barrier = Barrier(3)

    def begin_once():
        barrier.wait()
        try:
            return store.begin(
                tenant_id=tenant_id,
                user_id="member-1",
                auth_session_id="session-1",
                command_id=command_id,
                locale="en",
            )
        except ProposalAnalysisInProgress as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(begin_once) for _ in range(2)]
            barrier.wait()
            outcomes = [future.result() for future in futures]
        starts = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
        assert len(starts) == 1
        assert sum(isinstance(value, ProposalAnalysisInProgress) for value in outcomes) == 1

        with pytest.raises(ProposalAnalysisReplay):
            store.begin(
                tenant_id=tenant_id,
                user_id="member-1",
                auth_session_id="session-2",
                command_id=command_id,
                locale="en",
            )
        with pytest.raises(ProposalAnalysisReplay):
            store.begin(
                tenant_id=tenant_id,
                user_id="member-1",
                auth_session_id="session-1",
                command_id=command_id,
                locale="ko",
            )
    finally:
        _truncate()


@pytest.mark.integration
def test_clear_privacy_command_fences_analysis_receipt_in_same_database() -> None:
    _prepare()
    store = PostgresProposalAnalysisCommandStore(DATABASE_URL)
    tenant_id = _tenant_id()
    command_id = uuid4()

    try:
        started = store.begin(
            tenant_id=tenant_id,
            user_id="member-1",
            auth_session_id="session-1",
            command_id=command_id,
            locale="en",
        )
        assert started.lease is not None
        store.complete(
            tenant_id=tenant_id,
            user_id="member-1",
            lease=started.lease,
            receipt=_receipt(),
        )

        PostgresProposalPrivacyService(DATABASE_URL).clear(
            tenant_id=tenant_id,
            user_id="member-1",
            correlation_id="proposal-clear",
            command_id=uuid4(),
        )
        with connect(DATABASE_URL) as connection:
            assert connection.execute(
                """SELECT status, generation, lease_token, completed_at,
                          result_envelope
                     FROM ai_agent_proposal_analysis_commands
                    WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
                (int(tenant_id), "member-1", command_id),
            ).fetchone() == ("FAILED", 2, None, None, None)
    finally:
        _truncate()


@pytest.mark.integration
def test_clear_serializes_with_persistence_and_rejects_the_stale_lease() -> None:
    _prepare()
    store = PostgresProposalAnalysisCommandStore(DATABASE_URL)
    tenant_id = _tenant_id()
    command_id = uuid4()
    started = store.begin(
        tenant_id=tenant_id,
        user_id="member-1",
        auth_session_id="session-1",
        command_id=command_id,
        locale="en",
    )
    assert started.lease is not None
    entered = Event()
    release = Event()

    def persistence() -> str:
        def operation() -> str:
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("Timed out waiting to release persistence.")
            return "persisted"

        return store.run_under_lease(
            tenant_id=tenant_id,
            user_id="member-1",
            lease=started.lease,
            operation=operation,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            persistence_future = executor.submit(persistence)
            assert entered.wait(timeout=5)
            clear_future = executor.submit(
                PostgresProposalPrivacyService(DATABASE_URL).clear,
                tenant_id=tenant_id,
                user_id="member-1",
                correlation_id="proposal-clear",
                command_id=uuid4(),
            )
            with pytest.raises(FutureTimeoutError):
                clear_future.result(timeout=0.15)
            release.set()
            assert persistence_future.result(timeout=5) == "persisted"
            assert clear_future.result(timeout=5).hidden_count == 0

        callback_called = False

        def stale_operation() -> None:
            nonlocal callback_called
            callback_called = True

        with pytest.raises(ProposalAnalysisReplay):
            store.run_under_lease(
                tenant_id=tenant_id,
                user_id="member-1",
                lease=started.lease,
                operation=stale_operation,
            )
        assert callback_called is False
    finally:
        release.set()
        _truncate()


def _receipt() -> ProposalAnalysisReceipt:
    return ProposalAnalysisReceipt(
        analyzed_at=datetime.now(timezone.utc),
        sources_analyzed=0,
        actionable_proposals=0,
        attempted_sources=[],
        unavailable_sources=[],
        proposals=[],
    )


def _tenant_id() -> str:
    return str(850_000_000 + uuid4().int % 100_000_000)


def _prepare() -> None:
    database_name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Proposal analysis tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)
    _truncate()


def _truncate() -> None:
    with connect(DATABASE_URL) as connection:
        connection.execute(
            """TRUNCATE TABLE
                   ai_agent_proposal_analysis_commands,
                   ai_agent_proposal_preference_events,
                   ai_agent_proposal_preferences,
                   ai_agent_proposal_events,
                   ai_agent_proposals"""
        )
