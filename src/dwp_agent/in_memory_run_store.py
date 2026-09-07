from __future__ import annotations

import hmac
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import UUID

from .activity_contracts import ActivityRunSnapshot
from .contracts import AskResponse
from .run_store_errors import RequestIdConflict, RunStoreUnavailable
from .run_store_types import RunLease, RunStart


class InMemoryRunStore:
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._responses: dict[tuple[str, str, str], AskResponse] = {}
        self._pending: dict[tuple[str, str, str], RunLease] = {}
        self._run_keys: dict[str, tuple[str, str, str]] = {}
        self._completed_leases: dict[str, RunLease] = {}
        self._canonical_runs: dict[tuple[str, str, str], RunLease] = {}
        self._query_hashes: dict[tuple[str, str, str], str] = {}
        self._activity: dict[str, ActivityRunSnapshot] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def load(
        self,
        tenant_id: str,
        user_id: str,
        request_id: str,
        query_hash: str,
    ) -> AskResponse | None:
        key = (tenant_id, user_id, request_id)
        with self._lock:
            existing_hash = self._query_hashes.get(key)
            if existing_hash is not None and not hmac.compare_digest(existing_hash, query_hash):
                raise RequestIdConflict("The request ID was already used for another query.")
            return self._responses.get(key)

    def begin(self, start: RunStart) -> RunLease | None:
        key = (start.tenant_id, start.user_id, start.request_id)
        with self._lock:
            existing_hash = self._query_hashes.get(key)
            if existing_hash is not None and not hmac.compare_digest(
                existing_hash, start.query_hash
            ):
                raise RequestIdConflict("The request ID was already used for another query.")
            now = self._clock()
            pending = self._pending.get(key)
            if key in self._responses or (pending and self._lease_active(pending, now)):
                return None
            previous = self._canonical_runs.get(key)
            lease = RunLease(
                run_id=previous.run_id if previous else start.run_id,
                generation=previous.generation + 1 if previous else 1,
            )
            self._pending[key] = lease
            self._run_keys[lease.run_id] = key
            self._canonical_runs[key] = lease
            self._query_hashes[key] = start.query_hash
            previous_snapshot = self._activity.get(lease.run_id)
            self._activity[lease.run_id] = ActivityRunSnapshot(
                run_id=UUID(lease.run_id), tenant_id=start.tenant_id, user_id=start.user_id,
                agent_key=start.agent_key, agent_revision=start.agent_revision,
                run_state="RUNNING", risk_tier=start.risk_tier,
                policy_outcome=start.policy_outcome,
                created_at=previous_snapshot.created_at if previous_snapshot else now,
                generation=lease.generation, lease_expires_at=now + timedelta(minutes=2),
            )
            return lease

    def _lease_active(self, lease: RunLease, now: datetime) -> bool:
        snapshot = self._activity.get(lease.run_id)
        return bool(snapshot and snapshot.generation == lease.generation
                    and snapshot.lease_expires_at and snapshot.lease_expires_at > now)

    def activity_snapshots(self, *, tenant_id: str, user_id: str) -> list[ActivityRunSnapshot]:
        with self._lock:
            return [snapshot for snapshot in self._activity.values()
                    if snapshot.tenant_id == tenant_id and snapshot.user_id == user_id]

    def require_active(
        self,
        lease: RunLease,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
    ) -> None:
        key = (tenant_id, user_id, request_id)
        with self._lock:
            if (self._run_keys.get(lease.run_id) != key or self._pending.get(key) != lease
                    or not self._lease_active(lease, self._clock())):
                raise RunStoreUnavailable("Agent run lease is no longer owned.")

    def is_completed(
        self,
        lease: RunLease,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str,
    ) -> bool:
        key = (tenant_id, user_id, request_id)
        with self._lock:
            response = self._responses.get(key)
            return bool(
                response
                and response.run_id == lease.run_id
                and self._completed_leases.get(lease.run_id) == lease
            )

    def complete(
        self,
        response: AskResponse,
        *,
        lease: RunLease,
        tenant_id: str,
        user_id: str,
        provider_request_hash: str | None = None,
    ) -> None:
        key = (tenant_id, user_id, response.request_id)
        with self._lock:
            if (response.run_id != lease.run_id or self._pending.get(key) != lease
                    or not self._lease_active(lease, self._clock())):
                raise RunStoreUnavailable("Agent run lease is no longer owned.")
            self._responses[key] = response
            self._activity[lease.run_id] = replace(
                self._activity[lease.run_id], run_state="COMPLETED",
                answer_state=str(response.state), status_code=response.status_code,
                source_count=response.source_count, latency_ms=response.model_route.latency_ms,
                completed_at=response.completed_at, lease_expires_at=None,
            )
            self._completed_leases[lease.run_id] = lease
            self._pending.pop(key, None)
            self._run_keys.pop(lease.run_id, None)

    def fail(self, lease: RunLease, safe_error_code: str) -> None:
        with self._lock:
            key = self._run_keys.get(lease.run_id)
            if (key is not None and self._pending.get(key) == lease
                    and self._lease_active(lease, self._clock())):
                self._activity[lease.run_id] = replace(
                    self._activity[lease.run_id], run_state="FAILED",
                    completed_at=self._clock(), lease_expires_at=None,
                )
                self._run_keys.pop(lease.run_id, None)
                self._pending.pop(key, None)
