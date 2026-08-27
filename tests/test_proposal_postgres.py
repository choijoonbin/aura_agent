from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import Error as PsycopgError
from psycopg import connect

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.postgres_proposal_store import PostgresProposalStore
from dwp_agent.proposal_contracts import (
    CreateAgentProposalRequest,
    DecideAgentProposalRequest,
    ProposalContent,
    ProposalDecision,
    ProposalEvidence,
    ProposalInboxView,
    ProposalKind,
    ProposalPriority,
)
from dwp_agent.proposal_store import ProposalConflict, ProposalNotFound


DATABASE_URL = os.getenv("DWP_AGENT_ENVELOPE_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_ENVELOPE_TEST_DATABASE_URL is not configured.",
)


def proposal_request(*, target_user_id: str, command_id=None) -> CreateAgentProposalRequest:
    return CreateAgentProposalRequest(
        command_id=command_id or uuid4(),
        target_user_id=target_user_id,
        source_event_id="work-risk-2026-08-27",
        kind=ProposalKind.RISK,
        priority=ProposalPriority.HIGH,
        agent_key="DWP_ASSISTANT",
        action_key="SERVICE.REQUEST.CREATE",
        content=ProposalContent(
            title="비공개 프로젝트 위험 신호",
            summary="마감 전 확인이 필요한 업무가 있습니다.",
            rationale="마감일과 미완료 상태를 함께 분석했습니다.",
            action_inputs={
                "serviceCategory": "WORK_SUPPORT",
                "requestSummary": "프로젝트 위험 업무 지원 요청",
            },
            evidence=[
                ProposalEvidence(
                    source_type="WORK_ITEM",
                    reference_id="work-100",
                    label="고객 전환 계획 검토",
                )
            ],
        ),
        expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        change_reason="실제 업무 위험 신호를 사용자에게 제안합니다.",
    )


@pytest.mark.integration
def test_postgres_proposal_is_encrypted_scoped_audited_and_read_only() -> None:
    _prepare()
    store = PostgresProposalStore(DATABASE_URL)
    tenant_id = str(890_000_000 + uuid4().int % 90_000_000)
    request = proposal_request(target_user_id="member-1")

    try:
        created = store.create(
            tenant_id=tenant_id,
            actor_user_id="operator-1",
            correlation_id="proposal-create",
            request=request,
        )
        with connect(DATABASE_URL) as connection:
            envelope, event_note, fingerprint, before_events = connection.execute(
                """SELECT p.payload_envelope, e.note_envelope, p.request_fingerprint,
                          (SELECT COUNT(*) FROM ai_agent_proposal_events
                            WHERE tenant_id = %s)
                     FROM ai_agent_proposals p
                     JOIN ai_agent_proposal_events e USING (proposal_id)
                    WHERE p.proposal_id = %s""",
                (int(tenant_id), created.proposal_id),
            ).fetchone()
        assert envelope.startswith("dwp2.")
        assert event_note.startswith("dwp2.")
        assert "비공개" not in envelope
        assert request.change_reason not in event_note
        assert fingerprint == store.fingerprints.create(int(tenant_id), request)
        assert fingerprint != store.fingerprints.create(int(tenant_id) + 1, request)

        inbox = store.list(
            tenant_id=tenant_id,
            user_id="member-1",
            view=ProposalInboxView.ACTIVE,
            limit=50,
            cursor=None,
        )
        other = store.list(
            tenant_id=tenant_id,
            user_id="member-2",
            view=ProposalInboxView.ALL,
            limit=50,
            cursor=None,
        )
        with connect(DATABASE_URL) as connection:
            after_events = connection.execute(
                "SELECT COUNT(*) FROM ai_agent_proposal_events WHERE tenant_id = %s",
                (int(tenant_id),),
            ).fetchone()[0]
        assert inbox.items[0].content.title == request.content.title
        assert inbox.summary.active == 1
        assert other.items == []
        assert after_events == before_events

        with pytest.raises(ProposalNotFound):
            store.decide(
                tenant_id=tenant_id,
                user_id="member-2",
                correlation_id="cross-user",
                proposal_id=created.proposal_id,
                request=DecideAgentProposalRequest(
                    command_id=uuid4(),
                    expected_revision=1,
                    decision=ProposalDecision.ACCEPT,
                ),
            )
        snoozed = store.decide(
            tenant_id=tenant_id,
            user_id="member-1",
            correlation_id="proposal-snooze",
            proposal_id=created.proposal_id,
            request=DecideAgentProposalRequest(
                command_id=uuid4(),
                expected_revision=1,
                decision=ProposalDecision.SNOOZE,
                snooze_until=datetime.now(timezone.utc) + timedelta(hours=2),
                note="다음 집중 시간에 다시 검토합니다.",
            ),
        )
        assert snoozed.state.value == "SNOOZED"
        assert store.list(
            tenant_id=tenant_id,
            user_id="member-1",
            view=ProposalInboxView.SNOOZED,
            limit=50,
            cursor=None,
        ).summary.snoozed == 1

        with connect(DATABASE_URL) as connection:
            with pytest.raises(PsycopgError):
                connection.execute(
                    "UPDATE ai_agent_proposal_events SET revision = 99 WHERE proposal_id = %s",
                    (created.proposal_id,),
                )
            connection.rollback()
    finally:
        _truncate()


@pytest.mark.integration
def test_postgres_proposal_source_event_is_atomic_and_idempotent() -> None:
    _prepare()
    store = PostgresProposalStore(DATABASE_URL)
    tenant_id = str(980_000_000 + uuid4().int % 10_000_000)
    barrier = Barrier(3)
    base_request = proposal_request(target_user_id="member-1")
    requests = [
        base_request.model_copy(update={"command_id": uuid4()})
        for _ in range(2)
    ]

    def create_once(request: CreateAgentProposalRequest):
        barrier.wait()
        return store.create(
            tenant_id=tenant_id,
            actor_user_id="operator-1",
            correlation_id=str(uuid4()),
            request=request,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(create_once, request) for request in requests
            ]
            barrier.wait()
            proposals = [future.result() for future in futures]
        with connect(DATABASE_URL) as connection:
            proposal_count, event_count = connection.execute(
                """SELECT
                       (SELECT COUNT(*) FROM ai_agent_proposals WHERE tenant_id = %s),
                       (SELECT COUNT(*) FROM ai_agent_proposal_events WHERE tenant_id = %s)""",
                (int(tenant_id), int(tenant_id)),
            ).fetchone()
        assert proposals[0].proposal_id == proposals[1].proposal_id
        assert (proposal_count, event_count) == (1, 1)
    finally:
        _truncate()


@pytest.mark.integration
def test_postgres_creation_command_race_is_conflict_not_storage_failure() -> None:
    _prepare()
    store = PostgresProposalStore(DATABASE_URL)
    tenant_id = str(970_000_000 + uuid4().int % 10_000_000)
    command_id = uuid4()
    barrier = Barrier(3)
    requests = [
        proposal_request(target_user_id="member-1", command_id=command_id).model_copy(
            update={"source_event_id": f"work-risk-{index}"}
        )
        for index in range(2)
    ]

    def create_once(request: CreateAgentProposalRequest):
        barrier.wait()
        try:
            return store.create(
                tenant_id=tenant_id,
                actor_user_id="operator-1",
                correlation_id=str(uuid4()),
                request=request,
            )
        except ProposalConflict as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(create_once, request) for request in requests]
            barrier.wait()
            outcomes = [future.result() for future in futures]
        assert sum(isinstance(outcome, ProposalConflict) for outcome in outcomes) == 1
        assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
        with connect(DATABASE_URL) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM ai_agent_proposals WHERE tenant_id = %s",
                (int(tenant_id),),
            ).fetchone()[0] == 1
    finally:
        _truncate()


@pytest.mark.integration
def test_postgres_decision_command_race_replays_one_canonical_result() -> None:
    _prepare()
    store = PostgresProposalStore(DATABASE_URL)
    tenant_id = str(960_000_000 + uuid4().int % 10_000_000)
    proposal = store.create(
        tenant_id=tenant_id,
        actor_user_id="operator-1",
        correlation_id="proposal-create",
        request=proposal_request(target_user_id="member-1"),
    )
    command_id = uuid4()
    decision = DecideAgentProposalRequest(
        command_id=command_id,
        expected_revision=1,
        decision=ProposalDecision.ACCEPT,
        note="사용자가 검토를 계속하기로 했습니다.",
    )
    barrier = Barrier(3)

    def decide_once():
        barrier.wait()
        return store.decide(
            tenant_id=tenant_id,
            user_id="member-1",
            correlation_id=str(uuid4()),
            proposal_id=proposal.proposal_id,
            request=decision,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(decide_once) for _ in range(2)]
            barrier.wait()
            outcomes = [future.result() for future in futures]
        assert {(outcome.state.value, outcome.revision) for outcome in outcomes} == {
            ("ACCEPTED", 2)
        }
        with connect(DATABASE_URL) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM ai_agent_proposal_events WHERE tenant_id = %s",
                (int(tenant_id),),
            ).fetchone()[0] == 2

        with pytest.raises(ProposalConflict):
            store.decide(
                tenant_id=tenant_id,
                user_id="member-1",
                correlation_id="proposal-drift",
                proposal_id=proposal.proposal_id,
                request=decision.model_copy(update={"expected_revision": 2}),
            )
    finally:
        _truncate()


def _prepare() -> None:
    database_name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Agent proposal tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)
    _truncate()


def _truncate() -> None:
    with connect(DATABASE_URL) as connection:
        connection.execute(
            "TRUNCATE TABLE ai_agent_proposal_events, ai_agent_proposals"
        )
