from __future__ import annotations

import json
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
    ConversationStoreUnavailable,
    ConversationTurn,
    _title,
    conversation_answer_metadata,
)
from .conversation_exchange import build_exchange, exchange_matches
from .conversation_summary_query import LATEST_VISIBLE_ASSISTANT_JOIN, SUMMARY_COLUMNS
from .envelope import KeyContext, PayloadEncryption
from .grounded_response_status import normalize_legacy_grounded_status
from .payload_contexts import (
    conversation_context,
    feedback_context,
    legacy_conversation_aad,
    legacy_message_aad,
    message_context,
)
from .run_store import RunLease


class PostgresConversationStore:
    def __init__(self, database_url: str, encryption: PayloadEncryption) -> None:
        self.database_url = database_url
        self.encryption = encryption

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

        with connect(self.database_url) as connection:
            policy = connection.execute(
                """SELECT retention_days FROM ai_conversation_retention_policies
                    WHERE tenant_id = %s""",
                (int(tenant_id),),
            ).fetchone()
            if policy is None:
                raise ConversationStoreUnavailable(
                    "The tenant retention policy requires explicit bootstrap."
                )
        created = uuid4()
        title_envelope = self._encrypt_text(
            _title(initial_query), conversation_context(tenant_id, created, "title")
        )
        with connect(self.database_url) as connection:
            connection.execute(
                """INSERT INTO ai_conversations (
                       conversation_id, tenant_id, user_id, locale,
                       title_envelope, retention_until)
                   VALUES (%s, %s, %s, %s, %s,
                           CURRENT_TIMESTAMP + make_interval(days => %s))""",
                (
                    created,
                    int(tenant_id),
                    user_id,
                    locale,
                    title_envelope,
                    policy[0],
                ),
            )
        return created

    def recent_history(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID, limit: int = 8
    ) -> tuple[ConversationTurn, ...]:
        rows = self._message_rows(tenant_id, user_id, conversation_id, limit=limit)
        return tuple(
            ConversationTurn(
                role=ConversationRole(row[1]),
                content=self._message(
                    tenant_id,
                    conversation_id,
                    row,
                    provider=str(row[6]) if row[6] is not None else None,
                ).content,
            )
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
        lease: RunLease,
    ) -> tuple[UUID, UUID]:
        if response.run_id != lease.run_id or response.request_id != request_id:
            raise ConversationStoreUnavailable(
                "The conversation response does not match the claimed run lease."
            )
        with connect(self.database_url) as connection:
            active_lease = connection.execute(
                """SELECT run_id FROM ai_agent_runs
                     WHERE run_id = %s AND tenant_id = %s AND user_id = %s
                       AND request_id = %s AND lease_generation = %s
                       AND run_state = 'RUNNING'
                       AND lease_expires_at > CURRENT_TIMESTAMP
                     FOR UPDATE""",
                (
                    UUID(lease.run_id),
                    int(tenant_id),
                    user_id,
                    request_id,
                    lease.generation,
                ),
            ).fetchone()
            if active_lease is None:
                raise ConversationStoreUnavailable("Agent run lease is no longer owned.")
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
            connection.execute(
                """DELETE FROM ai_conversation_messages message
                     USING ai_agent_runs run
                     WHERE message.conversation_id = %s
                       AND message.lease_generation IS NOT NULL
                       AND run.run_id = message.run_id
                       AND (run.lease_generation <> message.lease_generation
                            OR run.run_state = 'FAILED'
                            OR (run.run_state = 'RUNNING'
                                AND run.lease_expires_at <= CURRENT_TIMESTAMP))""",
                (conversation_id,),
            )
            existing = connection.execute(
                """SELECT message_id, role, payload_envelope, payload_nonce,
                          payload_ciphertext, encryption_key_version, run_id,
                          lease_generation
                     FROM ai_conversation_messages
                     WHERE conversation_id = %s AND request_id = %s""",
                (conversation_id, request_id),
            ).fetchall()
            if len(existing) == 2:
                messages = [self._message(tenant_id, conversation_id, row) for row in existing]
                database_run_ids = {
                    ConversationRole(row[1]): UUID(str(row[6])) if row[6] else None
                    for row in existing
                }
                if (
                    all(int(row[7]) == lease.generation for row in existing)
                    and database_run_ids.get(ConversationRole.USER) == UUID(lease.run_id)
                    and database_run_ids.get(ConversationRole.ASSISTANT) == UUID(lease.run_id)
                    and exchange_matches(messages, query=query, response=response)
                ):
                    by_role = {
                        ConversationRole(row[1]): UUID(str(row[0])) for row in existing
                    }
                    return by_role[ConversationRole.USER], by_role[ConversationRole.ASSISTANT]
            if existing:
                connection.execute(
                    """DELETE FROM ai_conversation_messages
                        WHERE conversation_id = %s AND request_id = %s""",
                    (conversation_id, request_id),
                )

            user_message, assistant_message = build_exchange(query, response)
            for message in (user_message, assistant_message):
                envelope = self._encrypt_message(
                    message, tenant_id=tenant_id, conversation_id=conversation_id
                )
                connection.execute(
                    """INSERT INTO ai_conversation_messages (
                           message_id, conversation_id, request_id, run_id, role,
                           payload_envelope, created_at, lease_generation)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        message.message_id,
                        conversation_id,
                        request_id,
                        UUID(lease.run_id),
                        message.role,
                        envelope,
                        message.created_at,
                        lease.generation,
                    ),
                )
            return user_message.message_id, assistant_message.message_id

    def list(self, *, tenant_id: str, user_id: str, limit: int = 30) -> list[ConversationSummary]:
        with connect(self.database_url) as connection:
            rows = connection.execute(
                f"""SELECT {SUMMARY_COLUMNS}
                     FROM ai_conversations conversation
                     LEFT JOIN ai_conversation_retention_policies policy
                       ON policy.tenant_id = conversation.tenant_id
                     {LATEST_VISIBLE_ASSISTANT_JOIN}
                    WHERE conversation.tenant_id = %s AND conversation.user_id = %s
                      AND conversation.message_count > 0
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
                f"""SELECT {SUMMARY_COLUMNS}
                     FROM ai_conversations conversation
                     LEFT JOIN ai_conversation_retention_policies policy
                       ON policy.tenant_id = conversation.tenant_id
                     {LATEST_VISIBLE_ASSISTANT_JOIN}
                    WHERE conversation.conversation_id = %s
                      AND conversation.tenant_id = %s AND conversation.user_id = %s
                      AND conversation.message_count > 0
                      AND (conversation.retention_until > CURRENT_TIMESTAMP
                           OR COALESCE(policy.legal_hold, FALSE))""",
                (conversation_id, int(tenant_id), user_id),
            ).fetchone()
        if row is None:
            raise ConversationNotFound("Conversation was not found in the verified user scope.")
        return ConversationDetail(
            summary=self._summary_from_row(tenant_id, user_id, row),
            messages=[
                self._message(
                    tenant_id,
                    conversation_id,
                    item,
                    provider=str(item[6]) if item[6] is not None else None,
                )
                for item in self._message_rows(tenant_id, user_id, conversation_id, limit=200)
            ],
        )

    def rename(
        self, *, tenant_id: str, user_id: str, conversation_id: UUID, title: str
    ) -> ConversationDetail:
        title_envelope = self._encrypt_text(
            _title(title), conversation_context(tenant_id, conversation_id, "title")
        )
        with connect(self.database_url) as connection:
            updated = connection.execute(
                """UPDATE ai_conversations conversation
                      SET title_envelope = %s,
                          title_nonce = NULL, title_ciphertext = NULL,
                          encryption_key_version = NULL,
                          updated_at = CURRENT_TIMESTAMP
                     FROM ai_conversation_retention_policies policy
                    WHERE policy.tenant_id = conversation.tenant_id
                      AND conversation.conversation_id = %s
                      AND conversation.tenant_id = %s AND conversation.user_id = %s
                      AND (conversation.retention_until > CURRENT_TIMESTAMP
                           OR policy.legal_hold)""",
                (title_envelope, conversation_id, int(tenant_id), user_id),
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
        comment_envelope: str | None = None
        if request.comment:
            comment_envelope = self._encrypt_text(
                request.comment, feedback_context(tenant_id, run_id)
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
                       comment_envelope)
                   VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                   ON CONFLICT (run_id, user_id) DO UPDATE SET
                       rating = EXCLUDED.rating,
                       reason_codes = EXCLUDED.reason_codes,
                       comment_envelope = EXCLUDED.comment_envelope,
                       comment_nonce = NULL,
                       comment_ciphertext = NULL,
                       encryption_key_version = NULL,
                       updated_at = CURRENT_TIMESTAMP
                   RETURNING updated_at""",
                (
                    run_id,
                    int(tenant_id),
                    user_id,
                    request.rating,
                    json.dumps(request.reason_codes),
                    comment_envelope,
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
                """SELECT message.message_id, message.role, message.payload_envelope,
                          message.payload_nonce, message.payload_ciphertext,
                          message.encryption_key_version, completed_run.provider
                     FROM ai_conversation_messages message
                     JOIN ai_agent_runs completed_run
                      ON completed_run.run_id = message.run_id
                      AND completed_run.tenant_id = %s
                      AND completed_run.user_id = %s
                      AND completed_run.request_id = message.request_id
                      AND completed_run.run_state = 'COMPLETED'
                      AND completed_run.lease_generation = message.lease_generation
                    WHERE message.conversation_id = %s
                    ORDER BY message.created_at DESC, message.message_id DESC
                    LIMIT %s""",
                (int(tenant_id), user_id, conversation_id, max(1, min(limit, 200))),
            ).fetchall()
        rows.reverse()
        return rows

    def _message(
        self,
        tenant_id: str,
        conversation_id: UUID,
        row: tuple,
        *,
        provider: str | None = None,
    ) -> ConversationMessage:
        message_id = UUID(str(row[0]))
        payload = self.encryption.decrypt_bytes(
            envelope=str(row[2]) if row[2] is not None else None,
            context=message_context(tenant_id, conversation_id, message_id),
            legacy_version=str(row[5]) if row[5] is not None else None,
            legacy_nonce=bytes(row[3]) if row[3] is not None else None,
            legacy_ciphertext=bytes(row[4]) if row[4] is not None else None,
            legacy_aad=legacy_message_aad(message_id),
        )
        message = ConversationMessage.model_validate_json(payload)
        normalized_status = normalize_legacy_grounded_status(provider, message.status_code)
        if normalized_status == message.status_code:
            return message
        return message.model_copy(update={"status_code": normalized_status})

    def _summary_from_row(self, tenant_id: str, user_id: str, row: tuple) -> ConversationSummary:
        conversation_id = UUID(str(row[0]))
        title = self._decrypt_text(
            envelope=str(row[6]) if row[6] is not None else None,
            context=conversation_context(tenant_id, conversation_id, "title"),
            legacy_version=str(row[9]) if row[9] is not None else None,
            legacy_nonce=bytes(row[7]) if row[7] is not None else None,
            legacy_ciphertext=bytes(row[8]) if row[8] is not None else None,
            legacy_aad=legacy_conversation_aad(tenant_id, user_id, conversation_id, "title"),
        )
        last_assistant = None
        if row[12] is not None:
            provider = str(row[18]) if row[18] is not None else None
            last_assistant = self._message(
                tenant_id, conversation_id, tuple(row[12:19]), provider=provider
            )
        return ConversationSummary(
            conversation_id=conversation_id,
            title=title,
            locale=str(row[1]),
            message_count=int(row[2]),
            **conversation_answer_metadata([] if last_assistant is None else [last_assistant]),
            retention_until=row[10],
            legal_hold=bool(row[11]),
            created_at=row[3],
            updated_at=row[4],
            last_message_at=row[5],
        )

    def _encrypt_text(self, value: str, context: KeyContext) -> str:
        return self.encryption.encrypt_bytes(value.encode("utf-8"), context)

    def _decrypt_text(
        self,
        *,
        envelope: str | None,
        context: KeyContext,
        legacy_version: str | None,
        legacy_nonce: bytes | None,
        legacy_ciphertext: bytes | None,
        legacy_aad: bytes,
    ) -> str:
        return self.encryption.decrypt_bytes(
            envelope=envelope,
            context=context,
            legacy_version=legacy_version,
            legacy_nonce=legacy_nonce,
            legacy_ciphertext=legacy_ciphertext,
            legacy_aad=legacy_aad,
        ).decode("utf-8")

    def _encrypt_message(
        self,
        message: ConversationMessage,
        *,
        tenant_id: str,
        conversation_id: UUID,
    ) -> str:
        return self.encryption.encrypt_bytes(
            message.model_dump_json(by_alias=True).encode(),
            message_context(tenant_id, conversation_id, message.message_id),
        )
