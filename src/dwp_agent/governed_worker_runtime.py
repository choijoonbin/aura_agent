from __future__ import annotations

import logging
import os
import threading
import time

from psycopg import connect


LOGGER = logging.getLogger(__name__)
_WORKER_HEARTBEATS: dict[str, float] = {}
_HEARTBEAT_LOCK = threading.Lock()
_HEARTBEAT_TTL_SECONDS = 10.0


def governed_worker_available(worker_type: str) -> bool:
    with _HEARTBEAT_LOCK:
        heartbeat = _WORKER_HEARTBEATS.get(worker_type)
    return heartbeat is not None and time.monotonic() - heartbeat <= _HEARTBEAT_TTL_SECONDS


def _heartbeat(*worker_types: str) -> None:
    now = time.monotonic()
    with _HEARTBEAT_LOCK:
        for worker_type in worker_types:
            _WORKER_HEARTBEATS[worker_type] = now


def _remove_heartbeats(*worker_types: str) -> None:
    with _HEARTBEAT_LOCK:
        for worker_type in worker_types:
            _WORKER_HEARTBEATS.pop(worker_type, None)


class GovernedWorkerMaintenance:
    worker_types = ("ARTIFACT_EXPORT", "DATA_DELETION")
    topics = (
        "ai.artifact.export-requested.v1",
        "ai.personal-data.deletion-requested.v1",
    )

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None or not _workers_enabled():
            return
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            return
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(database_url,),
            name="dwp-agent-governed-workers",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=1)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        _remove_heartbeats(*self.worker_types)
        self._ready.clear()

    def process_once(self, database_url: str) -> bool:
        return self._process_once(*self._build_services(database_url))

    @staticmethod
    def _build_services(database_url: str) -> tuple[object, object, object]:
        from .artifact_export_worker import PostgresArtifactExportWorker
        from .personal_data_deletion_worker import PostgresPersonalDataDeletionWorker
        from .transactional_outbox import PostgresTransactionalOutboxStore

        return (
            PostgresTransactionalOutboxStore(database_url),
            PostgresArtifactExportWorker(database_url),
            PostgresPersonalDataDeletionWorker(database_url),
        )

    def _process_once(
        self, outbox: object, export_worker: object, deletion_worker: object
    ) -> bool:
        lease = outbox.claim_any(topics=self.topics, lease_seconds=300)
        if lease is None:
            return False
        try:
            if lease.topic == "ai.artifact.export-requested.v1":
                export_worker.process(lease)
            elif lease.topic == "ai.personal-data.deletion-requested.v1":
                deletion_worker.process(lease)
            else:
                raise ValueError("The governed worker topic is unsupported.")
            outbox.acknowledge(lease)
        except Exception as error:
            LOGGER.warning(
                "Governed worker intent failed; topic=%s error=%s",
                lease.topic,
                type(error).__name__,
            )
            try:
                outbox.retry(
                    lease,
                    safe_error_code="GOVERNED_WORKER_RETRY",
                    retry_after_seconds=5,
                )
            except Exception as retry_error:
                LOGGER.warning(
                    "Governed worker retry bookkeeping failed; error=%s",
                    type(retry_error).__name__,
                )
        return True

    def _run(self, database_url: str) -> None:
        try:
            services = self._build_services(database_url)
            self._require_database_schema(database_url)
            _heartbeat(*self.worker_types)
            self._ready.set()
            interval = _worker_interval_seconds()
            while not self._stop.is_set():
                _heartbeat(*self.worker_types)
                processed = self._process_once(*services)
                if not processed:
                    self._stop.wait(interval)
        except Exception as error:
            LOGGER.error(
                "Governed worker runtime stopped; error=%s",
                type(error).__name__,
            )
        finally:
            self._ready.set()
            _remove_heartbeats(*self.worker_types)

    @staticmethod
    def _require_database_schema(database_url: str) -> None:
        with connect(database_url) as connection:
            row = connection.execute(
                """SELECT to_regclass('ai_transactional_outbox') IS NOT NULL
                          AND to_regclass('ai_artifact_export_outputs') IS NOT NULL
                          AND to_regclass('ai_data_disposition_receipts') IS NOT NULL
                          AND EXISTS (
                              SELECT 1 FROM sys_schema_history
                               WHERE version = 'V34'
                          )"""
            ).fetchone()
        if row != (True,):
            raise RuntimeError("Governed worker database schema is unavailable.")


def _workers_enabled() -> bool:
    return os.getenv("DWP_GOVERNED_WORKERS_ENABLED", "false").strip().lower() == "true"


def _worker_interval_seconds() -> float:
    raw = os.getenv("DWP_GOVERNED_WORKER_INTERVAL_SECONDS", "1").strip()
    try:
        configured = float(raw)
    except ValueError:
        configured = 1.0
    return max(0.1, min(30.0, configured))


MAINTENANCE = GovernedWorkerMaintenance()
