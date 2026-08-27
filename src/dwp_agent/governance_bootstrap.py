from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from psycopg import connect

from .governance_contracts import BootstrapGovernancePoliciesRequest
from .governance_errors import GovernancePolicyConflict


PolicyInitializer = Callable[[Any, int, str], int]


def bootstrap_governance_policies(
    *,
    database_url: str,
    tenant: int,
    actor_user_id: str,
    correlation_id: str,
    request: BootstrapGovernancePoliciesRequest,
    category: str,
    event_type: str,
    target_type: str,
    existing_count_sql: str,
    initialize: PolicyInitializer,
) -> None:
    command_key = f"{category}:BOOTSTRAP:{request.idempotency_key}"
    with connect(database_url) as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"dwp-agent:governance:{tenant}:{category}",),
        )
        replayed = connection.execute(
            """SELECT 1 FROM ai_governance_events
                WHERE tenant_id = %s AND category = %s AND event_type = %s
                  AND target_type = %s AND target_key = %s""",
            (tenant, category, event_type, target_type, command_key),
        ).fetchone()
        if replayed is not None:
            return

        existing_count = connection.execute(existing_count_sql, (tenant,)).fetchone()[0]
        if existing_count != request.expected_existing_count:
            raise GovernancePolicyConflict(
                "The governance policy set changed. Reload and retry."
            )
        created_count = initialize(connection, tenant, actor_user_id)
        connection.execute(
            """INSERT INTO ai_governance_events (
                   event_id, tenant_id, category, event_type, target_type,
                   target_key, actor_user_id, correlation_id, change_reason,
                   previous_value, current_value)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s::jsonb, %s::jsonb)""",
            (
                uuid4(),
                tenant,
                category,
                event_type,
                target_type,
                command_key,
                actor_user_id,
                correlation_id,
                request.change_reason,
                json.dumps({"existingCount": existing_count}),
                json.dumps(
                    {
                        "createdCount": created_count,
                        "totalCount": existing_count + created_count,
                    }
                ),
            ),
        )
