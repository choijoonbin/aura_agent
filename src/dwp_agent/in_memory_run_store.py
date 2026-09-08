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
from .run_observability import (
    RunSourceHealthSnapshot,
    OPERATIONAL_STAGES,
    RunStageKey,
    RunStageSnapshot,
    RunStageState,
    SourceHealthObservation,
    audit_record_id,
    progress_percent,
    stage_sequence,
)


class InMemoryRunStore:
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._responses: dict[tuple[str, str, str], AskResponse] = {}
        self._pending: dict[tuple[str, str, str], RunLease] = {}
        self._run_keys: dict[str, tuple[str, str, str]] = {}
        self._completed_leases: dict[str, RunLease] = {}
        self._canonical_runs: dict[tuple[str, str, str], RunLease] = {}
        self._query_hashes: dict[tuple[str, str, str], str] = {}
        self._activity: dict[str, ActivityRunSnapshot] = {}
        self._stages: dict[tuple[str, int, RunStageKey], RunStageSnapshot] = {}
        self._source_health: dict[tuple[str, int, str], RunSourceHealthSnapshot] = {}
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
            record_id = audit_record_id(start.audit_id) if start.audit_id else None
            self._activity[lease.run_id] = ActivityRunSnapshot(
                run_id=UUID(lease.run_id), tenant_id=start.tenant_id, user_id=start.user_id,
                agent_key=start.agent_key, agent_revision=start.agent_revision,
                run_state="RUNNING", risk_tier=start.risk_tier,
                policy_outcome=start.policy_outcome,
                created_at=previous_snapshot.created_at if previous_snapshot else now,
                generation=lease.generation, lease_expires_at=now + timedelta(minutes=2),
                current_stage=RunStageKey.AUTHORIZING,
                progress_percent=0,
                audit_id=start.audit_id,
                audit_record_id=record_id,
                audit_link_state="PENDING" if record_id else None,
            )
            self._stages[(lease.run_id, lease.generation, RunStageKey.AUTHORIZING)] = (
                RunStageSnapshot(
                    key=RunStageKey.AUTHORIZING,
                    state=RunStageState.ACTIVE,
                    sequence=stage_sequence(RunStageKey.AUTHORIZING),
                    started_at=now,
                )
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

    def run_stages(
        self, *, tenant_id: str, user_id: str, run_id: UUID, generation: int
    ) -> tuple[RunStageSnapshot, ...]:
        with self._lock:
            snapshot = self._activity.get(str(run_id))
            if snapshot is None or (snapshot.tenant_id, snapshot.user_id) != (tenant_id, user_id):
                return ()
            rows = [row for (candidate, attempt, _), row in self._stages.items()
                    if candidate == str(run_id) and attempt == generation]
            return tuple(sorted(rows, key=lambda row: row.sequence))

    def run_source_health(
        self, *, tenant_id: str, user_id: str, run_id: UUID, generation: int
    ) -> tuple[RunSourceHealthSnapshot, ...]:
        with self._lock:
            snapshot = self._activity.get(str(run_id))
            if snapshot is None or (snapshot.tenant_id, snapshot.user_id) != (tenant_id, user_id):
                return ()
            rows = [row for (candidate, attempt, _), row in self._source_health.items()
                    if candidate == str(run_id) and attempt == generation]
            return tuple(sorted(rows, key=lambda row: row.source_type))

    def advance_stage(
        self, lease: RunLease, *, tenant_id: str, user_id: str,
        request_id: str, stage: RunStageKey,
    ) -> None:
        key = (tenant_id, user_id, request_id)
        with self._lock:
            if (self._run_keys.get(lease.run_id) != key or self._pending.get(key) != lease
                    or not self._lease_active(lease, self._clock())):
                raise RunStoreUnavailable("Agent run lease is no longer owned.")
            self._advance_stage_locked(lease, stage, self._clock())

    def record_source_health(
        self, lease: RunLease, *, tenant_id: str, user_id: str,
        request_id: str, observations: tuple[SourceHealthObservation, ...],
    ) -> None:
        key = (tenant_id, user_id, request_id)
        with self._lock:
            if (self._run_keys.get(lease.run_id) != key or self._pending.get(key) != lease
                    or not self._lease_active(lease, self._clock())):
                raise RunStoreUnavailable("Agent run lease is no longer owned.")
            for observation in observations:
                source_key = (lease.run_id, lease.generation, observation.source_type)
                self._source_health.setdefault(
                    source_key,
                    RunSourceHealthSnapshot(
                        source_type=observation.source_type,
                        status=observation.status,
                        latency_ms=observation.latency_ms,
                        last_attempt_at=observation.observed_at,
                        last_success_at=(observation.observed_at
                                         if observation.status == "SUCCESS" else None),
                    ),
                )

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
            snapshot = self._activity[lease.run_id]
            if snapshot.audit_id is not None and snapshot.audit_id != response.audit_id:
                raise RunStoreUnavailable("Agent response audit identity does not match the run.")
            self._responses[key] = response
            terminal_at = self._clock()
            self._terminal_stage_locked(lease, RunStageKey.COMPLETED, terminal_at)
            self._activity[lease.run_id] = replace(
                snapshot, run_state="COMPLETED",
                answer_state=str(response.state), status_code=response.status_code,
                source_count=response.source_count, latency_ms=response.model_route.latency_ms,
                completed_at=response.completed_at, lease_expires_at=None,
                current_stage=RunStageKey.COMPLETED,
                progress_percent=progress_percent(self._current_stages_locked(lease)),
                audit_id=response.audit_id, audit_record_id=audit_record_id(response.audit_id),
                audit_link_state="PENDING",
            )
            self._completed_leases[lease.run_id] = lease
            self._pending.pop(key, None)
            self._run_keys.pop(lease.run_id, None)

    def fail(self, lease: RunLease, safe_error_code: str) -> None:
        with self._lock:
            key = self._run_keys.get(lease.run_id)
            if (key is not None and self._pending.get(key) == lease
                    and self._lease_active(lease, self._clock())):
                terminal_at = self._clock()
                self._terminal_stage_locked(lease, RunStageKey.FAILED, terminal_at)
                self._activity[lease.run_id] = replace(
                    self._activity[lease.run_id], run_state="FAILED",
                    completed_at=terminal_at, lease_expires_at=None,
                    current_stage=RunStageKey.FAILED,
                    progress_percent=progress_percent(self._current_stages_locked(lease)),
                )
                self._run_keys.pop(lease.run_id, None)
                self._pending.pop(key, None)

    def _advance_stage_locked(
        self, lease: RunLease, stage: RunStageKey, occurred_at: datetime,
    ) -> None:
        rows = [row for (run_id, generation, _), row in self._stages.items()
                if run_id == lease.run_id and generation == lease.generation]
        latest = max(rows, key=lambda row: row.sequence)
        desired = stage_sequence(stage)
        if desired < latest.sequence or (desired == latest.sequence and stage != latest.key):
            raise RunStoreUnavailable("Agent run stage cannot move backwards.")
        if stage == latest.key:
            return
        self._stages[(lease.run_id, lease.generation, latest.key)] = replace(
            latest, state=RunStageState.COMPLETED, completed_at=occurred_at
        )
        existing_keys = {row.key for row in rows}
        for skipped in OPERATIONAL_STAGES:
            skipped_sequence = stage_sequence(skipped)
            if latest.sequence < skipped_sequence < desired and skipped not in existing_keys:
                self._stages[(lease.run_id, lease.generation, skipped)] = RunStageSnapshot(
                    key=skipped,
                    state=RunStageState.SKIPPED,
                    sequence=skipped_sequence,
                    started_at=occurred_at,
                    completed_at=occurred_at,
                )
        self._stages[(lease.run_id, lease.generation, stage)] = RunStageSnapshot(
            key=stage, state=RunStageState.ACTIVE, sequence=desired, started_at=occurred_at
        )
        self._activity[lease.run_id] = replace(
            self._activity[lease.run_id], current_stage=stage,
            progress_percent=progress_percent(self._current_stages_locked(lease)),
        )

    def _terminal_stage_locked(
        self, lease: RunLease, stage: RunStageKey, occurred_at: datetime,
    ) -> None:
        rows = [row for (run_id, generation, _), row in self._stages.items()
                if run_id == lease.run_id and generation == lease.generation]
        latest = max(rows, key=lambda row: row.sequence)
        if latest.state == RunStageState.ACTIVE:
            final_state = (RunStageState.COMPLETED if stage == RunStageKey.COMPLETED
                           else RunStageState.FAILED)
            self._stages[(lease.run_id, lease.generation, latest.key)] = replace(
                latest, state=final_state, completed_at=occurred_at
            )
        if stage == RunStageKey.COMPLETED:
            existing_keys = {row.key for row in rows}
            for skipped in OPERATIONAL_STAGES:
                if skipped not in existing_keys:
                    self._stages[(lease.run_id, lease.generation, skipped)] = RunStageSnapshot(
                        key=skipped,
                        state=RunStageState.SKIPPED,
                        sequence=stage_sequence(skipped),
                        started_at=occurred_at,
                        completed_at=occurred_at,
                    )
        self._stages[(lease.run_id, lease.generation, stage)] = RunStageSnapshot(
            key=stage,
            state=(RunStageState.COMPLETED if stage == RunStageKey.COMPLETED
                   else RunStageState.FAILED),
            sequence=stage_sequence(stage),
            started_at=occurred_at,
            completed_at=occurred_at,
        )

    def _current_stages_locked(self, lease: RunLease) -> tuple[RunStageSnapshot, ...]:
        rows = [row for (run_id, generation, _), row in self._stages.items()
                if run_id == lease.run_id and generation == lease.generation]
        return tuple(sorted(rows, key=lambda row: row.sequence))
