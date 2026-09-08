from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from psycopg import Error as PsycopgError, connect

from .activity_contracts import ActivityRunSnapshot
from .run_store import InMemoryRunStore, PostgresRunStore, get_run_store
from .run_store_errors import RunStoreUnavailable
from .run_observability import RunDataProvenance, RunStageKey


class ActivityStoreUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ActivityFilters:
    actor: str = "ALL"
    state: str = "ALL"
    query: str = ""
    source: str = ""
    object_type: str = ""
    object_id: str = ""
    execution_id: str = ""
    from_at: datetime | None = None
    to_at: datetime | None = None

    def matches(self, row: ActivityRunSnapshot, now: datetime) -> bool:
        return (
            row.data_provenance == "LIVE"
            and
            self.actor in {"ALL", "AGENT"}
            and self.source in {"", "DWAI_ON"}
            and self.object_type in {"", "AGENT_RUN"}
            and (not self.object_id or self.object_id == str(row.run_id))
            and (not self.execution_id or self.execution_id == str(row.run_id))
            and (self.state == "ALL" or self.state == row.activity_state(now))
            and (not self.from_at or row.created_at >= self.from_at)
            and (not self.to_at or row.created_at < self.to_at)
            and (not self.query or self.query.casefold() in _search_text(row).casefold())
        )


def _search_text(row: ActivityRunSnapshot) -> str:
    return f"DWAI_ON DWAI·ON Agent execution 에이전트 실행 {row.agent_key} {row.status_code or ''} {row.run_id}"


class AgentActivityStore(Protocol):
    def page(self, *, tenant_id: str, user_id: str, filters: ActivityFilters,
             snapshot_at: datetime, now: datetime, after: tuple[datetime, UUID] | None,
             limit: int) -> tuple[list[ActivityRunSnapshot], bool]: ...
    def detail(self, *, tenant_id: str, user_id: str, run_id: UUID) -> ActivityRunSnapshot | None: ...
    def counts(self, *, tenant_id: str, user_id: str, filters: ActivityFilters,
               now: datetime) -> dict[str, int]: ...


class InMemoryAgentActivityStore:
    def __init__(self, runs: InMemoryRunStore) -> None:
        self.runs = runs

    def page(self, *, tenant_id: str, user_id: str, filters: ActivityFilters,
             snapshot_at: datetime, now: datetime, after: tuple[datetime, UUID] | None,
             limit: int) -> tuple[list[ActivityRunSnapshot], bool]:
        rows = [row for row in self.runs.activity_snapshots(tenant_id=tenant_id, user_id=user_id)
                if row.created_at <= snapshot_at and filters.matches(row, now)
                and (after is None or (row.created_at, row.run_id) < after)]
        rows.sort(key=lambda row: (row.created_at, row.run_id), reverse=True)
        return rows[:limit], len(rows) > limit

    def detail(self, *, tenant_id: str, user_id: str, run_id: UUID) -> ActivityRunSnapshot | None:
        return next((row for row in self.runs.activity_snapshots(tenant_id=tenant_id, user_id=user_id)
                     if row.run_id == run_id and row.data_provenance == "LIVE"), None)

    def counts(self, *, tenant_id: str, user_id: str, filters: ActivityFilters,
               now: datetime) -> dict[str, int]:
        return dict(Counter(row.activity_state(now) for row in self.runs.activity_snapshots(
            tenant_id=tenant_id, user_id=user_id) if filters.matches(row, now)))


_COLUMNS = """run_id, tenant_id, user_id, agent_key, agent_revision, run_state,
    risk_tier, policy_outcome, created_at, lease_generation, answer_state, status_code,
    source_count, latency_ms, completed_at, lease_expires_at, current_audit_id,
    audit_record_id, audit_link_state, data_provenance,
    (SELECT stage.stage_key FROM ai_agent_run_stages stage
      WHERE stage.run_id = ai_agent_runs.run_id
        AND stage.lease_generation = ai_agent_runs.lease_generation
      ORDER BY stage.sequence DESC LIMIT 1),
    (SELECT COUNT(*) * 20 FROM ai_agent_run_stages stage
      WHERE stage.run_id = ai_agent_runs.run_id
        AND stage.lease_generation = ai_agent_runs.lease_generation
        AND stage.stage_key NOT IN ('COMPLETED', 'FAILED')
        AND stage.stage_state IN ('COMPLETED', 'SKIPPED'))"""

# No source payload, query, response envelope, correlation text or citation is read.
_STATE = """CASE WHEN run_state = 'RUNNING' THEN
    CASE WHEN lease_expires_at IS NULL OR lease_expires_at <= %s THEN 'UNKNOWN' ELSE 'RUNNING' END
    WHEN run_state = 'FAILED' THEN 'FAILED'
    WHEN policy_outcome = 'DENY' THEN 'POLICY_BLOCKED'
    WHEN policy_outcome = 'HANDOFF' THEN 'NEEDS_INPUT' ELSE 'COMPLETED' END"""


class PostgresAgentActivityStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def _where(self, tenant_id: str, user_id: str, filters: ActivityFilters,
               now: datetime) -> tuple[str, list[object]]:
        parts = ["tenant_id = %s", "user_id = %s", "data_provenance = 'LIVE'"]
        params: list[object] = [int(tenant_id), user_id]
        if filters.actor not in {"ALL", "AGENT"} or filters.source not in {"", "DWAI_ON"} or filters.object_type not in {"", "AGENT_RUN"}:
            parts.append("FALSE")
        for value in (filters.object_id, filters.execution_id):
            if value:
                parts.append("run_id::text = %s")
                params.append(value)
        if filters.state != "ALL":
            parts.append(f"({_STATE}) = %s")
            params.extend([now, filters.state])
        if filters.from_at:
            parts.append("created_at >= %s")
            params.append(filters.from_at)
        if filters.to_at:
            parts.append("created_at < %s")
            params.append(filters.to_at)
        if filters.query:
            parts.append("""POSITION(LOWER(%s) IN LOWER(CONCAT(
                'DWAI_ON DWAI·ON Agent execution 에이전트 실행 ', agent_key, ' ', status_code, ' ', run_id::text))) > 0""")
            params.append(filters.query)
        return " AND ".join(parts), params

    def _query(self, sql: str, params: list[object]) -> list[tuple]:
        try:
            with connect(self.database_url, connect_timeout=5) as connection:
                connection.read_only = True
                connection.execute("SET LOCAL statement_timeout = '5s'")
                return connection.execute(sql, params).fetchall()
        except (PsycopgError, ValueError) as error:
            raise ActivityStoreUnavailable("The Agent execution source is unavailable.") from error

    def page(self, *, tenant_id: str, user_id: str, filters: ActivityFilters,
             snapshot_at: datetime, now: datetime, after: tuple[datetime, UUID] | None,
             limit: int) -> tuple[list[ActivityRunSnapshot], bool]:
        where, params = self._where(tenant_id, user_id, filters, now)
        where += " AND created_at <= %s"
        params.append(snapshot_at)
        if after is not None:
            where += " AND (created_at, run_id) < (%s, %s)"
            params.extend(after)
        params.append(limit + 1)
        rows = self._query(f"SELECT {_COLUMNS} FROM ai_agent_runs WHERE {where} ORDER BY created_at DESC, run_id DESC LIMIT %s", params)
        return [_snapshot(row) for row in rows[:limit]], len(rows) > limit

    def detail(self, *, tenant_id: str, user_id: str, run_id: UUID) -> ActivityRunSnapshot | None:
        rows = self._query(f"SELECT {_COLUMNS} FROM ai_agent_runs WHERE tenant_id = %s AND user_id = %s AND run_id = %s AND data_provenance = 'LIVE'", [int(tenant_id), user_id, run_id])
        return _snapshot(rows[0]) if rows else None

    def counts(self, *, tenant_id: str, user_id: str, filters: ActivityFilters,
               now: datetime) -> dict[str, int]:
        where, params = self._where(tenant_id, user_id, filters, now)
        rows = self._query(f"SELECT {_STATE} AS activity_state, COUNT(*) FROM ai_agent_runs WHERE {where} GROUP BY 1", [now, *params])
        return {str(row[0]): int(row[1]) for row in rows}


def _snapshot(row: tuple) -> ActivityRunSnapshot:
    current_stage = RunStageKey(row[20]) if row[20] is not None else None
    return ActivityRunSnapshot(
        run_id=row[0], tenant_id=str(row[1]), user_id=row[2], agent_key=row[3],
        agent_revision=row[4], run_state=row[5], risk_tier=row[6], policy_outcome=row[7],
        created_at=row[8], generation=row[9], answer_state=row[10], status_code=row[11],
        source_count=row[12], latency_ms=row[13], completed_at=row[14], lease_expires_at=row[15],
        audit_id=row[16], audit_record_id=row[17], audit_link_state=row[18],
        data_provenance=RunDataProvenance(row[19]), current_stage=current_stage,
        progress_percent=int(row[21]) if row[20] is not None else None,
    )


def get_agent_activity_store() -> AgentActivityStore:
    # Use the same initialized store as the executor; do not silently change source
    # if configuration is edited while the process is running.
    try:
        store = get_run_store()
    except RunStoreUnavailable as error:
        raise ActivityStoreUnavailable("The Agent execution source is unavailable.") from error
    if isinstance(store, InMemoryRunStore):
        return InMemoryAgentActivityStore(store)
    if isinstance(store, PostgresRunStore):
        return PostgresAgentActivityStore(store.database_url)
    raise ActivityStoreUnavailable("The Agent execution source is unavailable.")
