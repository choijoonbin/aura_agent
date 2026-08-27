from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID, uuid4

from .contracts import (
    AnswerFeedbackRequest,
    ConversationDetail,
    ConversationMessage,
    ConversationRole,
    ConversationSummary,
    FeedbackReceipt,
    AskResponse,
)
from .conversation_exchange import build_exchange, exchange_matches
from .run_store import RunLease, RunStore, RunStoreUnavailable, get_run_store


class ConversationNotFound(RuntimeError):
    pass


class ConversationRetentionLocked(RuntimeError):
    pass


class ConversationStoreUnavailable(RunStoreUnavailable):
    pass


@dataclass(frozen=True)
class ConversationTurn:
    role: ConversationRole
    content: str


class ConversationStore(Protocol):
    def ensure(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID | None,
        locale: str,
        initial_query: str,
    ) -> UUID: ...

    def recent_history(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID, limit: int = 8
    ) -> tuple[ConversationTurn, ...]: ...

    def append_exchange(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID,
        request_id: str,
        query: str,
        response: AskResponse,
        lease: RunLease,
    ) -> tuple[UUID, UUID]: ...

    def list(self, *, tenant_id: str, user_id: str, limit: int = 30) -> list[ConversationSummary]: ...

    def get(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID
    ) -> ConversationDetail: ...

    def rename(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID, title: str
    ) -> ConversationDetail: ...

    def delete(self, *, tenant_id: str, user_id: str, conversation_id: UUID) -> None: ...

    def feedback(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: UUID,
        request: AnswerFeedbackRequest,
    ) -> FeedbackReceipt: ...


class InMemoryConversationStore:
    def __init__(self, run_store: RunStore | None = None) -> None:
        self._run_store = run_store or get_run_store()
        self._summaries: dict[tuple[str, str, UUID], ConversationSummary] = {}
        self._messages: dict[tuple[str, str, UUID], list[ConversationMessage]] = {}
        self._request_pairs: dict[tuple[str, str, UUID, str], tuple[UUID, UUID]] = {}
        self._request_leases: dict[tuple[str, str, UUID, str], RunLease] = {}
        self._message_claims: dict[UUID, tuple[RunLease, str]] = {}
        self._run_owners: dict[UUID, tuple[str, str]] = {}
        self._run_claims: dict[UUID, tuple[RunLease, str]] = {}
        self._feedback: dict[tuple[str, str, UUID], FeedbackReceipt] = {}
        self._lock = threading.RLock()

    def ensure(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID | None,
        locale: str,
        initial_query: str,
    ) -> UUID:
        with self._lock:
            if conversation_id is not None:
                self._summary(tenant_id, user_id, conversation_id)
                return conversation_id
            created_at = datetime.now(timezone.utc)
            created = uuid4()
            key = (tenant_id, user_id, created)
            self._summaries[key] = ConversationSummary(
                conversation_id=created,
                title=_title(initial_query),
                locale=locale,
                message_count=0,
                created_at=created_at,
                updated_at=created_at,
                last_message_at=created_at,
            )
            self._messages[key] = []
            return created

    def recent_history(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID, limit: int = 8
    ) -> tuple[ConversationTurn, ...]:
        with self._lock:
            self._summary(tenant_id, user_id, conversation_id)
            messages = self._visible_messages(tenant_id, user_id, conversation_id)[
                -max(0, limit) :
            ]
            return tuple(ConversationTurn(role=item.role, content=item.content) for item in messages)

    def append_exchange(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID,
        request_id: str,
        query: str,
        response: AskResponse,
        lease: RunLease,
    ) -> tuple[UUID, UUID]:
        if response.run_id != lease.run_id or response.request_id != request_id:
            raise ConversationStoreUnavailable(
                "The conversation response does not match the claimed run lease."
            )
        self._require_active_lease(lease, tenant_id, user_id, request_id)
        with self._lock:
            key = (tenant_id, user_id, conversation_id)
            self._summary(tenant_id, user_id, conversation_id)
            request_key = (*key, request_id)
            existing_pair = self._request_pairs.get(request_key)
            if existing_pair is not None:
                existing_ids = set(existing_pair)
                existing = [
                    message for message in self._messages[key]
                    if message.message_id in existing_ids
                ]
                if (
                    self._request_leases.get(request_key) == lease
                    and exchange_matches(existing, query=query, response=response)
                ):
                    return existing_pair
                self._messages[key] = [
                    message for message in self._messages[key]
                    if message.message_id not in existing_ids
                ]
                for message in existing:
                    if message.run_id is not None:
                        self._run_claims.pop(message.run_id, None)
                        self._run_owners.pop(message.run_id, None)
                for message_id in existing_ids:
                    self._message_claims.pop(message_id, None)

            user_message, assistant_message = build_exchange(query, response)
            self._messages[key].extend((user_message, assistant_message))
            pair = (user_message.message_id, assistant_message.message_id)
            self._request_pairs[request_key] = pair
            self._request_leases[request_key] = lease
            for message_id in pair:
                self._message_claims[message_id] = (lease, request_id)
            self._run_owners[UUID(response.run_id)] = (tenant_id, user_id)
            self._run_claims[UUID(response.run_id)] = (lease, request_id)
            try:
                self._require_active_lease(lease, tenant_id, user_id, request_id)
            except ConversationStoreUnavailable:
                self._messages[key] = [
                    message for message in self._messages[key]
                    if message.message_id not in pair
                ]
                self._request_pairs.pop(request_key, None)
                self._request_leases.pop(request_key, None)
                for message_id in pair:
                    self._message_claims.pop(message_id, None)
                self._run_claims.pop(UUID(response.run_id), None)
                self._run_owners.pop(UUID(response.run_id), None)
                raise
            return pair

    def list(self, *, tenant_id: str, user_id: str, limit: int = 30) -> list[ConversationSummary]:
        with self._lock:
            return sorted(
                [
                    self._visible_summary(key, summary)
                    for key, summary in self._summaries.items()
                    if key[0] == tenant_id and key[1] == user_id
                    and self._visible_messages(*key)
                ],
                key=lambda item: item.last_message_at,
                reverse=True,
            )[: max(1, min(limit, 100))]

    def get(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID
    ) -> ConversationDetail:
        with self._lock:
            summary = self._summary(tenant_id, user_id, conversation_id)
            messages = self._visible_messages(tenant_id, user_id, conversation_id)
            if not messages:
                raise ConversationNotFound(
                    "Conversation was not found in the verified user scope."
                )
            return ConversationDetail(
                summary=self._visible_summary(
                    (tenant_id, user_id, conversation_id), summary
                ),
                messages=messages,
            )

    def rename(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID, title: str
    ) -> ConversationDetail:
        with self._lock:
            key = (tenant_id, user_id, conversation_id)
            summary = self._summary(tenant_id, user_id, conversation_id)
            self._summaries[key] = summary.model_copy(
                update={"title": _title(title), "updated_at": datetime.now(timezone.utc)}
            )
            return self.get(
                tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
            )

    def delete(self, *, tenant_id: str, user_id: str, conversation_id: UUID) -> None:
        with self._lock:
            key = (tenant_id, user_id, conversation_id)
            self._summary(tenant_id, user_id, conversation_id)
            del self._summaries[key]
            for message in self._messages.pop(key, []):
                self._message_claims.pop(message.message_id, None)
                if message.run_id is not None:
                    self._run_claims.pop(message.run_id, None)
                    self._run_owners.pop(message.run_id, None)
            for request_key in [item for item in self._request_pairs if item[:3] == key]:
                del self._request_pairs[request_key]
                self._request_leases.pop(request_key, None)

    def feedback(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: UUID,
        request: AnswerFeedbackRequest,
    ) -> FeedbackReceipt:
        with self._lock:
            lease_and_request = self._run_claims.get(run_id)
            if (
                self._run_owners.get(run_id) != (tenant_id, user_id)
                or lease_and_request is None
                or not self._run_store.is_completed(
                    lease_and_request[0],
                    tenant_id=tenant_id,
                    user_id=user_id,
                    request_id=lease_and_request[1],
                )
            ):
                raise ConversationNotFound("The Agent run is not available to this user.")
            receipt = FeedbackReceipt(
                run_id=run_id, rating=request.rating, recorded_at=datetime.now(timezone.utc)
            )
            self._feedback[(tenant_id, user_id, run_id)] = receipt
            return receipt

    def _summary(self, tenant_id: str, user_id: str, conversation_id: UUID) -> ConversationSummary:
        summary = self._summaries.get((tenant_id, user_id, conversation_id))
        if summary is None:
            raise ConversationNotFound("Conversation was not found in the verified user scope.")
        return summary

    def _require_active_lease(
        self, lease: RunLease, tenant_id: str, user_id: str, request_id: str
    ) -> None:
        try:
            self._run_store.require_active(
                lease,
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
            )
        except RunStoreUnavailable as error:
            raise ConversationStoreUnavailable(str(error)) from error

    def _visible_messages(
        self, tenant_id: str, user_id: str, conversation_id: UUID
    ) -> list[ConversationMessage]:
        visible: list[ConversationMessage] = []
        for message in self._messages[(tenant_id, user_id, conversation_id)]:
            claim = self._message_claims.get(message.message_id)
            if claim is not None and self._run_store.is_completed(
                claim[0],
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=claim[1],
            ):
                visible.append(message)
        return visible

    def _visible_summary(
        self,
        key: tuple[str, str, UUID],
        summary: ConversationSummary,
    ) -> ConversationSummary:
        messages = self._visible_messages(*key)
        if not messages:
            return summary.model_copy(update={"message_count": 0})
        last_message_at = max(message.created_at for message in messages)
        return summary.model_copy(
            update={
                "message_count": len(messages),
                "updated_at": max(summary.updated_at, last_message_at),
                "last_message_at": last_message_at,
            }
        )

_STORE: ConversationStore | None = None
_MEMORY_STORES: dict[int, tuple[RunStore, InMemoryConversationStore]] = {}
_STORE_LOCK = threading.Lock()


def get_conversation_store(run_store: RunStore | None = None) -> ConversationStore:
    global _STORE
    with _STORE_LOCK:
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if database_url:
            if _STORE is not None:
                return _STORE
            from .postgres_conversation_store import PostgresConversationStore
            from .run_store import load_payload_encryption

            _STORE = PostgresConversationStore(
                database_url,
                load_payload_encryption(),
            )
            return _STORE
        resolved_run_store = run_store or get_run_store()
        store_key = id(resolved_run_store)
        cached = _MEMORY_STORES.get(store_key)
        if cached is not None and cached[0] is resolved_run_store:
            return cached[1]
        store = InMemoryConversationStore(resolved_run_store)
        _MEMORY_STORES[store_key] = (resolved_run_store, store)
        return store


def reset_conversation_store_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None
        _MEMORY_STORES.clear()


def _title(value: str) -> str:
    normalized = " ".join(value.split()).strip()
    return (normalized or "New DWAI·ON conversation")[:160]
