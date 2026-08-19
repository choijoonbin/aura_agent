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
from .run_store import RunStoreUnavailable


class ConversationNotFound(RuntimeError):
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
    def __init__(self) -> None:
        self._summaries: dict[tuple[str, str, UUID], ConversationSummary] = {}
        self._messages: dict[tuple[str, str, UUID], list[ConversationMessage]] = {}
        self._request_pairs: dict[tuple[str, str, UUID, str], tuple[UUID, UUID]] = {}
        self._run_owners: dict[UUID, tuple[str, str]] = {}
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
            messages = self._messages[(tenant_id, user_id, conversation_id)][-max(0, limit) :]
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
    ) -> tuple[UUID, UUID]:
        with self._lock:
            key = (tenant_id, user_id, conversation_id)
            summary = self._summary(tenant_id, user_id, conversation_id)
            request_key = (*key, request_id)
            if request_key in self._request_pairs:
                return self._request_pairs[request_key]
            now = datetime.now(timezone.utc)
            user_message = ConversationMessage(
                message_id=uuid4(), role=ConversationRole.USER, content=query, created_at=now
            )
            assistant_message = ConversationMessage(
                message_id=uuid4(),
                role=ConversationRole.ASSISTANT,
                content=response.answer or response.status_code,
                run_id=UUID(response.run_id),
                status_code=response.status_code,
                citations=response.citations,
                created_at=now,
            )
            self._messages[key].extend((user_message, assistant_message))
            self._summaries[key] = summary.model_copy(
                update={
                    "message_count": summary.message_count + 2,
                    "updated_at": now,
                    "last_message_at": now,
                }
            )
            pair = (user_message.message_id, assistant_message.message_id)
            self._request_pairs[request_key] = pair
            self._run_owners[UUID(response.run_id)] = (tenant_id, user_id)
            return pair

    def list(self, *, tenant_id: str, user_id: str, limit: int = 30) -> list[ConversationSummary]:
        with self._lock:
            return sorted(
                [
                    summary
                    for (tenant, user, _), summary in self._summaries.items()
                    if tenant == tenant_id and user == user_id
                ],
                key=lambda item: item.last_message_at,
                reverse=True,
            )[: max(1, min(limit, 100))]

    def get(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID
    ) -> ConversationDetail:
        with self._lock:
            summary = self._summary(tenant_id, user_id, conversation_id)
            return ConversationDetail(
                summary=summary,
                messages=list(self._messages[(tenant_id, user_id, conversation_id)]),
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
            self._messages.pop(key, None)
            for request_key in [item for item in self._request_pairs if item[:3] == key]:
                del self._request_pairs[request_key]

    def feedback(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: UUID,
        request: AnswerFeedbackRequest,
    ) -> FeedbackReceipt:
        with self._lock:
            if self._run_owners.get(run_id) != (tenant_id, user_id):
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


_STORE: ConversationStore | None = None
_STORE_LOCK = threading.Lock()


def get_conversation_store() -> ConversationStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if database_url:
            from .postgres_conversation_store import PostgresConversationStore

            data_key = os.getenv("DWP_AGENT_DATA_KEY", "").strip()
            if not data_key:
                raise RunStoreUnavailable("Agent conversation encryption key is required.")
            _STORE = PostgresConversationStore(database_url, data_key)
        else:
            _STORE = InMemoryConversationStore()
        return _STORE


def reset_conversation_store_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None


def _title(value: str) -> str:
    normalized = " ".join(value.split()).strip()
    return (normalized or "New DWAI·ON conversation")[:160]

