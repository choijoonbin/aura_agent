from __future__ import annotations

import os
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from psycopg import connect

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.dwaion_workflow_contracts import (
    ProposalHandoffObservation,
    ProposalHandoffState,
)
from dwp_agent.dwaion_workflow_errors import DwaionWorkflowConflict
from dwp_agent.governed_domain_core import GovernedPayloadCodec
from dwp_agent.personal_domain_security import PersonalDomainIdentity
from dwp_agent.proposal_handoff_store import ProposalHandoffStore


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
        pytest.fail("Proposal handoff receipt tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)


def test_completion_receipt_is_domain_typed_exactly_bound_and_encrypted() -> None:
    identity = _identity()
    proposal_id, handoff_id = _seed(identity)
    store = ProposalHandoffStore(DATABASE_URL)

    handed_off = store.observe(
        identity,
        handoff_id,
        ProposalHandoffObservation(
            commandId=uuid4(),
            expectedVersion=1,
            state=ProposalHandoffState.HANDED_OFF,
        ),
    )
    running = store.observe(
        identity,
        handoff_id,
        ProposalHandoffObservation(
            commandId=uuid4(),
            expectedVersion=handed_off.version,
            state=ProposalHandoffState.RUNNING,
        ),
    )
    request_id = uuid4()
    receipt = _receipt(
        identity,
        handoff_id=handoff_id,
        proposal_id=proposal_id,
        handoff_version=running.version,
        request_id=request_id,
    )

    with pytest.raises(DwaionWorkflowConflict, match="does not match"):
        store.observe(
            identity,
            handoff_id,
            receipt.model_copy(
                update={
                    "command_id": uuid4(),
                    "receipt": receipt.receipt.model_copy(
                        update={"proposal_id": uuid4()}
                    ),
                }
            ),
        )
    assert store.get(identity, handoff_id).state == ProposalHandoffState.RUNNING

    completed = store.observe(identity, handoff_id, receipt)
    replay = store.observe(identity, handoff_id, receipt)

    assert completed.state == ProposalHandoffState.COMPLETED
    assert replay == completed
    assert completed.receipt_id is not None
    with connect(DATABASE_URL) as connection:
        envelope = connection.execute(
            "SELECT receipt_envelope FROM ai_proposal_handoffs WHERE handoff_id = %s",
            (handoff_id,),
        ).fetchone()[0]
    assert envelope.startswith("dwp2.")
    assert str(request_id) not in envelope

    for changed_receipt in (
        receipt.model_copy(
            update={
                "receipt": receipt.receipt.model_copy(update={"request_id": uuid4()})
            }
        ),
        receipt.model_copy(
            update={
                "receipt": receipt.receipt.model_copy(update={"request_version": 3})
            }
        ),
    ):
        with pytest.raises(DwaionWorkflowConflict, match="already in use"):
            store.observe(identity, handoff_id, changed_receipt)


def test_completion_receipt_rejects_correlation_and_observation_version_mismatch() -> None:
    identity = _identity()
    proposal_id, handoff_id = _seed(identity, state="RUNNING", revision=3)
    store = ProposalHandoffStore(DATABASE_URL)
    base = _receipt(
        identity,
        handoff_id=handoff_id,
        proposal_id=proposal_id,
        handoff_version=3,
        request_id=uuid4(),
    )

    for mismatched in (
        base.model_copy(
            update={
                "command_id": uuid4(),
                "receipt": base.receipt.model_copy(update={"correlation_id": "other"}),
            }
        ),
        base.model_copy(
            update={
                "command_id": uuid4(),
                "receipt": base.receipt.model_copy(update={"handoff_version": 2}),
            }
        ),
    ):
        with pytest.raises(DwaionWorkflowConflict, match="does not match"):
            store.observe(identity, handoff_id, mismatched)
    assert store.get(identity, handoff_id).state == ProposalHandoffState.RUNNING


@pytest.mark.parametrize(
    ("action_key", "receipt_fields", "resource_field"),
    [
        (
            "CALENDAR.EVENT.CREATE",
            {
                "domain": "CALENDAR",
                "operation": "EVENT_CREATE",
                "eventId": uuid4(),
                "eventVersion": 0,
                "status": "CONFIRMED",
            },
            "eventId",
        ),
        (
            "MAIL.DRAFT.CREATE",
            {
                "domain": "MAIL",
                "operation": "DRAFT_CREATE",
                "threadId": uuid4(),
                "threadVersion": 0,
                "status": "DRAFT",
            },
            "threadId",
        ),
        (
            "SERVICE.REQUEST.CREATE",
            {
                "domain": "SERVICE",
                "operation": "REQUEST_CREATE",
                "requestId": uuid4(),
                "requestVersion": 0,
                "status": "SUBMITTED",
            },
            "requestId",
        ),
    ],
)
def test_owner_domain_completion_receipts_are_exactly_bound_and_persisted(
    action_key: str,
    receipt_fields: dict[str, object],
    resource_field: str,
) -> None:
    identity = _identity()
    proposal_id, handoff_id = _seed(
        identity, state="RUNNING", revision=3, action_key=action_key
    )
    store = ProposalHandoffStore(DATABASE_URL)
    observation = ProposalHandoffObservation(
        commandId=uuid4(),
        expectedVersion=3,
        state=ProposalHandoffState.COMPLETED,
        receipt=receipt_fields
        | {
            "handoffId": handoff_id,
            "proposalId": proposal_id,
            "actionKey": action_key,
            "handoffVersion": 3,
            "committedAt": datetime.now(UTC),
            "correlationId": identity.correlation_id,
        },
    )

    completed = store.observe(identity, handoff_id, observation)

    assert completed.state == ProposalHandoffState.COMPLETED
    assert completed.receipt_id is not None
    with connect(DATABASE_URL) as connection:
        envelope = connection.execute(
            "SELECT receipt_envelope FROM ai_proposal_handoffs WHERE handoff_id = %s",
            (handoff_id,),
        ).fetchone()[0]
    persisted = store.codec.decrypt_json(
        envelope,
        tenant_id=identity.tenant_id,
        resource_type="proposal-handoff-receipt",
        resource_id=str(completed.receipt_id),
        field="receipt",
    )
    assert persisted["actionKey"] == action_key
    assert persisted[resource_field] == str(receipt_fields[resource_field])


def _receipt(
    identity: PersonalDomainIdentity,
    *,
    handoff_id: UUID,
    proposal_id: UUID,
    handoff_version: int,
    request_id: UUID,
) -> ProposalHandoffObservation:
    return ProposalHandoffObservation(
        commandId=uuid4(),
        expectedVersion=handoff_version,
        state=ProposalHandoffState.COMPLETED,
        receipt={
            "domain": "APPROVAL",
            "operation": "REQUEST_SUBMIT",
            "handoffId": handoff_id,
            "proposalId": proposal_id,
            "actionKey": "APPROVAL.REQUEST.CREATE",
            "handoffVersion": handoff_version,
            "requestId": request_id,
            "requestVersion": 2,
            "status": "IN_REVIEW",
            "committedAt": datetime.now(UTC),
            "correlationId": identity.correlation_id,
        },
    )


def _seed(
    identity: PersonalDomainIdentity,
    *,
    state: str = "REVIEW_REQUIRED",
    revision: int = 1,
    action_key: str = "APPROVAL.REQUEST.CREATE",
) -> tuple[UUID, UUID]:
    proposal_id = uuid4()
    handoff_id = uuid4()
    codec = GovernedPayloadCodec()
    proposal_envelope = codec.encrypt_json(
        {"title": "Governed approval proposal"},
        tenant_id=identity.tenant_id,
        resource_type="proposal-handoff-receipt-test",
        resource_id=str(proposal_id),
        field="proposal",
    )
    reviewed_inputs = codec.encrypt_json(
        {"title": "Quarterly approval"},
        tenant_id=identity.tenant_id,
        resource_type="proposal-handoff-receipt-test",
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
                       'ACCEPTED', 1, 'PROPOSAL_TEST', %s, %s,
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
                action_key,
                proposal_envelope,
            ),
        )
        connection.execute(
            """INSERT INTO ai_proposal_handoffs (
                   handoff_id, proposal_id, tenant_id, user_id, command_id,
                   idempotency_key, action_key, target_route, handoff_state,
                   revision, approval_required, request_fingerprint,
                   reviewed_inputs_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, %s,
                       '/approvals/requests/new', %s, %s, TRUE, %s, %s)""",
            (
                handoff_id,
                proposal_id,
                identity.tenant_id,
                identity.user_id,
                uuid4(),
                uuid4(),
                action_key,
                state,
                revision,
                "b" * 64,
                reviewed_inputs,
            ),
        )
    return proposal_id, handoff_id


def _identity() -> PersonalDomainIdentity:
    return PersonalDomainIdentity(
        tenant_id=890_000_000 + uuid4().int % 100_000_000,
        user_id="member-1",
        correlation_id=f"handoff-{uuid4()}",
        auth_session_id="session-1",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset({"APP.ASK:VIEW"}),
    )
