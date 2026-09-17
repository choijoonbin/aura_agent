from __future__ import annotations

import os
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .dwaion_workflow_contracts import (
    CreateProposalHandoffRequest,
    ProposalHandoff,
    ProposalHandoffObservation,
    ProposalHandoffState,
)
from .dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
)
from .governed_domain_core import GovernedFingerprints, GovernedPayloadCodec, advisory_lock
from .personal_domain_security import PersonalDomainIdentity
from .proposal_contracts import AgentProposal, ProposalInboxView, ProposalState
from .proposal_store import ProposalStoreUnavailable, get_proposal_store


class ProposalHandoffStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise DwaionWorkflowUnavailable("Proposal handoff encryption is unavailable.") from error

    def create(
        self,
        identity: PersonalDomainIdentity,
        proposal_id: UUID,
        request: CreateProposalHandoffRequest,
        *,
        action_key: str,
        target_route: str,
        approval_required: bool,
        reviewed_inputs: dict[str, object],
    ) -> ProposalHandoff:
        proof = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="proposal-handoff-create",
            payload={
                "proposalId": str(proposal_id),
                "request": request.model_dump(mode="json", by_alias=True),
                "actionKey": action_key,
                "targetRoute": target_route,
                "approvalRequired": approval_required,
                "reviewedInputs": reviewed_inputs,
            },
        )
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(connection, "proposal-handoff", identity.tenant_id, identity.user_id, proposal_id)
                existing = connection.execute(
                    _SELECT + " WHERE h.tenant_id = %s AND h.user_id = %s AND "
                    "(h.command_id = %s OR h.idempotency_key = %s OR h.proposal_id = %s)",
                    (
                        identity.tenant_id,
                        identity.user_id,
                        request.command_id,
                        request.idempotency_key,
                        proposal_id,
                    ),
                ).fetchone()
                if existing is not None:
                    if existing["request_fingerprint"] != proof:
                        raise DwaionWorkflowConflict("The proposal handoff key is already bound to another request.")
                    return self._record(existing)
                envelope = self.codec.encrypt_json(
                    reviewed_inputs,
                    tenant_id=identity.tenant_id,
                    resource_type="proposal-handoff",
                    resource_id=str(proposal_id),
                    field="reviewed-inputs",
                )
                handoff_id = uuid4()
                state = (
                    ProposalHandoffState.AWAITING_APPROVAL
                    if approval_required
                    else ProposalHandoffState.REVIEW_REQUIRED
                )
                row = connection.execute(
                    """INSERT INTO ai_proposal_handoffs (
                           handoff_id, proposal_id, tenant_id, user_id, command_id,
                           idempotency_key, action_key, target_route, handoff_state,
                           approval_required, request_fingerprint, reviewed_inputs_envelope)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *""",
                    (
                        handoff_id,
                        proposal_id,
                        identity.tenant_id,
                        identity.user_id,
                        request.command_id,
                        request.idempotency_key,
                        action_key,
                        target_route,
                        state.value,
                        approval_required,
                        proof,
                        envelope,
                    ),
                ).fetchone()
                self._event(
                    connection,
                    identity,
                    row,
                    request.command_id,
                    "CREATED",
                    None,
                    proof,
                )
                return self._record(row)
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Proposal handoff storage is unavailable.") from error

    def by_proposal(
        self, identity: PersonalDomainIdentity, proposal_id: UUID
    ) -> ProposalHandoff:
        return self._get(identity, "h.proposal_id = %s", proposal_id)

    def get(self, identity: PersonalDomainIdentity, handoff_id: UUID) -> ProposalHandoff:
        return self._get(identity, "h.handoff_id = %s", handoff_id)

    def observe(
        self,
        identity: PersonalDomainIdentity,
        handoff_id: UUID,
        request: ProposalHandoffObservation,
    ) -> ProposalHandoff:
        request_fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="proposal-handoff-observation",
            payload={
                "handoffId": str(handoff_id),
                "request": request.model_dump(mode="json", by_alias=True),
            },
        )
        allowed = {
            ProposalHandoffState.REVIEW_REQUIRED: {ProposalHandoffState.HANDED_OFF, ProposalHandoffState.CANCELLED},
            ProposalHandoffState.AWAITING_APPROVAL: {ProposalHandoffState.HANDED_OFF, ProposalHandoffState.CANCELLED},
            ProposalHandoffState.HANDED_OFF: {ProposalHandoffState.RUNNING, ProposalHandoffState.FAILED},
            ProposalHandoffState.RUNNING: {
                ProposalHandoffState.PARTIAL,
                ProposalHandoffState.COMPLETED,
                ProposalHandoffState.FAILED,
                ProposalHandoffState.COMPENSATING,
            },
            ProposalHandoffState.PARTIAL: {
                ProposalHandoffState.RUNNING,
                ProposalHandoffState.COMPENSATING,
                ProposalHandoffState.FAILED,
            },
            ProposalHandoffState.COMPENSATING: {
                ProposalHandoffState.COMPENSATED,
                ProposalHandoffState.FAILED,
            },
        }
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    _SELECT + " WHERE h.handoff_id = %s AND h.tenant_id = %s AND h.user_id = %s FOR UPDATE",
                    (handoff_id, identity.tenant_id, identity.user_id),
                ).fetchone()
                if row is None:
                    raise DwaionWorkflowNotFound("The proposal handoff is unavailable.")
                replay = connection.execute(
                    """SELECT handoff_id, current_state, revision, request_fingerprint
                         FROM ai_proposal_handoff_events
                        WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
                    (identity.tenant_id, identity.user_id, request.command_id),
                ).fetchone()
                if replay is not None:
                    if (
                        replay["handoff_id"] != handoff_id
                        or replay["request_fingerprint"] != request_fingerprint
                        or replay["current_state"] != request.state.value
                    ):
                        raise DwaionWorkflowConflict("The handoff command ID is already in use.")
                    if (
                        int(row["revision"]) != int(replay["revision"])
                        or row["handoff_state"] != replay["current_state"]
                    ):
                        raise DwaionWorkflowConflict(
                            "The handoff advanced after this command was applied."
                        )
                    return self._record(row)
                if int(row["revision"]) != request.expected_version:
                    raise DwaionWorkflowConflict("The proposal handoff version has changed.")
                current = ProposalHandoffState(row["handoff_state"])
                if request.state not in allowed.get(current, set()):
                    raise DwaionWorkflowConflict("The requested handoff state transition is not allowed.")
                if request.receipt is not None and (
                    request.receipt.handoff_id != handoff_id
                    or request.receipt.proposal_id != row["proposal_id"]
                    or request.receipt.action_key != row["action_key"]
                    or request.receipt.handoff_version != request.expected_version
                    or request.receipt.correlation_id != identity.correlation_id
                ):
                    raise DwaionWorkflowConflict(
                        "The domain completion receipt does not match the reviewed handoff."
                    )
                receipt_id = uuid4() if request.receipt is not None else None
                receipt_envelope = (
                    self.codec.encrypt_json(
                        request.receipt.model_dump(mode="json", by_alias=True),
                        tenant_id=identity.tenant_id,
                        resource_type="proposal-handoff-receipt",
                        resource_id=str(receipt_id),
                        field="receipt",
                    )
                    if receipt_id
                    else None
                )
                updated = connection.execute(
                    """UPDATE ai_proposal_handoffs
                          SET handoff_state = %s, revision = revision + 1,
                              receipt_id = %s, receipt_envelope = %s,
                              completed_at = CASE WHEN %s = 'COMPLETED' THEN CURRENT_TIMESTAMP ELSE completed_at END,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE handoff_id = %s
                    RETURNING *""",
                    (request.state.value, receipt_id, receipt_envelope, request.state.value, handoff_id),
                ).fetchone()
                self._event(
                    connection,
                    identity,
                    updated,
                    request.command_id,
                    "STATE_CHANGED",
                    current.value,
                    request_fingerprint,
                )
                return self._record(updated)
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Proposal handoff storage is unavailable.") from error

    def _get(self, identity: PersonalDomainIdentity, predicate: str, value: UUID) -> ProposalHandoff:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    _SELECT + f" WHERE {predicate} AND h.tenant_id = %s AND h.user_id = %s",
                    (value, identity.tenant_id, identity.user_id),
                ).fetchone()
                if row is None:
                    raise DwaionWorkflowNotFound("The proposal handoff is unavailable.")
                return self._record(row)
        except DwaionWorkflowNotFound:
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Proposal handoff storage is unavailable.") from error

    @staticmethod
    def _record(row: Any) -> ProposalHandoff:
        return ProposalHandoff(
            handoff_id=row["handoff_id"],
            proposal_id=row["proposal_id"],
            action_key=row["action_key"],
            state=row["handoff_state"],
            version=row["revision"],
            target_route=row["target_route"],
            approval_required=row["approval_required"],
            receipt_id=row["receipt_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _event(
        connection: Any,
        identity: PersonalDomainIdentity,
        row: Any,
        command_id: UUID,
        event_type: str,
        previous: str | None,
        request_fingerprint: str,
    ) -> None:
        connection.execute(
            """INSERT INTO ai_proposal_handoff_events (
                   event_id, handoff_id, tenant_id, user_id, actor_user_id,
                   correlation_id, command_id, event_type, previous_state,
                   current_state, revision, request_fingerprint)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(), row["handoff_id"], identity.tenant_id, identity.user_id,
                identity.user_id, identity.correlation_id, command_id, event_type,
                previous,
                row["handoff_state"],
                row["revision"],
                request_fingerprint,
            ),
        )


def find_user_proposal(identity: PersonalDomainIdentity, proposal_id: UUID) -> AgentProposal:
    try:
        store = get_proposal_store()
        cursor: str | None = None
        while True:
            page = store.list(
                tenant_id=str(identity.tenant_id),
                user_id=identity.user_id,
                view=ProposalInboxView.ALL,
                limit=100,
                cursor=cursor,
            )
            for proposal in page.items:
                if proposal.proposal_id == proposal_id:
                    return proposal
            cursor = page.next_cursor
            if cursor is None:
                raise DwaionWorkflowNotFound("The agent proposal is unavailable.")
    except ProposalStoreUnavailable as error:
        raise DwaionWorkflowUnavailable("Agent proposals are unavailable.") from error


def require_accepted_proposal(proposal: AgentProposal, expected_version: int) -> None:
    if proposal.revision != expected_version:
        raise DwaionWorkflowConflict("The agent proposal version has changed.")
    if proposal.state != ProposalState.ACCEPTED:
        raise DwaionWorkflowConflict("Only an accepted proposal can create a handoff.")
    if not proposal.action_key:
        raise DwaionWorkflowConflict("The accepted proposal has no registered action.")


@lru_cache(maxsize=1)
def get_proposal_handoff_store() -> ProposalHandoffStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise DwaionWorkflowUnavailable("Proposal handoff storage is unavailable.")
    return ProposalHandoffStore(database_url)


_SELECT = """SELECT h.* FROM ai_proposal_handoffs h"""
