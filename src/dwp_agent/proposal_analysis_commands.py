from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol, TypeVar
from uuid import UUID, uuid4

from .proposal_analysis_contracts import ProposalAnalysisReceipt
from .proposal_analysis_fingerprints import ProposalAnalysisFingerprints


ANALYSIS_PURPOSE = "PROACTIVE_WORK_ANALYSIS_V1"
MAX_ANALYSES_PER_MINUTE = 2
MAX_ANALYSES_PER_DAY = 100
MAX_COMMAND_ATTEMPTS = 3
LEASE_TTL = timedelta(minutes=2)
_Result = TypeVar("_Result")


class ProposalAnalysisCommandError(RuntimeError):
    pass


class ProposalAnalysisCommandUnavailable(ProposalAnalysisCommandError):
    pass


class ProposalAnalysisInProgress(ProposalAnalysisCommandError):
    pass


class ProposalAnalysisRateLimited(ProposalAnalysisCommandError):
    pass


class ProposalAnalysisReplay(ProposalAnalysisCommandError):
    pass


@dataclass(frozen=True)
class ProposalAnalysisLease:
    command_id: UUID
    generation: int
    token: UUID


@dataclass(frozen=True)
class ProposalAnalysisStart:
    lease: ProposalAnalysisLease | None = None
    replay: ProposalAnalysisReceipt | None = None


class ProposalAnalysisCommandStore(Protocol):
    def begin(
        self,
        *,
        tenant_id: str,
        user_id: str,
        auth_session_id: str,
        command_id: UUID,
        locale: str,
    ) -> ProposalAnalysisStart: ...

    def complete(
        self,
        *,
        tenant_id: str,
        user_id: str,
        lease: ProposalAnalysisLease,
        receipt: ProposalAnalysisReceipt,
    ) -> ProposalAnalysisReceipt: ...

    def fail(
        self,
        *,
        tenant_id: str,
        user_id: str,
        lease: ProposalAnalysisLease,
    ) -> None: ...

    def clear_for_user(self, *, tenant_id: str, user_id: str) -> None: ...

    def run_under_lease(
        self,
        *,
        tenant_id: str,
        user_id: str,
        lease: ProposalAnalysisLease,
        operation: Callable[[], _Result],
    ) -> _Result: ...


@dataclass
class _MemoryCommand:
    session_fingerprint: str
    request_fingerprint: str
    status: str
    generation: int
    lease_token: UUID | None
    attempt_count: int
    created_at: datetime
    started_at: datetime
    receipt: ProposalAnalysisReceipt | None = None


class InMemoryProposalAnalysisCommandStore:
    def __init__(
        self, fingerprints: ProposalAnalysisFingerprints | None = None
    ) -> None:
        self._fingerprints = fingerprints or ProposalAnalysisFingerprints.ephemeral()
        self._lock = threading.Lock()
        self._commands: dict[tuple[str, str, UUID], _MemoryCommand] = {}

    def begin(
        self,
        *,
        tenant_id: str,
        user_id: str,
        auth_session_id: str,
        command_id: UUID,
        locale: str,
    ) -> ProposalAnalysisStart:
        now = datetime.now(timezone.utc)
        request_fingerprint = self._fingerprints.analysis_command(
            tenant_id, locale=locale
        )
        session_fingerprint = self._fingerprints.session(
            tenant_id, auth_session_id
        )
        key = (tenant_id, user_id, command_id)
        with self._lock:
            record = self._commands.get(key)
            if record is not None:
                self._validate_replay(
                    record,
                    tenant_id=tenant_id,
                    auth_session_id=auth_session_id,
                    locale=locale,
                )
                if record.status == "COMPLETED" and record.receipt is not None:
                    return ProposalAnalysisStart(replay=record.receipt)
                if record.status == "RUNNING" and record.started_at > now - LEASE_TTL:
                    raise ProposalAnalysisInProgress(
                        "The analysis command is already running."
                    )
                if record.attempt_count >= MAX_COMMAND_ATTEMPTS:
                    raise ProposalAnalysisRateLimited(
                        "The analysis command retry limit was reached."
                    )
                record.status = "RUNNING"
                record.generation += 1
                record.lease_token = uuid4()
                record.attempt_count += 1
                record.started_at = now
                record.receipt = None
                return ProposalAnalysisStart(
                    lease=ProposalAnalysisLease(
                        command_id, record.generation, record.lease_token
                    )
                )
            recent = [
                item.created_at
                for (tenant, user, _), item in self._commands.items()
                if tenant == tenant_id and user == user_id
            ]
            _enforce_rate(recent, now)
            token = uuid4()
            self._commands[key] = _MemoryCommand(
                session_fingerprint=session_fingerprint,
                request_fingerprint=request_fingerprint,
                status="RUNNING",
                generation=1,
                lease_token=token,
                attempt_count=1,
                created_at=now,
                started_at=now,
            )
            return ProposalAnalysisStart(
                lease=ProposalAnalysisLease(command_id, 1, token)
            )

    def complete(
        self,
        *,
        tenant_id: str,
        user_id: str,
        lease: ProposalAnalysisLease,
        receipt: ProposalAnalysisReceipt,
    ) -> ProposalAnalysisReceipt:
        with self._lock:
            record = self._require_current(tenant_id, user_id, lease)
            record.status = "COMPLETED"
            record.lease_token = None
            record.receipt = receipt
            return receipt

    def fail(
        self,
        *,
        tenant_id: str,
        user_id: str,
        lease: ProposalAnalysisLease,
    ) -> None:
        with self._lock:
            try:
                record = self._require_current(tenant_id, user_id, lease)
            except ProposalAnalysisReplay:
                return
            record.status = "FAILED"
            record.lease_token = None
            record.receipt = None

    def clear_for_user(self, *, tenant_id: str, user_id: str) -> None:
        with self._lock:
            for (tenant, user, _), record in self._commands.items():
                if tenant != tenant_id or user != user_id:
                    continue
                record.status = "FAILED"
                record.generation += 1
                record.lease_token = None
                record.receipt = None

    def run_under_lease(
        self,
        *,
        tenant_id: str,
        user_id: str,
        lease: ProposalAnalysisLease,
        operation: Callable[[], _Result],
    ) -> _Result:
        with self._lock:
            self._require_current(tenant_id, user_id, lease)
            return operation()

    def _validate_replay(
        self,
        record: _MemoryCommand,
        *,
        tenant_id: str,
        auth_session_id: str,
        locale: str,
    ) -> None:
        if not self._fingerprints.matches_session(
            record.session_fingerprint, tenant_id, auth_session_id
        ):
            raise ProposalAnalysisReplay(
                "The analysis command belongs to another session."
            )
        if not self._fingerprints.matches_analysis_command(
            record.request_fingerprint, tenant_id, locale=locale
        ):
            raise ProposalAnalysisReplay(
                "The analysis command payload changed."
            )

    def _require_current(
        self, tenant_id: str, user_id: str, lease: ProposalAnalysisLease
    ) -> _MemoryCommand:
        record = self._commands.get((tenant_id, user_id, lease.command_id))
        if (
            record is None
            or record.status != "RUNNING"
            or record.generation != lease.generation
            or record.lease_token != lease.token
        ):
            raise ProposalAnalysisReplay(
                "The analysis command lease is no longer current."
            )
        return record


def _enforce_rate(values: list[datetime], now: datetime) -> None:
    minute = sum(value >= now - timedelta(minutes=1) for value in values)
    day = sum(value >= now - timedelta(days=1) for value in values)
    if minute >= MAX_ANALYSES_PER_MINUTE or day >= MAX_ANALYSES_PER_DAY:
        raise ProposalAnalysisRateLimited("Analysis request capacity exceeded.")


_COMMAND_STORE: ProposalAnalysisCommandStore | None = None
_COMMAND_STORE_LOCK = threading.Lock()


def get_proposal_analysis_command_store() -> ProposalAnalysisCommandStore:
    global _COMMAND_STORE
    with _COMMAND_STORE_LOCK:
        if _COMMAND_STORE is not None:
            return _COMMAND_STORE
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            raise ProposalAnalysisCommandUnavailable(
                "Proposal analysis commands require the configured Agent database."
            )
        from .proposal_analysis_postgres_commands import (
            PostgresProposalAnalysisCommandStore,
        )

        _COMMAND_STORE = PostgresProposalAnalysisCommandStore(database_url)
        return _COMMAND_STORE


def set_proposal_analysis_command_store_for_tests(
    store: ProposalAnalysisCommandStore | None,
) -> None:
    global _COMMAND_STORE
    with _COMMAND_STORE_LOCK:
        _COMMAND_STORE = store
