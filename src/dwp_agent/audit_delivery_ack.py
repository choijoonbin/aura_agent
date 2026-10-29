from __future__ import annotations

import logging
import os
from datetime import datetime
from uuid import UUID

from psycopg import Error as PsycopgError
from psycopg import connect


LOGGER = logging.getLogger("uvicorn.error.dwp.audit.delivery")


def acknowledge_agent_run_delivery(event: object) -> bool:
    """Bind a delivered AGENT_RUN event to its exact local source-ledger row."""
    if not isinstance(event, dict) or event.get("targetType") != "AGENT_RUN":
        return True
    if event.get("sourceService") != "dwp-agent-runtime":
        LOGGER.critical("Delivered AGENT_RUN audit acknowledgement rejected its source service")
        return False

    tenant = event.get("tenantId")
    if isinstance(tenant, bool) or not isinstance(tenant, int) or tenant <= 0:
        LOGGER.critical("Delivered AGENT_RUN audit acknowledgement rejected its tenant binding")
        return False
    try:
        run_id = UUID(str(event.get("targetId", "")))
        event_id = UUID(str(event.get("eventId", "")))
    except (TypeError, ValueError):
        LOGGER.critical("Delivered AGENT_RUN audit acknowledgement rejected its identifier binding")
        return False
    occurred_at = _occurred_at(event.get("occurredAt"))
    metadata = event.get("metadata")
    legacy_schema = not (
        isinstance(metadata, dict) and metadata.get("auditLinkSchemaVersion") == 2
    )

    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        LOGGER.warning("Delivered AGENT_RUN audit acknowledgement deferred without a database")
        return False
    try:
        with connect(database_url, connect_timeout=5) as connection:
            connection.execute("SET LOCAL lock_timeout = '2s'")
            connection.execute("SET LOCAL statement_timeout = '5s'")
            # V73 installs the exact-attempt ledger before V79 backfills existing
            # projections. Materialize a still-current pre-cutover projection so
            # a collector acknowledgement in that window cannot be mistaken for
            # an unrecoverable legacy event and delete its only spool copy.
            connection.execute(
                """
                INSERT INTO ai_agent_run_audit_links (
                    run_id, lease_generation, audit_id, event_id,
                    link_state, linked_at)
                SELECT run.run_id,
                       run.lease_generation,
                       run.current_audit_id,
                       run.audit_record_id,
                       run.audit_link_state,
                       CASE WHEN run.audit_link_state = 'LINKED'
                           THEN CURRENT_TIMESTAMP ELSE NULL END
                  FROM ai_agent_runs run
                 WHERE run.tenant_id = %s
                   AND run.run_id = %s
                   AND run.audit_record_id = %s
                   AND run.current_audit_id IS NOT NULL
                   AND run.audit_link_state IN ('PENDING', 'LINKED')
                ON CONFLICT (run_id, lease_generation) DO NOTHING
                """,
                (tenant, run_id, event_id),
            )
            row = connection.execute(
                """
                WITH acknowledged AS (
                    UPDATE ai_agent_run_audit_links link
                       SET link_state = 'LINKED',
                           linked_at = COALESCE(linked_at, CURRENT_TIMESTAMP)
                      FROM ai_agent_runs run
                     WHERE run.run_id = link.run_id
                       AND run.tenant_id = %s
                       AND link.run_id = %s
                       AND link.event_id = %s
                       AND link.link_state IN ('PENDING', 'LINKED')
                    RETURNING link.run_id, link.event_id, link.link_state
                ), projected AS (
                    UPDATE ai_agent_runs run
                       SET audit_link_state = 'LINKED'
                      FROM acknowledged ack
                     WHERE run.run_id = ack.run_id
                       AND run.audit_record_id = ack.event_id
                    RETURNING run.run_id
                ), legacy_inserted AS (
                    INSERT INTO ai_agent_run_audit_legacy_receipts (
                        event_id, run_id, reason)
                    SELECT %s, run.run_id, 'PRE_V73_UNRECOVERABLE_LINK'
                      FROM ai_agent_runs run
                      JOIN ai_agent_run_audit_link_cutover cutover
                        ON cutover.singleton = 1
                     WHERE NOT EXISTS (SELECT 1 FROM acknowledged)
                       AND %s
                       AND %s <= cutover.cutover_at + INTERVAL '5 minutes'
                       AND run.tenant_id = %s
                       AND run.run_id = %s
                       AND run.audit_record_id IS DISTINCT FROM %s
                       AND run.created_at <= cutover.cutover_at
                       AND NOT EXISTS (
                           SELECT 1 FROM ai_agent_run_audit_links link
                            WHERE link.event_id = %s)
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING 'LEGACY_QUARANTINED'::varchar AS result
                ), legacy_existing AS (
                    SELECT 'LEGACY_QUARANTINED'::varchar AS result
                      FROM ai_agent_run_audit_legacy_receipts receipt
                      JOIN ai_agent_runs run ON run.run_id = receipt.run_id
                     WHERE receipt.event_id = %s
                       AND run.tenant_id = %s
                       AND run.run_id = %s
                )
                SELECT result
                  FROM (
                    SELECT 0 AS priority, link_state AS result FROM acknowledged
                    UNION ALL
                    SELECT 1 AS priority, result FROM legacy_inserted
                    UNION ALL
                    SELECT 2 AS priority, result FROM legacy_existing
                     WHERE NOT EXISTS (SELECT 1 FROM legacy_inserted)
                  ) outcome
                 ORDER BY priority
                 LIMIT 1
                """,
                (
                    tenant,
                    run_id,
                    event_id,
                    event_id,
                    legacy_schema,
                    occurred_at,
                    tenant,
                    run_id,
                    event_id,
                    event_id,
                    event_id,
                    tenant,
                    run_id,
                ),
            ).fetchone()
    except (PsycopgError, ValueError):
        LOGGER.warning("Delivered AGENT_RUN audit acknowledgement deferred by its database")
        return False
    if row is None:
        LOGGER.critical("Delivered AGENT_RUN audit acknowledgement rejected a ledger mismatch")
        return False
    if row[0] == "LEGACY_QUARANTINED":
        LOGGER.critical(
            "Delivered pre-V73 AGENT_RUN audit event quarantined after collector acceptance"
        )
        return True
    if row[0] != "LINKED":
        LOGGER.critical("Delivered AGENT_RUN audit acknowledgement returned an invalid state")
        return False
    return True


def _occurred_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
