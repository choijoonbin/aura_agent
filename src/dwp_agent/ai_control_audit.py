from __future__ import annotations

import json
from uuid import uuid4


def record_ai_control_event(
    connection,
    tenant,
    event_type,
    actor,
    correlation,
    reason,
    previous,
    current,
    *,
    target_key=None,
    request_fingerprint=None,
) -> None:
    current_value = current.model_dump(mode="json")
    if request_fingerprint is not None:
        current_value = {
            "requestFingerprint": request_fingerprint,
            "policy": current_value,
        }
    connection.execute(
        """INSERT INTO ai_governance_events (
               event_id, tenant_id, category, event_type, target_type, target_key,
               actor_user_id, correlation_id, change_reason, previous_value, current_value)
           VALUES (%s, %s, 'AI_CONTROL', %s, 'AI_EXECUTION_POLICY', %s,
                   %s, %s, %s, %s::jsonb, %s::jsonb)""",
        (
            uuid4(), tenant, event_type, target_key or str(tenant), actor, correlation,
            reason,
            json.dumps(previous.model_dump(mode="json") if previous else None),
            json.dumps(current_value),
        ),
    )
