from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from .contracts import AskResponse, ConversationMessage, ConversationRole


def build_exchange(
    query: str,
    response: AskResponse,
) -> tuple[ConversationMessage, ConversationMessage]:
    created_at = datetime.now(timezone.utc)
    return (
        ConversationMessage(
            message_id=uuid4(),
            role=ConversationRole.USER,
            content=query,
            created_at=created_at,
        ),
        ConversationMessage(
            message_id=uuid4(),
            role=ConversationRole.ASSISTANT,
            content=response.answer or response.status_code,
            run_id=UUID(response.run_id),
            status_code=response.status_code,
            citations=response.citations,
            created_at=created_at + timedelta(microseconds=1),
        ),
    )


def exchange_matches(
    messages: list[ConversationMessage],
    *,
    query: str,
    response: AskResponse,
) -> bool:
    if len(messages) != 2:
        return False
    by_role = {message.role: message for message in messages}
    user = by_role.get(ConversationRole.USER)
    assistant = by_role.get(ConversationRole.ASSISTANT)
    return bool(
        user
        and assistant
        and user.content == query
        and assistant.content == (response.answer or response.status_code)
        and assistant.run_id == UUID(response.run_id)
        and assistant.status_code == response.status_code
        and assistant.citations == response.citations
    )
