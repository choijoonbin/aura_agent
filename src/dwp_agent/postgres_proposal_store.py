from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError
from psycopg import connect
from pydantic import ValidationError

from .envelope import EnvelopeEncryptionError, KeyContext
from .key_provider import KeyProviderConfigurationError
from .proposal_contracts import (
    AgentProposal,
    CreateAgentProposalRequest,
    DecideAgentProposalRequest,
    ProposalContent,
    ProposalDecision,
    ProposalInboxSummary,
    ProposalInboxView,
    ProposalKind,
    ProposalPriority,
    ProposalState,
)
from .proposal_store import (
    ProposalConflict,
    ProposalCursorInvalid,
    ProposalNotFound,
    ProposalPage,
    ProposalStoreUnavailable,
    _decision_state,
    _project,
    _utc,
    _validate_window,
    decode_proposal_cursor,
    encode_proposal_cursor,
)
from .proposal_fingerprints import ProposalRequestFingerprints
from .run_store import load_payload_encryption
from .run_store_errors import RunStoreUnavailable


class PostgresProposalStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            self.encryption = load_payload_encryption()
            self.fingerprints = ProposalRequestFingerprints.load()
        except (RunStoreUnavailable, KeyProviderConfigurationError, ValueError) as error:
            raise ProposalStoreUnavailable(
                "Agent proposal encryption is unavailable."
            ) from error

    def create(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        request: CreateAgentProposalRequest,
    ) -> AgentProposal:
        tenant = _tenant(tenant_id)
        request_fingerprint = self.fingerprints.create(tenant, request)
        try:
            with connect(self.database_url) as connection:
                _lock_idempotency_keys(
                    connection,
                    f"proposal-command:{tenant}:{actor_user_id}:{request.command_id}",
                    f"proposal-source:{tenant}:{request.target_user_id}:{request.source_event_id}",
                )
                now = connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
                existing = connection.execute(
                    _SELECT_PROPOSAL
                    + " WHERE tenant_id = %s AND target_user_id = %s AND source_event_id = %s",
                    (tenant, request.target_user_id, request.source_event_id),
                ).fetchone()
                if existing is not None:
                    if not self.fingerprints.matches_create(
                        str(existing[_REQUEST_FINGERPRINT]), tenant, request
                    ):
                        raise ProposalConflict(
                            "The source event already produced another proposal."
                        )
                    return self._proposal(existing, now)
                command = connection.execute(
                    """SELECT source_event_id, request_fingerprint
                         FROM ai_agent_proposals
                        WHERE tenant_id = %s AND created_by_user_id = %s
                          AND creation_command_id = %s""",
                    (tenant, actor_user_id, request.command_id),
                ).fetchone()
                if command is not None:
                    raise ProposalConflict("The proposal command ID is already in use.")
                available_at = _utc(request.available_at or now)
                expires_at = _utc(request.expires_at)
                _validate_window(available_at, expires_at, now)
                proposal_id = uuid4()
                payload_envelope = self._encrypt_content(
                    tenant, proposal_id, request.content
                )
                connection.execute(
                    """INSERT INTO ai_agent_proposals (
                           proposal_id, tenant_id, target_user_id, source_event_id,
                           creation_command_id, created_by_user_id, request_fingerprint,
                           kind, priority, state, revision, agent_key, action_key,
                           payload_envelope, proposed_at, available_at, expires_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING', 1,
                               %s, %s, %s, %s, %s, %s)""",
                    (
                        proposal_id,
                        tenant,
                        request.target_user_id,
                        request.source_event_id,
                        request.command_id,
                        actor_user_id,
                        request_fingerprint,
                        request.kind.value,
                        request.priority.value,
                        request.agent_key,
                        request.action_key,
                        payload_envelope,
                        now,
                        available_at,
                        expires_at,
                    ),
                )
                self._insert_event(
                    connection,
                    proposal_id=proposal_id,
                    tenant_id=tenant,
                    target_user_id=request.target_user_id,
                    actor_user_id=actor_user_id,
                    correlation_id=correlation_id,
                    command_id=request.command_id,
                    event_type="CREATED",
                    previous_state=None,
                    current_state=ProposalState.PENDING,
                    revision=1,
                    note=request.change_reason,
                    request_fingerprint=request_fingerprint,
                )
                row = connection.execute(
                    _SELECT_PROPOSAL + " WHERE proposal_id = %s",
                    (proposal_id,),
                ).fetchone()
                return self._proposal(row, now)
        except (ProposalConflict, ProposalStoreUnavailable):
            raise
        except (PsycopgError, EnvelopeEncryptionError, ValidationError, ValueError) as error:
            raise ProposalStoreUnavailable("Agent proposal storage is unavailable.") from error

    def list(
        self,
        *,
        tenant_id: str,
        user_id: str,
        view: ProposalInboxView,
        limit: int,
        cursor: str | None,
    ) -> ProposalPage:
        tenant = _tenant(tenant_id)
        cursor_value = decode_proposal_cursor(cursor) if cursor else None
        try:
            with connect(self.database_url) as connection:
                now = connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
                summary_row = connection.execute(
                    _SUMMARY_QUERY, (now, now, tenant, user_id, now)
                ).fetchone()
                conditions = [
                    "tenant_id = %s",
                    "target_user_id = %s",
                    "available_at <= %s",
                    _view_condition(view),
                ]
                parameters: list[object] = [now, now, tenant, user_id, now]
                if view != ProposalInboxView.ALL:
                    parameters.extend([now, now])
                if cursor_value:
                    conditions.append(
                        "(proposed_at, proposal_id) < (%s, %s)"
                    )
                    parameters.extend([cursor_value[0], UUID(cursor_value[1])])
                parameters.append(limit + 1)
                rows = connection.execute(
                    _SELECT_PROJECTED
                    + " WHERE "
                    + " AND ".join(conditions)
                    + " ORDER BY proposed_at DESC, proposal_id DESC LIMIT %s",
                    tuple(parameters),
                ).fetchall()
                items = [self._proposal(row, now) for row in rows]
                next_cursor = (
                    encode_proposal_cursor(items[limit - 1])
                    if len(items) > limit
                    else None
                )
                return ProposalPage(
                    items=items[:limit],
                    summary=ProposalInboxSummary(
                        active=int(summary_row[0]),
                        high_priority=int(summary_row[1]),
                        snoozed=int(summary_row[2]),
                        handled=int(summary_row[3]),
                    ),
                    next_cursor=next_cursor,
                )
        except ProposalCursorInvalid:
            raise
        except (PsycopgError, EnvelopeEncryptionError, ValidationError, ValueError) as error:
            raise ProposalStoreUnavailable("Agent proposal storage is unavailable.") from error

    def decide(
        self,
        *,
        tenant_id: str,
        user_id: str,
        correlation_id: str,
        proposal_id: UUID,
        request: DecideAgentProposalRequest,
    ) -> AgentProposal:
        tenant = _tenant(tenant_id)
        request_fingerprint = self.fingerprints.decision(
            tenant, proposal_id, request
        )
        try:
            with connect(self.database_url) as connection:
                _lock_idempotency_keys(
                    connection,
                    f"proposal-decision:{tenant}:{user_id}:{request.command_id}",
                )
                now = connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
                event = connection.execute(
                    """SELECT proposal_id, event_type, request_fingerprint
                         FROM ai_agent_proposal_events
                        WHERE tenant_id = %s AND actor_user_id = %s AND command_id = %s""",
                    (tenant, user_id, request.command_id),
                ).fetchone()
                if event is not None:
                    if (
                        event[0] != proposal_id
                        or event[1] != request.decision.value
                        or not self.fingerprints.matches_decision(
                            event[2], tenant, proposal_id, request
                        )
                    ):
                        raise ProposalConflict(
                            "The proposal decision command ID is already in use."
                        )
                    row = connection.execute(
                        _SELECT_PROPOSAL
                        + " WHERE proposal_id = %s AND tenant_id = %s AND target_user_id = %s",
                        (proposal_id, tenant, user_id),
                    ).fetchone()
                    if row is None:
                        raise ProposalNotFound("The agent proposal is unavailable.")
                    return self._proposal(row, now)
                row = connection.execute(
                    _SELECT_PROPOSAL
                    + " WHERE proposal_id = %s AND tenant_id = %s AND target_user_id = %s FOR UPDATE",
                    (proposal_id, tenant, user_id),
                ).fetchone()
                if row is None:
                    raise ProposalNotFound("The agent proposal is unavailable.")
                current = self._proposal(row, now)
                if current.state == ProposalState.EXPIRED:
                    raise ProposalConflict("The agent proposal has expired.")
                if current.state not in {ProposalState.PENDING, ProposalState.SNOOZED}:
                    raise ProposalConflict("The agent proposal was already handled.")
                if current.revision != request.expected_revision:
                    raise ProposalConflict("The agent proposal revision has changed.")
                next_state, snoozed_until, decided_at = _decision_state(
                    request, now=now, expires_at=current.expires_at
                )
                updated = connection.execute(
                    """UPDATE ai_agent_proposals
                          SET state = %s, revision = revision + 1,
                              snoozed_until = %s, decided_at = %s, updated_at = %s
                        WHERE proposal_id = %s AND tenant_id = %s
                          AND target_user_id = %s AND revision = %s
                    RETURNING revision""",
                    (
                        next_state.value,
                        snoozed_until,
                        decided_at,
                        now,
                        proposal_id,
                        tenant,
                        user_id,
                        request.expected_revision,
                    ),
                ).fetchone()
                if updated is None:
                    raise ProposalConflict("The agent proposal revision has changed.")
                self._insert_event(
                    connection,
                    proposal_id=proposal_id,
                    tenant_id=tenant,
                    target_user_id=user_id,
                    actor_user_id=user_id,
                    correlation_id=correlation_id,
                    command_id=request.command_id,
                    event_type=request.decision.value,
                    previous_state=current.state,
                    current_state=next_state,
                    revision=int(updated[0]),
                    note=request.note,
                    request_fingerprint=request_fingerprint,
                )
                result = connection.execute(
                    _SELECT_PROPOSAL + " WHERE proposal_id = %s", (proposal_id,)
                ).fetchone()
                return self._proposal(result, now)
        except (ProposalConflict, ProposalNotFound, ProposalStoreUnavailable):
            raise
        except (PsycopgError, EnvelopeEncryptionError, ValidationError, ValueError) as error:
            raise ProposalStoreUnavailable("Agent proposal storage is unavailable.") from error

    def _proposal(self, row: Sequence[Any], now: datetime) -> AgentProposal:
        plaintext = self.encryption.decrypt_bytes(
            envelope=str(row[_PAYLOAD]),
            context=_proposal_context(row[_TENANT], row[_ID]),
            legacy_version=None,
            legacy_nonce=None,
            legacy_ciphertext=None,
            legacy_aad=b"",
        )
        content = ProposalContent.model_validate_json(plaintext)
        proposal = AgentProposal(
            proposal_id=row[_ID],
            kind=ProposalKind(row[_KIND]),
            priority=ProposalPriority(row[_PRIORITY]),
            state=ProposalState(row[_STATE]),
            revision=int(row[_REVISION]),
            agent_key=str(row[_AGENT]),
            action_key=str(row[_ACTION]) if row[_ACTION] else None,
            content=content,
            proposed_at=row[_PROPOSED],
            available_at=row[_AVAILABLE],
            expires_at=row[_EXPIRES],
            snoozed_until=row[_SNOOZED],
            decided_at=row[_DECIDED],
        )
        return _project(proposal, now)

    def _encrypt_content(
        self, tenant_id: int, proposal_id: UUID, content: ProposalContent
    ) -> str:
        return self.encryption.encrypt_bytes(
            content.model_dump_json(by_alias=True).encode("utf-8"),
            _proposal_context(tenant_id, proposal_id),
        )

    def _insert_event(
        self,
        connection: Any,
        *,
        proposal_id: UUID,
        tenant_id: int,
        target_user_id: str,
        actor_user_id: str,
        correlation_id: str,
        command_id: UUID,
        event_type: str,
        previous_state: ProposalState | None,
        current_state: ProposalState,
        revision: int,
        note: str | None,
        request_fingerprint: str,
    ) -> None:
        event_id = uuid4()
        note_envelope = (
            self.encryption.encrypt_bytes(
                note.encode("utf-8"), _event_context(tenant_id, event_id)
            )
            if note
            else None
        )
        connection.execute(
            """INSERT INTO ai_agent_proposal_events (
                   event_id, proposal_id, tenant_id, target_user_id, actor_user_id,
                   correlation_id, command_id, event_type, previous_state,
                   current_state, revision, note_envelope, request_fingerprint)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                event_id,
                proposal_id,
                tenant_id,
                target_user_id,
                actor_user_id,
                correlation_id,
                command_id,
                event_type,
                previous_state.value if previous_state else None,
                current_state.value,
                revision,
                note_envelope,
                request_fingerprint,
            ),
        )


def _tenant(value: str) -> int:
    try:
        tenant = int(value)
    except ValueError as error:
        raise ProposalStoreUnavailable("Agent proposal storage is unavailable.") from error
    if tenant <= 0:
        raise ProposalStoreUnavailable("Agent proposal storage is unavailable.")
    return tenant


def _proposal_context(tenant_id: str | int, proposal_id: UUID) -> KeyContext:
    return KeyContext.payload(
        tenant_id=tenant_id,
        resource_type="agent-proposal",
        resource_id=str(proposal_id),
        field="content",
    )


def _event_context(tenant_id: str | int, event_id: UUID) -> KeyContext:
    return KeyContext.payload(
        tenant_id=tenant_id,
        resource_type="agent-proposal-event",
        resource_id=str(event_id),
        field="note",
    )


def _lock_idempotency_keys(connection: Any, *keys: str) -> None:
    for key in sorted(keys):
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"dwp-agent:{key}",),
        )


def _view_condition(view: ProposalInboxView) -> str:
    effective = _EFFECTIVE_STATE
    if view == ProposalInboxView.ACTIVE:
        return f"{effective} = 'PENDING'"
    if view == ProposalInboxView.SNOOZED:
        return f"{effective} = 'SNOOZED'"
    if view == ProposalInboxView.HANDLED:
        return f"{effective} IN ('ACCEPTED', 'DISMISSED', 'EXPIRED')"
    return "TRUE"


_ID = 0
_TENANT = 1
_KIND = 2
_PRIORITY = 3
_STATE = 4
_REVISION = 5
_AGENT = 6
_ACTION = 7
_PAYLOAD = 8
_REQUEST_FINGERPRINT = 9
_PROPOSED = 10
_AVAILABLE = 11
_EXPIRES = 12
_SNOOZED = 13
_DECIDED = 14

_EFFECTIVE_STATE = """CASE
    WHEN expires_at <= %s THEN 'EXPIRED'
    WHEN state = 'SNOOZED' AND snoozed_until <= %s THEN 'PENDING'
    ELSE state END"""

_SELECT_PROPOSAL = """SELECT proposal_id, tenant_id, kind, priority, state, revision,
       agent_key, action_key, payload_envelope, request_fingerprint, proposed_at,
       available_at, expires_at, snoozed_until, decided_at
  FROM ai_agent_proposals"""

_SELECT_PROJECTED = """SELECT proposal_id, tenant_id, kind, priority,
       """ + _EFFECTIVE_STATE + """ AS state, revision, agent_key, action_key,
       payload_envelope, request_fingerprint, proposed_at, available_at, expires_at,
       snoozed_until, decided_at
  FROM ai_agent_proposals"""

_SUMMARY_QUERY = """WITH projected AS (
    SELECT priority, """ + _EFFECTIVE_STATE + """ AS effective_state
      FROM ai_agent_proposals
     WHERE tenant_id = %s AND target_user_id = %s AND available_at <= %s
)
SELECT
    COUNT(*) FILTER (WHERE effective_state = 'PENDING'),
    COUNT(*) FILTER (WHERE effective_state = 'PENDING' AND priority IN ('HIGH', 'URGENT')),
    COUNT(*) FILTER (WHERE effective_state = 'SNOOZED'),
    COUNT(*) FILTER (WHERE effective_state IN ('ACCEPTED', 'DISMISSED', 'EXPIRED'))
FROM projected"""
