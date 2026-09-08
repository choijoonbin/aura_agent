from __future__ import annotations

from uuid import UUID

from psycopg import connect

from .run_observability import (
    OPERATIONAL_STAGES,
    RunStageKey,
    RunStageState,
    SourceHealthObservation,
    stage_sequence,
)
from .run_store_errors import RunStoreUnavailable
from .run_store_types import RunLease


def start_stage(connection, run_id: str, generation: int) -> None:
    connection.execute(
        """INSERT INTO ai_agent_run_stages (
               run_id, lease_generation, stage_key, stage_state, sequence, started_at)
           VALUES (%s, %s, 'AUTHORIZING', 'ACTIVE', %s, CURRENT_TIMESTAMP)
           ON CONFLICT (run_id, lease_generation, stage_key) DO NOTHING""",
        (UUID(run_id), generation, stage_sequence(RunStageKey.AUTHORIZING)),
    )


def advance_stage(
    database_url: str,
    lease: RunLease,
    *,
    tenant_id: str,
    user_id: str,
    request_id: str,
    stage: RunStageKey,
) -> None:
    desired_sequence = stage_sequence(stage)
    with connect(database_url) as connection:
        _require_active(connection, lease, tenant_id, user_id, request_id)
        latest = connection.execute(
            """SELECT stage_key, sequence FROM ai_agent_run_stages
                WHERE run_id = %s AND lease_generation = %s
                ORDER BY sequence DESC LIMIT 1 FOR UPDATE""",
            (UUID(lease.run_id), lease.generation),
        ).fetchone()
        if latest is None:
            raise RunStoreUnavailable("Agent run stage ledger is unavailable.")
        if desired_sequence < int(latest[1]) or (
            desired_sequence == int(latest[1]) and stage.value != latest[0]
        ):
            raise RunStoreUnavailable("Agent run stage cannot move backwards.")
        if stage.value == latest[0]:
            return
        connection.execute(
            """UPDATE ai_agent_run_stages
                  SET stage_state = 'COMPLETED', completed_at = CURRENT_TIMESTAMP
                WHERE run_id = %s AND lease_generation = %s
                  AND stage_key = %s AND stage_state = 'ACTIVE'""",
            (UUID(lease.run_id), lease.generation, latest[0]),
        )
        for skipped in OPERATIONAL_STAGES:
            skipped_sequence = stage_sequence(skipped)
            if int(latest[1]) < skipped_sequence < desired_sequence:
                connection.execute(
                    """INSERT INTO ai_agent_run_stages (
                           run_id, lease_generation, stage_key, stage_state, sequence,
                           started_at, completed_at)
                       VALUES (%s, %s, %s, 'SKIPPED', %s,
                               CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                       ON CONFLICT (run_id, lease_generation, stage_key) DO NOTHING""",
                    (UUID(lease.run_id), lease.generation, skipped.value, skipped_sequence),
                )
        connection.execute(
            """INSERT INTO ai_agent_run_stages (
                   run_id, lease_generation, stage_key, stage_state, sequence, started_at)
               VALUES (%s, %s, %s, 'ACTIVE', %s, CURRENT_TIMESTAMP)
               ON CONFLICT (run_id, lease_generation, stage_key) DO NOTHING""",
            (UUID(lease.run_id), lease.generation, stage.value, desired_sequence),
        )


def record_source_health(
    database_url: str,
    lease: RunLease,
    *,
    tenant_id: str,
    user_id: str,
    request_id: str,
    observations: tuple[SourceHealthObservation, ...],
) -> None:
    if not observations:
        return
    with connect(database_url) as connection:
        _require_active(connection, lease, tenant_id, user_id, request_id)
        for observation in observations:
            connection.execute(
                """INSERT INTO ai_agent_run_source_health (
                       run_id, lease_generation, source_type, health_status,
                       latency_ms, last_attempt_at, last_success_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (run_id, lease_generation, source_type) DO NOTHING""",
                (
                    UUID(lease.run_id), lease.generation, observation.source_type,
                    observation.status.value, observation.latency_ms,
                    observation.observed_at,
                    (observation.observed_at
                     if observation.status.value == "SUCCESS" else None),
                ),
            )


def finish_attempt_stage(connection, lease: RunLease, terminal: RunStageKey) -> None:
    terminal_state = (
        RunStageState.COMPLETED.value
        if terminal == RunStageKey.COMPLETED
        else RunStageState.FAILED.value
    )
    connection.execute(
        """UPDATE ai_agent_run_stages
              SET stage_state = %s, completed_at = CURRENT_TIMESTAMP
            WHERE run_id = %s AND lease_generation = %s AND stage_state = 'ACTIVE'""",
        (terminal_state, UUID(lease.run_id), lease.generation),
    )
    if terminal == RunStageKey.COMPLETED:
        for skipped in OPERATIONAL_STAGES:
            connection.execute(
                """INSERT INTO ai_agent_run_stages (
                       run_id, lease_generation, stage_key, stage_state, sequence,
                       started_at, completed_at)
                   VALUES (%s, %s, %s, 'SKIPPED', %s,
                           CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                   ON CONFLICT (run_id, lease_generation, stage_key) DO NOTHING""",
                (UUID(lease.run_id), lease.generation, skipped.value, stage_sequence(skipped)),
            )
    connection.execute(
        """INSERT INTO ai_agent_run_stages (
               run_id, lease_generation, stage_key, stage_state, sequence,
               started_at, completed_at)
           VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
           ON CONFLICT (run_id, lease_generation, stage_key) DO NOTHING""",
        (
            UUID(lease.run_id), lease.generation, terminal.value, terminal_state,
            stage_sequence(terminal),
        ),
    )


def _require_active(
    connection, lease: RunLease, tenant_id: str, user_id: str, request_id: str,
) -> None:
    active = connection.execute(
        """SELECT 1 FROM ai_agent_runs
            WHERE run_id = %s AND lease_generation = %s
              AND tenant_id = %s AND user_id = %s AND request_id = %s
              AND run_state = 'RUNNING'
              AND lease_expires_at > CURRENT_TIMESTAMP
            FOR UPDATE""",
        (UUID(lease.run_id), lease.generation, int(tenant_id), user_id, request_id),
    ).fetchone()
    if active is None:
        raise RunStoreUnavailable("Agent run lease is no longer owned.")
