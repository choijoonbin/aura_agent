from __future__ import annotations

import os
from typing import Protocol

from psycopg import Error as PsycopgError
from psycopg import connect

from .user_run_contracts import AgentRunState, UserAgentRunSummary


class UserRunStoreUnavailable(RuntimeError):
    pass


class UserRunStore(Protocol):
    def list(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
        run_state: AgentRunState | None,
    ) -> list[UserAgentRunSummary]: ...


class EmptyUserRunStore:
    def list(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
        run_state: AgentRunState | None,
    ) -> list[UserAgentRunSummary]:
        return []


class PostgresUserRunStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def list(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int,
        run_state: AgentRunState | None,
    ) -> list[UserAgentRunSummary]:
        try:
            with connect(self.database_url) as connection:
                rows = connection.execute(
                    """
                    SELECT run.run_id, run.agent_key, run.agent_revision, run.run_state,
                           run.answer_state, run.risk_tier, run.policy_outcome,
                           run.status_code, run.source_count, run.latency_ms,
                           MIN(conversation.conversation_id), run.created_at, run.completed_at
                      FROM ai_agent_runs run
                      LEFT JOIN ai_conversation_messages message
                        ON message.run_id = run.run_id
                      LEFT JOIN ai_conversations conversation
                        ON conversation.conversation_id = message.conversation_id
                       AND conversation.tenant_id = run.tenant_id
                       AND conversation.user_id = run.user_id
                     WHERE run.tenant_id = %s AND run.user_id = %s
                       AND (%s IS NULL OR run.run_state = %s)
                     GROUP BY run.run_id
                     ORDER BY run.created_at DESC
                     LIMIT %s
                    """,
                    (
                        int(tenant_id),
                        user_id,
                        run_state.value if run_state else None,
                        run_state.value if run_state else None,
                        limit,
                    ),
                ).fetchall()
        except (PsycopgError, ValueError) as error:
            raise UserRunStoreUnavailable("The Agent activity store is unavailable.") from error
        return [
            UserAgentRunSummary(
                run_id=row[0],
                agent_key=row[1],
                agent_revision=row[2],
                run_state=row[3],
                answer_state=row[4],
                risk_tier=row[5],
                policy_outcome=row[6],
                status_code=row[7],
                source_count=row[8],
                latency_ms=row[9],
                conversation_id=row[10],
                created_at=row[11],
                completed_at=row[12],
            )
            for row in rows
        ]


def get_user_run_store() -> UserRunStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    return PostgresUserRunStore(database_url) if database_url else EmptyUserRunStore()
