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
from .conversation_store import (
    ConversationNotFound,
    ConversationRetentionLocked,
    ConversationTurn,
    _title,
)
from .crypto import PayloadCipherKeyring


class PostgresConversationStore:
    def __init__(
        self, database_url: str, keyring: PayloadCipherKeyring, retention_days: int
    ) -> None:
        self.database_url = database_url
        self.keyring = keyring
        self.retention_days = retention_days

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
                    """SELECT conversation.conversation_id
                         FROM ai_conversations conversation
                         LEFT JOIN ai_conversation_retention_policies policy
                           ON policy.tenant_id = conversation.tenant_id
                        WHERE conversation.conversation_id = %s
                          AND conversation.tenant_id = %s AND conversation.user_id = %s
                          AND (conversation.retention_until > CURRENT_TIMESTAMP
                               OR COALESCE(policy.legal_hold, FALSE))""",
                    (conversation_id, int(tenant_id), user_id),
                ).fetchone()
            if row is None:
                raise ConversationNotFound("Conversation was not found in the verified user scope.")
            return conversation_id

        created = uuid4()
        key_version, nonce, ciphertext = self._encrypt_text(
            _title(initial_query), _conversation_aad(tenant_id, user_id, created, "title")
        )
        with connect(self.database_url) as connection:
            connection.execute(
                """INSERT INTO ai_conversation_retention_policies (tenant_id, retention_days)
                   VALUES (%s, %s)
                   ON CONFLICT (tenant_id) DO NOTHING""",
                (int(tenant_id), self.retention_days),
            )
            retention_days = connection.execute(
                """SELECT retention_days FROM ai_conversation_retention_policies
                    WHERE tenant_id = %s""",
                (int(tenant_id),),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO ai_conversations (
                       conversation_id, tenant_id, user_id, locale,
                       title_nonce, title_ciphertext, encryption_key_version, retention_until)
                   VALUES (%s, %s, %s, %s, %s, %s, %s,
                           CURRENT_TIMESTAMP + make_interval(days => %s))""",
                (
                    created,
                    int(tenant_id),
                    user_id,
                    locale,
                    nonce,
                    ciphertext,
                    key_version,
                    retention_days,
                ),
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
                """SELECT conversation.conversation_id
                     FROM ai_conversations conversation
                     LEFT JOIN ai_conversation_retention_policies policy
                       ON policy.tenant_id = conversation.tenant_id
                    WHERE conversation.conversation_id = %s
                      AND conversation.tenant_id = %s AND conversation.user_id = %s
                      AND (conversation.retention_until > CURRENT_TIMESTAMP
                           OR COALESCE(policy.legal_hold, FALSE))
                     FOR UPDATE OF conversation""",
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
                key_version, nonce, ciphertext = self._encrypt_message(message)
                connection.execute(
                    """INSERT INTO ai_conversation_messages (
                           message_id, conversation_id, request_id, run_id, role,
                           payload_nonce, payload_ciphertext, encryption_key_version, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        message.message_id,
                        conversation_id,
                        request_id,
                        message.run_id,
                        message.role,
                        nonce,
                        ciphertext,
                        key_version,
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
                """DELETE FROM ai_conversations conversation
                    WHERE conversation.retention_until <= CURRENT_TIMESTAMP
                      AND NOT EXISTS (
                          SELECT 1 FROM ai_conversation_retention_policies policy
                           WHERE policy.tenant_id = conversation.tenant_id
                             AND policy.legal_hold)"""
            )
            rows = connection.execute(
                """SELECT conversation.conversation_id, conversation.locale,
                          conversation.message_count, conversation.created_at,
                          conversation.updated_at, conversation.last_message_at,
                          conversation.title_nonce, conversation.title_ciphertext,
                          conversation.encryption_key_version
                     FROM ai_conversations conversation
                     LEFT JOIN ai_conversation_retention_policies policy
                       ON policy.tenant_id = conversation.tenant_id
                    WHERE conversation.tenant_id = %s AND conversation.user_id = %s
                      AND (conversation.retention_until > CURRENT_TIMESTAMP
                           OR COALESCE(policy.legal_hold, FALSE))
                    ORDER BY conversation.last_message_at DESC
                    LIMIT %s""",
                (int(tenant_id), user_id, max(1, min(limit, 100))),
            ).fetchall()
        return [self._summary_from_row(tenant_id, user_id, row) for row in rows]

    def get(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID
    ) -> ConversationDetail:
        with connect(self.database_url) as connection:
            row = connection.execute(
                """SELECT conversation.conversation_id, conversation.locale,
                          conversation.message_count, conversation.created_at,
                          conversation.updated_at, conversation.last_message_at,
                          conversation.title_nonce, conversation.title_ciphertext,
                          conversation.encryption_key_version
                     FROM ai_conversations conversation
                     LEFT JOIN ai_conversation_retention_policies policy
                       ON policy.tenant_id = conversation.tenant_id
                    WHERE conversation.conversation_id = %s
                      AND conversation.tenant_id = %s AND conversation.user_id = %s
                      AND (conversation.retention_until > CURRENT_TIMESTAMP
                           OR COALESCE(policy.legal_hold, FALSE))""",
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
        key_version, nonce, ciphertext = self._encrypt_text(
            _title(title), _conversation_aad(tenant_id, user_id, conversation_id, "title")
        )
        with connect(self.database_url) as connection:
            updated = connection.execute(
                """UPDATE ai_conversations conversation
                      SET title_nonce = %s, title_ciphertext = %s,
                          encryption_key_version = %s,
                          updated_at = CURRENT_TIMESTAMP
                     FROM ai_conversation_retention_policies policy
                    WHERE policy.tenant_id = conversation.tenant_id
                      AND conversation.conversation_id = %s
                      AND conversation.tenant_id = %s AND conversation.user_id = %s
                      AND (conversation.retention_until > CURRENT_TIMESTAMP
                           OR policy.legal_hold)""",
                (nonce, ciphertext, key_version, conversation_id, int(tenant_id), user_id),
            ).rowcount
        if updated != 1:
            raise ConversationNotFound("Conversation was not found in the verified user scope.")
        return self.get(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)

    def delete(self, *, tenant_id: str, user_id: str, conversation_id: UUID) -> None:
        with connect(self.database_url) as connection:
            legal_hold = connection.execute(
                """SELECT policy.legal_hold
                     FROM ai_conversations conversation
                     JOIN ai_conversation_retention_policies policy
                       ON policy.tenant_id = conversation.tenant_id
                    WHERE conversation.conversation_id = %s
                      AND conversation.tenant_id = %s AND conversation.user_id = %s""",
                (conversation_id, int(tenant_id), user_id),
            ).fetchone()
            if legal_hold and legal_hold[0]:
                raise ConversationRetentionLocked(
                    "Conversation deletion is blocked by the tenant legal-hold policy."
                )
            deleted = connection.execute(
                """DELETE FROM ai_conversations
                    WHERE conversation_id = %s AND tenant_id = %s AND user_id = %s
                      AND retention_until > CURRENT_TIMESTAMP""",
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
        key_version = self.keyring.active_version
        if request.comment:
            key_version, comment_nonce, comment_ciphertext = self._encrypt_text(
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
                       comment_nonce, comment_ciphertext, encryption_key_version)
                   VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                   ON CONFLICT (run_id, user_id) DO UPDATE SET
                       rating = EXCLUDED.rating,
                       reason_codes = EXCLUDED.reason_codes,
                       comment_nonce = EXCLUDED.comment_nonce,
                       comment_ciphertext = EXCLUDED.comment_ciphertext,
                       encryption_key_version = EXCLUDED.encryption_key_version,
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
                    key_version,
                ),
            ).fetchone()
        return FeedbackReceipt(run_id=run_id, rating=request.rating, recorded_at=row[0])

    def _message_rows(
        self, tenant_id: str, user_id: str, conversation_id: UUID, *, limit: int
    ) -> list[tuple]:
        with connect(self.database_url) as connection:
            owner = connection.execute(
                """SELECT conversation.conversation_id
                     FROM ai_conversations conversation
                     LEFT JOIN ai_conversation_retention_policies policy
                       ON policy.tenant_id = conversation.tenant_id
                    WHERE conversation.conversation_id = %s
                      AND conversation.tenant_id = %s AND conversation.user_id = %s
                      AND (conversation.retention_until > CURRENT_TIMESTAMP
                           OR COALESCE(policy.legal_hold, FALSE))""",
                (conversation_id, int(tenant_id), user_id),
            ).fetchone()
            if owner is None:
                raise ConversationNotFound("Conversation was not found in the verified user scope.")
            rows = connection.execute(
                """SELECT message_id, role, payload_nonce, payload_ciphertext,
                          encryption_key_version
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
        payload = self.keyring.decrypt_bytes(
            str(row[4]), bytes(row[2]), bytes(row[3]), _message_aad(message_id)
        )
        return ConversationMessage.model_validate_json(payload)

    def _summary_from_row(self, tenant_id: str, user_id: str, row: tuple) -> ConversationSummary:
        conversation_id = UUID(str(row[0]))
        title = self._decrypt_text(
            str(row[8]),
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

    def _encrypt_text(self, value: str, aad: bytes) -> tuple[str, bytes, bytes]:
        return self.keyring.encrypt_bytes(value.encode("utf-8"), aad)

    def _decrypt_text(
        self, version: str, nonce: bytes, ciphertext: bytes, aad: bytes
    ) -> str:
        return self.keyring.decrypt_bytes(version, nonce, ciphertext, aad).decode("utf-8")

    def _encrypt_message(self, message: ConversationMessage) -> tuple[str, bytes, bytes]:
        return self.keyring.encrypt_bytes(
            message.model_dump_json(by_alias=True).encode(),
            _message_aad(message.message_id),
        )


def _conversation_aad(
    tenant_id: str, user_id: str, conversation_id: UUID, field: str
) -> bytes:
    return f"{tenant_id}:{user_id}:{conversation_id}:{field}".encode()


def _message_aad(message_id: UUID) -> bytes:
    return f"dwaion-message:{message_id}".encode()
