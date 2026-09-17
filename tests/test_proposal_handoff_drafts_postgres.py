from __future__ import annotations

import os
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from psycopg import Error as PsycopgError, connect

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
)
from dwp_agent.governed_domain_core import GovernedPayloadCodec
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.proposal_handoff_draft_contracts import SaveProposalHandoffDraftRequest
from dwp_agent.proposal_handoff_draft_store import ProposalHandoffDraftStore


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
        pytest.fail("Proposal handoff draft tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)


def test_handoff_drafts_are_encrypted_append_only_scoped_and_idempotent() -> None:
    identity = _identity()
    proposal_id, handoff_id = _seed_handoff(identity)
    store = ProposalHandoffDraftStore(DATABASE_URL)
    command_id = uuid4()
    first_request = SaveProposalHandoffDraftRequest(
        commandId=command_id,
        expectedVersion=1,
        reviewedInputs={"title": "Quarterly approval", "amount": 1200},
    )

    first = store.save(identity, handoff_id, first_request)
    replay = store.save(identity, handoff_id, first_request)

    assert replay == first
    assert first.proposal_id == proposal_id
    assert first.handoff_version == 1
    assert first.revision == 1
    assert first.reviewed_inputs == {
        "title": "Quarterly approval",
        "amount": 1200,
    }
    assert len(first.content_sha256) == 64
    assert store.current(identity, handoff_id) == first

    with pytest.raises(DwaionWorkflowConflict, match="already bound"):
        store.save(
            identity,
            handoff_id,
            SaveProposalHandoffDraftRequest(
                commandId=command_id,
                expectedVersion=1,
                reviewedInputs={"title": "A different command payload"},
            ),
        )

    second = store.save(
        identity,
        handoff_id,
        SaveProposalHandoffDraftRequest(
            commandId=uuid4(),
            expectedVersion=1,
            reviewedInputs={"title": "Quarterly approval v2", "amount": 1300},
        ),
    )
    assert second.revision == 2
    assert store.current(identity, handoff_id) == second

    with pytest.raises(DwaionWorkflowNotFound):
        store.current(_identity(tenant_id=identity.tenant_id, user="other-user"), handoff_id)

    with connect(DATABASE_URL) as connection:
        rows = connection.execute(
            """SELECT reviewed_inputs_envelope, actor_user_id, correlation_id
                 FROM ai_proposal_handoff_draft_versions
                WHERE handoff_id = %s ORDER BY draft_revision""",
            (handoff_id,),
        ).fetchall()
    assert len(rows) == 2
    assert all(row[0].startswith("dwp2.") for row in rows)
    assert all("Quarterly approval" not in row[0] for row in rows)
    assert rows[0][1:] == (identity.user_id, identity.correlation_id)

    with pytest.raises(PsycopgError):
        with connect(DATABASE_URL) as connection:
            connection.execute(
                """UPDATE ai_proposal_handoff_draft_versions
                      SET content_sha256 = %s WHERE draft_id = %s""",
                ("f" * 64, first.draft_id),
            )


def test_handoff_draft_rejects_stale_handoff_revision() -> None:
    identity = _identity()
    _, handoff_id = _seed_handoff(identity, handoff_version=2)
    store = ProposalHandoffDraftStore(DATABASE_URL)

    with pytest.raises(DwaionWorkflowConflict, match="version has changed"):
        store.save(
            identity,
            handoff_id,
            SaveProposalHandoffDraftRequest(
                commandId=uuid4(),
                expectedVersion=1,
                reviewedInputs={"title": "Stale"},
            ),
        )


def _seed_handoff(
    identity: PersonalDomainIdentity, *, handoff_version: int = 1
) -> tuple[UUID, UUID]:
    proposal_id = uuid4()
    handoff_id = uuid4()
    codec = GovernedPayloadCodec()
    proposal_envelope = codec.encrypt_json(
        {"title": "Governed proposal"},
        tenant_id=identity.tenant_id,
        resource_type="proposal-test-fixture",
        resource_id=str(proposal_id),
        field="payload",
    )
    handoff_envelope = codec.encrypt_json(
        {"title": "Quarterly approval"},
        tenant_id=identity.tenant_id,
        resource_type="proposal-handoff-test-fixture",
        resource_id=str(handoff_id),
        field="reviewed-inputs",
    )
    with connect(DATABASE_URL) as connection:
        connection.execute(
            """INSERT INTO ai_agent_proposals (
                   proposal_id, tenant_id, target_user_id, source_event_id,
                   creation_command_id, created_by_user_id, request_fingerprint, kind,
                   priority, state, revision, agent_key, action_key, payload_envelope,
                   proposed_at, available_at, expires_at, decided_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'APPROVAL', 'HIGH',
                       'ACCEPTED', 1, 'PROPOSAL_TEST', 'APPROVAL.REQUEST.CREATE', %s,
                       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                       CURRENT_TIMESTAMP + INTERVAL '1 day', CURRENT_TIMESTAMP,
                       CURRENT_TIMESTAMP)""",
            (
                proposal_id,
                identity.tenant_id,
                identity.user_id,
                f"source-{uuid4()}",
                uuid4(),
                identity.user_id,
                "a" * 64,
                proposal_envelope,
            ),
        )
        connection.execute(
            """INSERT INTO ai_proposal_handoffs (
                   handoff_id, proposal_id, tenant_id, user_id, command_id,
                   idempotency_key, action_key, target_route, handoff_state,
                   revision, approval_required, request_fingerprint,
                   reviewed_inputs_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, 'APPROVAL.REQUEST.CREATE',
                       '/approvals/requests/new', 'REVIEW_REQUIRED', %s, TRUE, %s, %s)""",
            (
                handoff_id,
                proposal_id,
                identity.tenant_id,
                identity.user_id,
                uuid4(),
                uuid4(),
                handoff_version,
                "b" * 64,
                handoff_envelope,
            ),
        )
    return proposal_id, handoff_id


def _identity(
    tenant_id: int | None = None, *, user: str = "member-1"
) -> PersonalDomainIdentity:
    return PersonalDomainIdentity(
        tenant_id=tenant_id or (880_000_000 + uuid4().int % 100_000_000),
        user_id=user,
        correlation_id=str(uuid4()),
        auth_session_id="session-1",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset({"APP.ASK:VIEW"}),
    )
