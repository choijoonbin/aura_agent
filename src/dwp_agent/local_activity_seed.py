from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from psycopg import connect

from .key_provider import normalized_environment
from .run_observability import audit_record_id


TENANT_ID = 1
USER_ID = "900018"


class LocalActivitySeedConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalActivitySeedResult:
    tenant_id: int
    user_id: str
    run_count: int


def seed_local_activity() -> LocalActivitySeedResult | None:
    if not _enabled("DWP_AGENT_LOCAL_ACTIVITY_SEED_ENABLED"):
        return None
    if normalized_environment() != "local":
        raise LocalActivitySeedConfigurationError(
            "Local Agent activity seed is allowed only in DWP_ENVIRONMENT=local."
        )
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise LocalActivitySeedConfigurationError(
            "Local Agent activity seed requires DWP_AGENT_DATABASE_URL."
        )
    with connect(database_url) as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"dwp-agent:local-activity:{TENANT_ID}:{USER_ID}",),
        )
        fixtures = _fixtures()
        _validate_existing_seed_targets(connection, fixtures, require_all=False)
        for offset, fixture in enumerate(fixtures):
            _insert_run(connection, fixture, offset)
        _validate_existing_seed_targets(connection, fixtures, require_all=True)
        for offset, fixture in enumerate(fixtures):
            _insert_stages(connection, fixture, offset)
            _insert_source_health(connection, fixture, offset)
    return LocalActivitySeedResult(TENANT_ID, USER_ID, len(fixtures))


def _fixtures() -> tuple[dict[str, object], ...]:
    return (
        {
            "key": "completed-grounded",
            "state": "COMPLETED",
            "answer": "COMPLETED",
            "status": "READ_ONLY_GROUNDED_ANSWER",
            "policy": "ALLOW",
            "latency": 842,
            "sources": 3,
            "stages": ("AUTHORIZING", "RETRIEVING", "REASONING", "VERIFYING", "PERSISTING"),
            "health": (("WORK_ITEM", "SUCCESS", 84), ("MAIL", "SUCCESS", 126)),
        },
        {
            "key": "policy-handoff",
            "state": "COMPLETED",
            "answer": "ABSTAINED",
            "status": "ASK_POLICY_HANDOFF",
            "policy": "HANDOFF",
            "latency": 0,
            "sources": 0,
            "stages": ("AUTHORIZING", "SKIP", "SKIP", "SKIP", "PERSISTING"),
            "health": (),
        },
        {
            "key": "source-unavailable",
            "state": "COMPLETED",
            "answer": "ABSTAINED",
            "status": "CONTEXT_SOURCE_UNAVAILABLE",
            "policy": "ALLOW",
            "latency": 0,
            "sources": 0,
            "stages": ("AUTHORIZING", "RETRIEVING", "SKIP", "SKIP", "PERSISTING"),
            "health": (("CALENDAR", "NOT_CONFIGURED", None), ("MAIL", "UNAVAILABLE", 3001)),
        },
        {
            "key": "runtime-failed",
            "state": "FAILED",
            "answer": None,
            "status": "ASK_RUNTIME_FAILED",
            "policy": "ALLOW",
            "latency": 0,
            "sources": 0,
            "stages": ("AUTHORIZING", "FAILED", "ABSENT", "ABSENT", "ABSENT"),
            "health": (),
        },
    )


def _insert_run(connection, fixture: dict[str, object], offset: int) -> None:
    run_id = _id(str(fixture["key"]))
    audit_id = str(_id(f"audit:{fixture['key']}"))
    connection.execute(
        """INSERT INTO ai_agent_runs (
               run_id, tenant_id, user_id, request_id, query_hash, agent_key,
               agent_revision, run_state, answer_state, risk_tier, policy_outcome,
               status_code, locale, latency_ms, source_count, correlation_id,
               created_at, completed_at, lease_expires_at, lease_generation,
               current_audit_id, audit_record_id, audit_link_state, data_provenance)
           VALUES (%s, 1, '900018', %s, %s, 'DWP_ASSISTANT', 1, %s, %s,
                   'L1', %s, %s, 'ko-KR', %s, %s, %s,
                   CURRENT_TIMESTAMP - INTERVAL '2 hours' - (%s * INTERVAL '11 minutes'),
                   CURRENT_TIMESTAMP - INTERVAL '2 hours' - (%s * INTERVAL '11 minutes') + INTERVAL '4 seconds',
                   NULL, 1, %s, %s, 'PENDING', 'SAMPLE')
           ON CONFLICT (run_id) DO NOTHING""",
        (
            run_id, f"local-activity-{fixture['key']}",
            hashlib.sha256(f"local:{fixture['key']}".encode()).hexdigest(),
            fixture["state"], fixture["answer"], fixture["policy"], fixture["status"],
            fixture["latency"], fixture["sources"],
            f"local-activity-{fixture['key']}", offset + 1, offset + 1,
            audit_id, audit_record_id(audit_id),
        ),
    )


def _validate_existing_seed_targets(
    connection,
    fixtures: tuple[dict[str, object], ...],
    *,
    require_all: bool,
) -> None:
    expected_by_run = {
        _id(str(fixture["key"])): f"local-activity-{fixture['key']}"
        for fixture in fixtures
    }
    expected_by_request = {
        request_id: run_id for run_id, request_id in expected_by_run.items()
    }
    rows = connection.execute(
        """SELECT run_id, tenant_id, user_id, request_id, data_provenance
             FROM ai_agent_runs
            WHERE run_id = ANY(%s::uuid[])
               OR (tenant_id = %s AND user_id = %s
                   AND request_id = ANY(%s::text[]))
            FOR UPDATE""",
        (
            list(expected_by_run),
            TENANT_ID,
            USER_ID,
            list(expected_by_request),
        ),
    ).fetchall()
    observed: set[object] = set()
    for run_id, tenant_id, user_id, request_id, provenance in rows:
        expected_run_id = expected_by_request.get(request_id)
        if (
            expected_run_id != run_id
            or expected_by_run.get(run_id) != request_id
            or tenant_id != TENANT_ID
            or user_id != USER_ID
            or provenance != "SAMPLE"
        ):
            raise LocalActivitySeedConfigurationError(
                "Local Agent activity seed collides with an existing run identity."
            )
        observed.add(run_id)
    if require_all and observed != set(expected_by_run):
        raise LocalActivitySeedConfigurationError(
            "Local Agent activity seed did not persist every expected SAMPLE run."
        )


def _insert_stages(connection, fixture: dict[str, object], offset: int) -> None:
    run_id = _id(str(fixture["key"]))
    keys = ("AUTHORIZING", "RETRIEVING", "REASONING", "VERIFYING", "PERSISTING")
    stages = fixture["stages"]
    for index, (key, state) in enumerate(zip(keys, stages, strict=True), start=1):
        if state == "ABSENT":
            break
        if state == "FAILED":
            stored_state = "FAILED"
        elif state == "SKIP":
            stored_state = "SKIPPED"
        else:
            stored_state = "COMPLETED"
        connection.execute(
            """INSERT INTO ai_agent_run_stages (
                   run_id, lease_generation, stage_key, stage_state, sequence,
                   started_at, completed_at)
               VALUES (%s, 1, %s, %s, %s,
                       CURRENT_TIMESTAMP - INTERVAL '2 hours' - (%s * INTERVAL '11 minutes')
                           + (%s * INTERVAL '200 milliseconds'),
                       CURRENT_TIMESTAMP - INTERVAL '2 hours' - (%s * INTERVAL '11 minutes')
                           + (%s * INTERVAL '200 milliseconds'))
               ON CONFLICT (run_id, lease_generation, stage_key) DO NOTHING""",
            (run_id, key, stored_state, index * 10,
             offset + 1, index, offset + 1, index + 1),
        )
        if stored_state == "FAILED":
            break
    terminal = "COMPLETED" if fixture["state"] == "COMPLETED" else "FAILED"
    terminal_state = terminal
    connection.execute(
        """INSERT INTO ai_agent_run_stages (
               run_id, lease_generation, stage_key, stage_state, sequence,
               started_at, completed_at)
           VALUES (%s, 1, %s, %s, 60,
                   CURRENT_TIMESTAMP - INTERVAL '2 hours' - (%s * INTERVAL '11 minutes')
                       + INTERVAL '4 seconds',
                   CURRENT_TIMESTAMP - INTERVAL '2 hours' - (%s * INTERVAL '11 minutes')
                       + INTERVAL '4 seconds')
           ON CONFLICT (run_id, lease_generation, stage_key) DO NOTHING""",
        (run_id, terminal, terminal_state, offset + 1, offset + 1),
    )


def _insert_source_health(connection, fixture: dict[str, object], offset: int) -> None:
    for source_type, status, latency in fixture["health"]:
        connection.execute(
            """INSERT INTO ai_agent_run_source_health (
                   run_id, lease_generation, source_type, health_status,
                   latency_ms, last_attempt_at, last_success_at)
               VALUES (%s, 1, %s, %s, %s,
                       CURRENT_TIMESTAMP - INTERVAL '2 hours' - (%s * INTERVAL '11 minutes'),
                       CASE WHEN %s = 'SUCCESS' THEN
                           CURRENT_TIMESTAMP - INTERVAL '2 hours' - (%s * INTERVAL '11 minutes')
                       ELSE NULL END)
               ON CONFLICT (run_id, lease_generation, source_type) DO NOTHING""",
            (_id(str(fixture["key"])), source_type, status, latency,
             offset + 1, status, offset + 1),
        )


def _id(key: str):
    return uuid5(NAMESPACE_URL, f"dwp://local-activity/{TENANT_ID}/{USER_ID}/{key}")


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}
