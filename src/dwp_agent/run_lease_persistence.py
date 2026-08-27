from __future__ import annotations

from datetime import datetime
from uuid import UUID

from psycopg import Connection, connect


def require_active_run(
    database_url: str,
    *,
    run_id: str,
    generation: int,
    tenant_id: str,
    user_id: str,
    request_id: str,
) -> bool:
    with connect(database_url) as connection:
        return connection.execute(
            """SELECT 1 FROM ai_agent_runs
                WHERE run_id = %s AND tenant_id = %s AND user_id = %s
                  AND request_id = %s AND lease_generation = %s
                  AND run_state = 'RUNNING'
                  AND lease_expires_at > CURRENT_TIMESTAMP""",
            (UUID(run_id), int(tenant_id), user_id, request_id, generation),
        ).fetchone() is not None


def is_completed_run(
    database_url: str,
    *,
    run_id: str,
    generation: int,
    tenant_id: str,
    user_id: str,
    request_id: str,
) -> bool:
    with connect(database_url) as connection:
        return connection.execute(
            """SELECT 1 FROM ai_agent_runs
                WHERE run_id = %s AND tenant_id = %s AND user_id = %s
                  AND request_id = %s AND lease_generation = %s
                  AND run_state = 'COMPLETED'""",
            (UUID(run_id), int(tenant_id), user_id, request_id, generation),
        ).fetchone() is not None


def activate_conversation_messages(
    connection: Connection,
    *,
    run_id: str,
    generation: int,
    completed_at: datetime,
) -> None:
    connection.execute(
        """UPDATE ai_conversations conversation
              SET message_count = (
                      SELECT COUNT(*)::INTEGER
                        FROM ai_conversation_messages message
                        JOIN ai_agent_runs completed_run
                          ON completed_run.run_id = message.run_id
                         AND completed_run.run_state = 'COMPLETED'
                         AND completed_run.lease_generation = message.lease_generation
                       WHERE message.conversation_id = conversation.conversation_id
                  ),
                  updated_at = GREATEST(conversation.updated_at, %s),
                  last_message_at = GREATEST(conversation.last_message_at, %s)
            WHERE EXISTS (
                      SELECT 1 FROM ai_conversation_messages message
                       WHERE message.conversation_id = conversation.conversation_id
                         AND message.run_id = %s
                         AND message.lease_generation = %s
                  )""",
        (completed_at, completed_at, UUID(run_id), generation),
    )
