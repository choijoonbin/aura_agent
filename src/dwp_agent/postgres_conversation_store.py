from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from psycopg import connect

from .contracts import (
    AnswerFeedbackRequest,
    AskResponse,
    ConversationDetail,
    ConversationMessage,
    ConversationRole,
    ConversationSummary,
    FeedbackReceipt,
)
from .conversation_store import ConversationNotFound, ConversationTurn, _title
from .run_store import PayloadCipher


class PostgresConversationStore:
    def __init__(self, database_url: str, data_key: str) -> None:
        self.database_url = database_url
        self.cipher = PayloadCipher(data_key)

    def ensure(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: UUID | None,
        locale: str,
        initial_query: str,
    ) -> UUID:
        if conversation_id is not None:
            with connect(self.database_url) as connection:
                row = connection.execute(
                    """SELECT conversation_id FROM ai_conversations
                         WHERE conversation_id = %s AND tenant_id = %s AND user_id = %s
                           AND retention_until > CURRENT_TIMESTAMP""",
                    (conversation_id, int(tenant_id), user_id),
                ).fetchone()
            if row is None:
                raise ConversationNotFound("Conversation was not found in the verified user scope.")
            return conversation_id

        created = uuid4()
        nonce, ciphertext = self._encrypt_text(
            _title(initial_query), _conversation_aad(tenant_id, user_id, created, "title")
        )
        with connect(self.database_url) as connection:
            connection.execute(
                """INSERT INTO ai_conversations (
                       conversation_id, tenant_id, user_id, locale,
                       title_nonce, title_ciphertext, retention_until)
                   VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP + INTERVAL '90 days')""",
                (created, int(tenant_id), user_id, locale, nonce, ciphertext),
            )
        return created

    def recent_history(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID, limit: int = 8
    ) -> tuple[ConversationTurn, ...]:
        rows = self._message_rows(tenant_id, user_id, conversation_id, limit=limit)
        return tuple(
            ConversationTurn(role=ConversationRole(row[1]), content=self._message(row).content)
            for row in rows
        )

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
        with connect(self.database_url) as connection:
            owner = connection.execute(
                """SELECT conversation_id FROM ai_conversations
                     WHERE conversation_id = %s AND tenant_id = %s AND user_id = %s
                       AND retention_until > CURRENT_TIMESTAMP
                     FOR UPDATE""",
                (conversation_id, int(tenant_id), user_id),
            ).fetchone()
            if owner is None:
                raise ConversationNotFound("Conversation was not found in the verified user scope.")
            existing = connection.execute(
                """SELECT message_id, role FROM ai_conversation_messages
                     WHERE conversation_id = %s AND request_id = %s""",
                (conversation_id, request_id),
            ).fetchall()
            if len(existing) == 2:
                by_role = {str(row[1]): UUID(str(row[0])) for row in existing}
                return by_role[ConversationRole.USER], by_role[ConversationRole.ASSISTANT]

            user_message_id = uuid4()
            assistant_message_id = uuid4()
            user_message = ConversationMessage(
                message_id=user_message_id,
                role=ConversationRole.USER,
                content=query,
                created_at=datetime.now(timezone.utc),
            )
            assistant_message = ConversationMessage(
                message_id=assistant_message_id,
                role=ConversationRole.ASSISTANT,
                content=response.answer or response.status_code,
                run_id=UUID(response.run_id),
                status_code=response.status_code,
                citations=response.citations,
                created_at=datetime.now(timezone.utc),
            )
            for message in (user_message, assistant_message):
                nonce, ciphertext = self._encrypt_message(message)
                connection.execute(
                    """INSERT INTO ai_conversation_messages (
                           message_id, conversation_id, request_id, run_id, role,
                           payload_nonce, payload_ciphertext, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        message.message_id,
                        conversation_id,
                        request_id,
                        message.run_id,
                        message.role,
                        nonce,
                        ciphertext,
                        message.created_at,
                    ),
                )
            connection.execute(
                """UPDATE ai_conversations
                      SET message_count = message_count + 2,
                          updated_at = CURRENT_TIMESTAMP,
                          last_message_at = CURRENT_TIMESTAMP
                    WHERE conversation_id = %s""",
                (conversation_id,),
            )
            return user_message_id, assistant_message_id

    def list(self, *, tenant_id: str, user_id: str, limit: int = 30) -> list[ConversationSummary]:
        with connect(self.database_url) as connection:
            connection.execute(
                "DELETE FROM ai_conversations WHERE retention_until <= CURRENT_TIMESTAMP"
            )
            rows = connection.execute(
                """SELECT conversation_id, locale, message_count, created_at, updated_at,
                          last_message_at, title_nonce, title_ciphertext
                     FROM ai_conversations
                    WHERE tenant_id = %s AND user_id = %s
                      AND retention_until > CURRENT_TIMESTAMP
                    ORDER BY last_message_at DESC
                    LIMIT %s""",
                (int(tenant_id), user_id, max(1, min(limit, 100))),
            ).fetchall()
        return [self._summary_from_row(tenant_id, user_id, row) for row in rows]

    def get(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID
    ) -> ConversationDetail:
        with connect(self.database_url) as connection:
            row = connection.execute(
                """SELECT conversation_id, locale, message_count, created_at, updated_at,
                          last_message_at, title_nonce, title_ciphertext
                     FROM ai_conversations
                    WHERE conversation_id = %s AND tenant_id = %s AND user_id = %s
                      AND retention_until > CURRENT_TIMESTAMP""",
                (conversation_id, int(tenant_id), user_id),
            ).fetchone()
        if row is None:
            raise ConversationNotFound("Conversation was not found in the verified user scope.")
        return ConversationDetail(
            summary=self._summary_from_row(tenant_id, user_id, row),
            messages=[
                self._message(item)
                for item in self._message_rows(tenant_id, user_id, conversation_id, limit=200)
            ],
        )

    def rename(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID, title: str
    ) -> ConversationDetail:
        nonce, ciphertext = self._encrypt_text(
            _title(title), _conversation_aad(tenant_id, user_id, conversation_id, "title")
        )
        with connect(self.database_url) as connection:
            updated = connection.execute(
                """UPDATE ai_conversations SET title_nonce = %s, title_ciphertext = %s,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE conversation_id = %s AND tenant_id = %s AND user_id = %s
                      AND retention_until > CURRENT_TIMESTAMP""",
                (nonce, ciphertext, conversation_id, int(tenant_id), user_id),
            ).rowcount
        if updated != 1:
            raise ConversationNotFound("Conversation was not found in the verified user scope.")
        return self.get(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)

    def delete(self, *, tenant_id: str, user_id: str, conversation_id: UUID) -> None:
        with connect(self.database_url) as connection:
            deleted = connection.execute(
                """DELETE FROM ai_conversations
                    WHERE conversation_id = %s AND tenant_id = %s AND user_id = %s""",
                (conversation_id, int(tenant_id), user_id),
            ).rowcount
        if deleted != 1:
            raise ConversationNotFound("Conversation was not found in the verified user scope.")

    def feedback(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: UUID,
        request: AnswerFeedbackRequest,
    ) -> FeedbackReceipt:
        comment_nonce: bytes | None = None
        comment_ciphertext: bytes | None = None
        if request.comment:
            comment_nonce, comment_ciphertext = self._encrypt_text(
                request.comment, f"{tenant_id}:{user_id}:{run_id}:feedback".encode()
            )
        with connect(self.database_url) as connection:
            owner = connection.execute(
                """SELECT run_id FROM ai_agent_runs
                     WHERE run_id = %s AND tenant_id = %s AND user_id = %s
                       AND run_state = 'COMPLETED'""",
                (run_id, int(tenant_id), user_id),
            ).fetchone()
            if owner is None:
                raise ConversationNotFound("The Agent run is not available to this user.")
            row = connection.execute(
                """INSERT INTO ai_answer_feedback (
                       run_id, tenant_id, user_id, rating, reason_codes,
                       comment_nonce, comment_ciphertext)
                   VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                   ON CONFLICT (run_id, user_id) DO UPDATE SET
                       rating = EXCLUDED.rating,
                       reason_codes = EXCLUDED.reason_codes,
                       comment_nonce = EXCLUDED.comment_nonce,
                       comment_ciphertext = EXCLUDED.comment_ciphertext,
                       updated_at = CURRENT_TIMESTAMP
                   RETURNING updated_at""",
                (
                    run_id,
                    int(tenant_id),
                    user_id,
                    request.rating,
                    json.dumps(request.reason_codes),
                    comment_nonce,
                    comment_ciphertext,
                ),
            ).fetchone()
        return FeedbackReceipt(run_id=run_id, rating=request.rating, recorded_at=row[0])

    def _message_rows(
        self, tenant_id: str, user_id: str, conversation_id: UUID, *, limit: int
    ) -> list[tuple]:
        with connect(self.database_url) as connection:
            owner = connection.execute(
                """SELECT conversation_id FROM ai_conversations
                     WHERE conversation_id = %s AND tenant_id = %s AND user_id = %s
                       AND retention_until > CURRENT_TIMESTAMP""",
                (conversation_id, int(tenant_id), user_id),
            ).fetchone()
            if owner is None:
                raise ConversationNotFound("Conversation was not found in the verified user scope.")
            rows = connection.execute(
                """SELECT message_id, role, payload_nonce, payload_ciphertext
                     FROM ai_conversation_messages
                    WHERE conversation_id = %s
                    ORDER BY created_at DESC, message_id DESC
                    LIMIT %s""",
                (conversation_id, max(1, min(limit, 200))),
            ).fetchall()
        rows.reverse()
        return rows

    def _message(self, row: tuple) -> ConversationMessage:
        message_id = UUID(str(row[0]))
        payload = self.cipher.decrypt_bytes(
            bytes(row[2]), bytes(row[3]), _message_aad(message_id)
        )
        return ConversationMessage.model_validate_json(payload)

    def _summary_from_row(self, tenant_id: str, user_id: str, row: tuple) -> ConversationSummary:
        conversation_id = UUID(str(row[0]))
        title = self._decrypt_text(
            bytes(row[6]),
            bytes(row[7]),
            _conversation_aad(tenant_id, user_id, conversation_id, "title"),
        )
        return ConversationSummary(
            conversation_id=conversation_id,
            title=title,
            locale=str(row[1]),
            message_count=int(row[2]),
            created_at=row[3],
            updated_at=row[4],
            last_message_at=row[5],
        )

    def _encrypt_text(self, value: str, aad: bytes) -> tuple[bytes, bytes]:
        return self.cipher.encrypt_bytes(value.encode("utf-8"), aad)

    def _decrypt_text(self, nonce: bytes, ciphertext: bytes, aad: bytes) -> str:
        return self.cipher.decrypt_bytes(nonce, ciphertext, aad).decode("utf-8")

    def _encrypt_message(self, message: ConversationMessage) -> tuple[bytes, bytes]:
        return self.cipher.encrypt_bytes(
            message.model_dump_json(by_alias=True).encode(),
            _message_aad(message.message_id),
        )


def _conversation_aad(
    tenant_id: str, user_id: str, conversation_id: UUID, field: str
) -> bytes:
    return f"{tenant_id}:{user_id}:{conversation_id}:{field}".encode()


def _message_aad(message_id: UUID) -> bytes:
    return f"dwaion-message:{message_id}".encode()
