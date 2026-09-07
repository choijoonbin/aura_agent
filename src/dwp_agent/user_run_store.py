from __future__ import annotations

from typing import Protocol
from uuid import UUID

from psycopg import Error as PsycopgError
from psycopg import connect

from .activity_contracts import ActivityRunSnapshot
from .user_run_contracts import AgentRunState, UserAgentRunSummary
from .run_store import InMemoryRunStore, PostgresRunStore, get_run_store
from .run_store_errors import RunStoreUnavailable


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

    def get(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: UUID,
    ) -> UserAgentRunSummary | None: ...


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

    def get(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: UUID,
    ) -> UserAgentRunSummary | None:
        return None


class InMemoryUserRunStore:
    def __init__(self, runs: InMemoryRunStore) -> None:
        self.runs = runs

    def list(self, *, tenant_id: str, user_id: str, limit: int,
             run_state: AgentRunState | None) -> list[UserAgentRunSummary]:
        rows = self.runs.activity_snapshots(tenant_id=tenant_id, user_id=user_id)
        rows.sort(key=lambda row: (row.created_at, row.run_id), reverse=True)
        return [_in_memory_summary(row) for row in rows
                if run_state is None or row.run_state == run_state][:limit]

    def get(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: UUID,
    ) -> UserAgentRunSummary | None:
        row = next((candidate for candidate in self.runs.activity_snapshots(
            tenant_id=tenant_id, user_id=user_id) if candidate.run_id == run_id), None)
        return _in_memory_summary(row) if row is not None else None


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
                           MIN(conversation.conversation_id::text)::uuid, run.created_at, run.completed_at
                      FROM ai_agent_runs run
                      LEFT JOIN ai_conversation_messages message
                        ON message.run_id = run.run_id
                       AND message.lease_generation = run.lease_generation
                       AND run.run_state = 'COMPLETED'
                      LEFT JOIN ai_conversations conversation
                        ON conversation.conversation_id = message.conversation_id
                       AND conversation.tenant_id = run.tenant_id
                       AND conversation.user_id = run.user_id
                     WHERE run.tenant_id = %s AND run.user_id = %s
                       AND (%s::text IS NULL OR run.run_state = %s)
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
            _postgres_summary(row)
            for row in rows
        ]

    def get(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: UUID,
    ) -> UserAgentRunSummary | None:
        try:
            with connect(self.database_url, connect_timeout=5) as connection:
                connection.read_only = True
                connection.execute("SET LOCAL statement_timeout = '5s'")
                row = connection.execute(
                    """
                    SELECT run.run_id, run.agent_key, run.agent_revision, run.run_state,
                           run.answer_state, run.risk_tier, run.policy_outcome,
                           run.status_code, run.source_count, run.latency_ms,
                           MIN(conversation.conversation_id::text)::uuid, run.created_at, run.completed_at
                      FROM ai_agent_runs run
                      LEFT JOIN ai_conversation_messages message
                        ON message.run_id = run.run_id
                       AND message.lease_generation = run.lease_generation
                       AND run.run_state = 'COMPLETED'
                      LEFT JOIN ai_conversations conversation
                        ON conversation.conversation_id = message.conversation_id
                       AND conversation.tenant_id = run.tenant_id
                       AND conversation.user_id = run.user_id
                     WHERE run.tenant_id = %s AND run.user_id = %s AND run.run_id = %s
                     GROUP BY run.run_id
                    """,
                    (int(tenant_id), user_id, run_id),
                ).fetchone()
        except (PsycopgError, ValueError) as error:
            raise UserRunStoreUnavailable("The Agent activity store is unavailable.") from error
        return _postgres_summary(row) if row is not None else None


def _in_memory_summary(row: ActivityRunSnapshot) -> UserAgentRunSummary:
    return UserAgentRunSummary(
        run_id=row.run_id, agent_key=row.agent_key, agent_revision=row.agent_revision,
        run_state=row.run_state, answer_state=row.answer_state, risk_tier=row.risk_tier,
        policy_outcome=row.policy_outcome, status_code=row.status_code,
        source_count=row.source_count, latency_ms=row.latency_ms,
        created_at=row.created_at, completed_at=row.completed_at,
    )


def _postgres_summary(row: tuple) -> UserAgentRunSummary:
    return UserAgentRunSummary(
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


def get_user_run_store() -> UserRunStore:
    try:
        store = get_run_store()
    except RunStoreUnavailable as error:
        raise UserRunStoreUnavailable("The Agent activity store is unavailable.") from error
    if isinstance(store, InMemoryRunStore):
        return InMemoryUserRunStore(store)
    if isinstance(store, PostgresRunStore):
        return PostgresUserRunStore(store.database_url)
    raise UserRunStoreUnavailable("The Agent activity store is unavailable.")
