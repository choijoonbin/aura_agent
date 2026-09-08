from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from psycopg import Error as PsycopgError
from psycopg import connect

from .activity_contracts import ActivityRunSnapshot
from .user_run_contracts import (
    AgentRunState,
    RunAuditEvidenceStatus,
    RunLeaseStatus,
    RunMeasurementStatus,
    UserAgentRunAuditEvidence,
    UserAgentRunLease,
    UserAgentRunSourceHealth,
    UserAgentRunStage,
    UserAgentRunSummary,
)
from .run_store import InMemoryRunStore, PostgresRunStore, get_run_store
from .run_store_errors import RunStoreUnavailable
from .run_observability import (
    RunSourceHealthSnapshot,
    RunSourceHealthStatus,
    RunStageKey,
    RunStageSnapshot,
    RunStageState,
    progress_percent,
    safe_activity_title,
)


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
        return [_in_memory_summary(row, self.runs) for row in rows
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
        return _in_memory_summary(row, self.runs) if row is not None else None


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
                _configure_read_snapshot(connection)
                rows = connection.execute(
                    """
                    SELECT run.run_id, run.agent_key, run.agent_revision, run.run_state,
                           run.answer_state, run.risk_tier, run.policy_outcome,
                           run.status_code, run.source_count, run.latency_ms,
                           MIN(conversation.conversation_id::text)::uuid, run.created_at,
                           run.completed_at, run.lease_generation, run.lease_expires_at,
                           run.current_audit_id, run.audit_record_id,
                           run.audit_link_state, run.data_provenance
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
                stages, sources = _postgres_observability(
                    connection, tenant_id, user_id, rows
                )
        except (PsycopgError, ValueError) as error:
            raise UserRunStoreUnavailable("The Agent activity store is unavailable.") from error
        return [
            _postgres_summary(row, stages.get(row[0], ()), sources.get(row[0], ()))
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
                _configure_read_snapshot(connection)
                row = connection.execute(
                    """
                    SELECT run.run_id, run.agent_key, run.agent_revision, run.run_state,
                           run.answer_state, run.risk_tier, run.policy_outcome,
                           run.status_code, run.source_count, run.latency_ms,
                           MIN(conversation.conversation_id::text)::uuid, run.created_at,
                           run.completed_at, run.lease_generation, run.lease_expires_at,
                           run.current_audit_id, run.audit_record_id,
                           run.audit_link_state, run.data_provenance
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
                stages, sources = _postgres_observability(
                    connection, tenant_id, user_id, [row] if row is not None else []
                )
        except (PsycopgError, ValueError) as error:
            raise UserRunStoreUnavailable("The Agent activity store is unavailable.") from error
        return (
            _postgres_summary(row, stages.get(row[0], ()), sources.get(row[0], ()))
            if row is not None else None
        )


def _in_memory_summary(
    row: ActivityRunSnapshot, store: InMemoryRunStore,
) -> UserAgentRunSummary:
    stages = store.run_stages(
        tenant_id=row.tenant_id, user_id=row.user_id,
        run_id=row.run_id, generation=row.generation,
    )
    sources = store.run_source_health(
        tenant_id=row.tenant_id, user_id=row.user_id,
        run_id=row.run_id, generation=row.generation,
    )
    return _summary(
        run_id=row.run_id, agent_key=row.agent_key, agent_revision=row.agent_revision,
        run_state=row.run_state, answer_state=row.answer_state, risk_tier=row.risk_tier,
        policy_outcome=row.policy_outcome, status_code=row.status_code,
        source_count=row.source_count, latency_ms=row.latency_ms,
        conversation_id=None, created_at=row.created_at, completed_at=row.completed_at,
        generation=row.generation, lease_expires_at=row.lease_expires_at,
        audit_id=row.audit_id, audit_record_id=row.audit_record_id,
        audit_link_state=row.audit_link_state, data_provenance=row.data_provenance,
        stages=stages, sources=sources,
    )


def _postgres_summary(
    row: tuple,
    stages: tuple[RunStageSnapshot, ...],
    sources: tuple[RunSourceHealthSnapshot, ...],
) -> UserAgentRunSummary:
    return _summary(
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
        generation=row[13], lease_expires_at=row[14], audit_id=row[15],
        audit_record_id=row[16], audit_link_state=row[17],
        data_provenance=row[18], stages=stages, sources=sources,
    )


def _summary(*, generation: int, lease_expires_at: datetime | None,
             audit_id: str | None, audit_record_id: UUID | None,
             audit_link_state: str | None, data_provenance: str,
             stages: tuple[RunStageSnapshot, ...],
             sources: tuple[RunSourceHealthSnapshot, ...], **values) -> UserAgentRunSummary:
    now = datetime.now(timezone.utc)
    run_state = str(values["run_state"])
    progress = progress_percent(stages)
    if not stages:
        measurement = RunMeasurementStatus.NOT_AVAILABLE
    elif run_state == AgentRunState.RUNNING:
        measurement = RunMeasurementStatus.MEASURING
    elif run_state == AgentRunState.COMPLETED and progress == 100:
        measurement = RunMeasurementStatus.MEASURED
    else:
        measurement = RunMeasurementStatus.PARTIAL
    lease_status = RunLeaseStatus.RELEASED
    if run_state == AgentRunState.RUNNING:
        lease_status = (
            RunLeaseStatus.ACTIVE
            if lease_expires_at is not None and lease_expires_at > now
            else RunLeaseStatus.EXPIRED
        )
    current_stage = stages[-1].key if stages else None
    audit_status = RunAuditEvidenceStatus.NOT_AVAILABLE
    if audit_id and audit_record_id:
        audit_status = (
            RunAuditEvidenceStatus.LINKED
            if audit_link_state == "LINKED"
            else RunAuditEvidenceStatus.PENDING
        )
    return UserAgentRunSummary(
        **values,
        activity_title=safe_activity_title(str(values["agent_key"])),
        attempt=max(1, generation),
        lease=UserAgentRunLease(status=lease_status, expires_at=lease_expires_at),
        current_stage=current_stage,
        progress_percent=progress,
        measurement_status=measurement,
        stages=[UserAgentRunStage(
            key=stage.key, state=stage.state, sequence=stage.sequence,
            started_at=stage.started_at, completed_at=stage.completed_at,
            duration_ms=stage.duration_ms(now),
        ) for stage in stages],
        audit_evidence=UserAgentRunAuditEvidence(
            audit_id=audit_id, audit_record_id=audit_record_id, status=audit_status,
        ),
        source_health=[UserAgentRunSourceHealth(
            source_type=source.source_type, status=source.status,
            latency_ms=source.latency_ms, last_attempt_at=source.last_attempt_at,
            last_success_at=source.last_success_at,
        ) for source in sources],
        data_provenance=data_provenance,
    )


def _configure_read_snapshot(connection) -> None:
    connection.execute(
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )
    connection.execute("SET LOCAL statement_timeout = '5s'")


def _postgres_observability(connection, tenant_id: str, user_id: str, rows: list[tuple]):
    if not rows:
        return {}, {}
    run_ids = [row[0] for row in rows]
    generations = {row[0]: int(row[13]) for row in rows}
    stage_rows = connection.execute(
        """SELECT stage.run_id, stage.lease_generation, stage.stage_key,
                  stage.stage_state, stage.sequence, stage.started_at,
                  stage.completed_at
             FROM ai_agent_run_stages stage
             JOIN ai_agent_runs run ON run.run_id = stage.run_id
            WHERE run.tenant_id = %s AND run.user_id = %s
              AND stage.run_id = ANY(%s::uuid[])
            ORDER BY stage.run_id, stage.lease_generation, stage.sequence""",
        (int(tenant_id), user_id, run_ids),
    ).fetchall()
    source_rows = connection.execute(
        """SELECT health.run_id, health.lease_generation, health.source_type,
                  health.health_status, health.latency_ms, health.last_attempt_at,
                  health.last_success_at
             FROM ai_agent_run_source_health health
             JOIN ai_agent_runs run ON run.run_id = health.run_id
            WHERE run.tenant_id = %s AND run.user_id = %s
              AND health.run_id = ANY(%s::uuid[])
            ORDER BY health.run_id, health.lease_generation, health.source_type""",
        (int(tenant_id), user_id, run_ids),
    ).fetchall()
    stages: dict[UUID, list[RunStageSnapshot]] = {}
    for row in stage_rows:
        if generations.get(row[0]) != int(row[1]):
            continue
        stages.setdefault(row[0], []).append(RunStageSnapshot(
            key=RunStageKey(row[2]), state=RunStageState(row[3]), sequence=row[4],
            started_at=row[5], completed_at=row[6],
        ))
    sources: dict[UUID, list[RunSourceHealthSnapshot]] = {}
    for row in source_rows:
        if generations.get(row[0]) != int(row[1]):
            continue
        sources.setdefault(row[0], []).append(RunSourceHealthSnapshot(
            source_type=row[2], status=RunSourceHealthStatus(row[3]), latency_ms=row[4],
            last_attempt_at=row[5], last_success_at=row[6],
        ))
    return (
        {key: tuple(value) for key, value in stages.items()},
        {key: tuple(value) for key, value in sources.items()},
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
