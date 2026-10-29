from __future__ import annotations

from uuid import UUID

from .run_observability import audit_record_id


def record_run_audit_link(
    connection,
    *,
    run_id: UUID,
    lease_generation: int,
    audit_id: str | None,
) -> None:
    if audit_id is None:
        return
    event_id = audit_record_id(audit_id)
    connection.execute(
        """
        INSERT INTO ai_agent_run_audit_links (
            run_id, lease_generation, audit_id, event_id, link_state)
        VALUES (%s, %s, %s, %s, 'PENDING')
        ON CONFLICT (run_id, lease_generation) DO UPDATE
            SET audit_id = EXCLUDED.audit_id,
                event_id = EXCLUDED.event_id
          WHERE ai_agent_run_audit_links.audit_id = EXCLUDED.audit_id
            AND ai_agent_run_audit_links.event_id = EXCLUDED.event_id
        """,
        (run_id, lease_generation, audit_id, event_id),
    )
