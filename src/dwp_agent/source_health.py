from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic_ns

import httpx

from .run_observability import RunSourceHealthStatus, SourceHealthObservation


def source_observation(
    source_type: str,
    status: RunSourceHealthStatus,
    latency_ms: int | None,
    *,
    operational: bool = True,
) -> SourceHealthObservation:
    return SourceHealthObservation(
        source_type=source_type,
        status=status,
        observed_at=datetime.now(timezone.utc),
        latency_ms=latency_ms,
        operational=operational,
    )


def operational_http_failure(
    error: httpx.HTTPStatusError,
    *,
    non_operational_statuses: frozenset[int] = frozenset(),
) -> bool:
    return error.response.status_code not in non_operational_statuses


def elapsed_ms(started: int) -> int:
    return max(0, (monotonic_ns() - started) // 1_000_000)
