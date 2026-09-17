from __future__ import annotations

import hashlib
import json
from typing import Any

from .personal_routine_evidence_contracts import RoutineTelemetryEvent


def evidence_integrity(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def telemetry_event(source: str, row: Any) -> RoutineTelemetryEvent:
    payload = {
        "source": source,
        "eventId": str(row["event_id"]),
        "routineRunId": str(row["routine_run_id"]) if row["routine_run_id"] else None,
        "eventType": row["event_type"],
        "previousState": row["previous_state"],
        "currentState": row["current_state"],
        "version": row["version"],
        "occurredAt": row["occurred_at"].isoformat(),
    }
    return RoutineTelemetryEvent.model_validate(
        {**payload, "integrityFingerprint": evidence_integrity(payload)}
    )
