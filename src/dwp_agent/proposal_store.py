from __future__ import annotations

import base64
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID, uuid4

from .proposal_contracts import (
    AgentProposal,
    CreateAgentProposalRequest,
    DecideAgentProposalRequest,
    ProposalDecision,
    ProposalInboxSummary,
    ProposalInboxView,
    ProposalState,
)
from .proposal_fingerprints import ProposalRequestFingerprints


class ProposalStoreUnavailable(RuntimeError):
    pass


class ProposalConflict(RuntimeError):
    pass


class ProposalNotFound(RuntimeError):
    pass


class ProposalCursorInvalid(RuntimeError):
    pass


@dataclass(frozen=True)
class ProposalPage:
    items: list[AgentProposal]
    summary: ProposalInboxSummary
    next_cursor: str | None


class ProposalStore(Protocol):
    def create(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        request: CreateAgentProposalRequest,
    ) -> AgentProposal: ...

    def list(
        self,
        *,
        tenant_id: str,
        user_id: str,
        view: ProposalInboxView,
        limit: int,
        cursor: str | None,
    ) -> ProposalPage: ...

    def decide(
        self,
        *,
        tenant_id: str,
        user_id: str,
        correlation_id: str,
        proposal_id: UUID,
        request: DecideAgentProposalRequest,
    ) -> AgentProposal: ...


@dataclass
class _MemoryRecord:
    tenant_id: str
    target_user_id: str
    source_event_id: str
    creation_command_id: UUID
    request_fingerprint: str
    proposal: AgentProposal


class InMemoryProposalStore:
    def __init__(
        self, fingerprints: ProposalRequestFingerprints | None = None
    ) -> None:
        self._lock = threading.Lock()
        self._records: dict[UUID, _MemoryRecord] = {}
        self._decisions: dict[tuple[str, str, UUID], tuple[UUID, str]] = {}
        self._fingerprints = fingerprints or ProposalRequestFingerprints.ephemeral()
        self.events: list[dict[str, object]] = []

    def create(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        correlation_id: str,
        request: CreateAgentProposalRequest,
    ) -> AgentProposal:
        now = datetime.now(timezone.utc)
        available_at = _utc(request.available_at or now)
        expires_at = _utc(request.expires_at)
        _validate_window(available_at, expires_at, now)
        request_fingerprint = self._fingerprints.create(tenant_id, request)
        with self._lock:
            for record in self._records.values():
                if record.tenant_id != tenant_id:
                    continue
                if record.creation_command_id == request.command_id:
                    if record.source_event_id != request.source_event_id:
                        raise ProposalConflict("The proposal command ID is already in use.")
                    if not self._fingerprints.matches_create(
                        record.request_fingerprint, tenant_id, request
                    ):
                        raise ProposalConflict("The proposal command payload has changed.")
                    return _project(record.proposal, now)
                if (
                    record.target_user_id == request.target_user_id
                    and record.source_event_id == request.source_event_id
                ):
                    if not self._fingerprints.matches_create(
                        record.request_fingerprint, tenant_id, request
                    ):
                        raise ProposalConflict("The source event already produced another proposal.")
                    return _project(record.proposal, now)
            proposal = AgentProposal(
                proposal_id=uuid4(),
                kind=request.kind,
                priority=request.priority,
                state=ProposalState.PENDING,
                revision=1,
                agent_key=request.agent_key,
                action_key=request.action_key,
                content=request.content,
                proposed_at=now,
                available_at=available_at,
                expires_at=expires_at,
            )
            self._records[proposal.proposal_id] = _MemoryRecord(
                tenant_id=tenant_id,
                target_user_id=request.target_user_id,
                source_event_id=request.source_event_id,
                creation_command_id=request.command_id,
                request_fingerprint=request_fingerprint,
                proposal=proposal,
            )
            self.events.append(
                _event(
                    proposal,
                    tenant_id=tenant_id,
                    target_user_id=request.target_user_id,
                    actor_user_id=actor_user_id,
                    correlation_id=correlation_id,
                    command_id=request.command_id,
                    event_type="CREATED",
                    previous_state=None,
                    request_fingerprint=request_fingerprint,
                )
            )
            return proposal

    def list(
        self,
        *,
        tenant_id: str,
        user_id: str,
        view: ProposalInboxView,
        limit: int,
        cursor: str | None,
    ) -> ProposalPage:
        now = datetime.now(timezone.utc)
        cursor_value = decode_proposal_cursor(cursor) if cursor else None
        with self._lock:
            projected = [
                _project(record.proposal, now)
                for record in self._records.values()
                if record.tenant_id == tenant_id
                and record.target_user_id == user_id
                and record.proposal.available_at <= now
            ]
        summary = summarize_proposals(projected)
        candidates = sorted(
            (proposal for proposal in projected if _matches_view(proposal.state, view)),
            key=lambda proposal: (proposal.proposed_at, str(proposal.proposal_id)),
            reverse=True,
        )
        if cursor_value:
            candidates = [
                proposal
                for proposal in candidates
                if (proposal.proposed_at, str(proposal.proposal_id)) < cursor_value
            ]
        page = candidates[: limit + 1]
        next_cursor = encode_proposal_cursor(page[limit - 1]) if len(page) > limit else None
        return ProposalPage(items=page[:limit], summary=summary, next_cursor=next_cursor)

    def decide(
        self,
        *,
        tenant_id: str,
        user_id: str,
        correlation_id: str,
        proposal_id: UUID,
        request: DecideAgentProposalRequest,
    ) -> AgentProposal:
        now = datetime.now(timezone.utc)
        decision_key = (tenant_id, user_id, request.command_id)
        request_fingerprint = self._fingerprints.decision(
            tenant_id, proposal_id, request
        )
        with self._lock:
            previous_decision = self._decisions.get(decision_key)
            if previous_decision:
                if previous_decision != (proposal_id, request_fingerprint):
                    raise ProposalConflict("The proposal decision command ID is already in use.")
                record = self._records.get(proposal_id)
                if record is None:
                    raise ProposalNotFound("The agent proposal is unavailable.")
                return _project(record.proposal, now)
            record = self._records.get(proposal_id)
            if (
                record is None
                or record.tenant_id != tenant_id
                or record.target_user_id != user_id
            ):
                raise ProposalNotFound("The agent proposal is unavailable.")
            current = _project(record.proposal, now)
            if current.state == ProposalState.EXPIRED:
                raise ProposalConflict("The agent proposal has expired.")
            if current.state not in {ProposalState.PENDING, ProposalState.SNOOZED}:
                raise ProposalConflict("The agent proposal was already handled.")
            if record.proposal.revision != request.expected_revision:
                raise ProposalConflict("The agent proposal revision has changed.")
            next_state, snoozed_until, decided_at = _decision_state(
                request, now=now, expires_at=record.proposal.expires_at
            )
            previous_state = current.state
            updated = record.proposal.model_copy(
                update={
                    "state": next_state,
                    "revision": record.proposal.revision + 1,
                    "snoozed_until": snoozed_until,
                    "decided_at": decided_at,
                }
            )
            record.proposal = updated
            self._decisions[decision_key] = (proposal_id, request_fingerprint)
            self.events.append(
                _event(
                    updated,
                    tenant_id=tenant_id,
                    target_user_id=user_id,
                    actor_user_id=user_id,
                    correlation_id=correlation_id,
                    command_id=request.command_id,
                    event_type=request.decision.value,
                    previous_state=previous_state,
                    request_fingerprint=request_fingerprint,
                )
            )
            return updated


def summarize_proposals(proposals: list[AgentProposal]) -> ProposalInboxSummary:
    return ProposalInboxSummary(
        active=sum(proposal.state == ProposalState.PENDING for proposal in proposals),
        high_priority=sum(
            proposal.state == ProposalState.PENDING
            and proposal.priority.value in {"HIGH", "URGENT"}
            for proposal in proposals
        ),
        snoozed=sum(proposal.state == ProposalState.SNOOZED for proposal in proposals),
        handled=sum(
            proposal.state
            in {ProposalState.ACCEPTED, ProposalState.DISMISSED, ProposalState.EXPIRED}
            for proposal in proposals
        ),
    )


def encode_proposal_cursor(proposal: AgentProposal) -> str:
    raw = json.dumps(
        [proposal.proposed_at.isoformat(), str(proposal.proposal_id)],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_proposal_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
        proposed_at = _utc(datetime.fromisoformat(value[0]))
        proposal_id = str(UUID(value[1]))
    except (ValueError, TypeError, IndexError, json.JSONDecodeError) as error:
        raise ProposalCursorInvalid("The proposal cursor is invalid.") from error
    return proposed_at, proposal_id


def _project(proposal: AgentProposal, now: datetime) -> AgentProposal:
    if proposal.expires_at <= now:
        return proposal.model_copy(update={"state": ProposalState.EXPIRED})
    if (
        proposal.state == ProposalState.SNOOZED
        and proposal.snoozed_until is not None
        and proposal.snoozed_until <= now
    ):
        return proposal.model_copy(update={"state": ProposalState.PENDING})
    return proposal


def _matches_view(state: ProposalState, view: ProposalInboxView) -> bool:
    if view == ProposalInboxView.ALL:
        return True
    if view == ProposalInboxView.ACTIVE:
        return state == ProposalState.PENDING
    if view == ProposalInboxView.SNOOZED:
        return state == ProposalState.SNOOZED
    return state in {ProposalState.ACCEPTED, ProposalState.DISMISSED, ProposalState.EXPIRED}


def _decision_state(
    request: DecideAgentProposalRequest, *, now: datetime, expires_at: datetime
) -> tuple[ProposalState, datetime | None, datetime | None]:
    if request.decision == ProposalDecision.SNOOZE:
        snooze_until = _utc(request.snooze_until)
        if snooze_until <= now or snooze_until >= expires_at:
            raise ProposalConflict("The snooze time must be before the proposal expires.")
        return ProposalState.SNOOZED, snooze_until, None
    if request.decision == ProposalDecision.ACCEPT:
        return ProposalState.ACCEPTED, None, now
    return ProposalState.DISMISSED, None, now


def _validate_window(available_at: datetime, expires_at: datetime, now: datetime) -> None:
    if expires_at <= available_at or expires_at <= now:
        raise ProposalConflict("The proposal expiry must follow its availability window.")
    if expires_at > now + timedelta(days=90):
        raise ProposalConflict("Agent proposals can remain active for at most 90 days.")


def _utc(value: datetime | None) -> datetime:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise ProposalConflict("Proposal timestamps must include a time zone.")
    return value.astimezone(timezone.utc)


def _event(
    proposal: AgentProposal,
    *,
    tenant_id: str,
    target_user_id: str,
    actor_user_id: str,
    correlation_id: str,
    command_id: UUID,
    event_type: str,
    previous_state: ProposalState | None,
    request_fingerprint: str,
) -> dict[str, object]:
    return {
        "proposalId": proposal.proposal_id,
        "tenantId": tenant_id,
        "targetUserId": target_user_id,
        "actorUserId": actor_user_id,
        "correlationId": correlation_id,
        "commandId": command_id,
        "eventType": event_type,
        "previousState": previous_state,
        "currentState": proposal.state,
        "revision": proposal.revision,
        "requestFingerprint": request_fingerprint,
    }


_STORE: ProposalStore | None = None
_STORE_LOCK = threading.Lock()


def get_proposal_store() -> ProposalStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            raise ProposalStoreUnavailable(
                "Agent proposals require the configured Agent database."
            )
        from .postgres_proposal_store import PostgresProposalStore

        _STORE = PostgresProposalStore(database_url)
        return _STORE


def set_proposal_store_for_tests(store: ProposalStore | None) -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = store
