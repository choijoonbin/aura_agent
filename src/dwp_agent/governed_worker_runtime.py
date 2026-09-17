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


def register_governed_worker_heartbeat(*worker_types: str) -> None:
    """Publish liveness for capability responses without exposing heartbeat storage."""
    _heartbeat(*worker_types)


def remove_governed_worker_heartbeat(*worker_types: str) -> None:
    _remove_heartbeats(*worker_types)


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
    worker_types = (
        "ARTIFACT_EXPORT",
        "DATA_DELETION",
        "RESEARCH_DELIVERY",
        "RESEARCH_RUN",
        "ADMIN_CONTROL_COMMAND",
        "SECURE_ATTACHMENT_PIPELINE",
    )
    topics = (
        "ai.artifact.export-requested.v1",
        "ai.personal-data.deletion-requested.v1",
        "ai.research.delivery-requested.v1",
        "RESEARCH_DELIVERY",
        "ADMIN_CONTROL_COMMAND",
        "ai.secure-attachment.processing-requested.v1",
    )

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._research_turn = True

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
    def _build_services(
        database_url: str,
    ) -> tuple[object, object, object, object, object, object, object]:
        from .admin_control_plane_executor import PostgresAdminControlCommandExecutor
        from .artifact_export_worker import PostgresArtifactExportWorker
        from .personal_data_deletion_worker import PostgresPersonalDataDeletionWorker
        from .research_delivery_worker import PostgresResearchDeliveryWorker
        from .research_run_worker import PostgresResearchRunWorker
        from .secure_attachment_worker import PostgresSecureAttachmentWorker
        from .transactional_outbox import PostgresTransactionalOutboxStore

        return (
            PostgresTransactionalOutboxStore(database_url),
            PostgresArtifactExportWorker(database_url),
            PostgresPersonalDataDeletionWorker(database_url),
            PostgresResearchDeliveryWorker(database_url),
            PostgresResearchRunWorker(database_url),
            PostgresAdminControlCommandExecutor(database_url),
            PostgresSecureAttachmentWorker(database_url),
        )

    def _process_once(
        self,
        outbox: object,
        export_worker: object,
        deletion_worker: object,
        research_delivery_worker: object,
        research_run_worker: object,
        admin_control_worker: object,
        secure_attachment_worker: object,
    ) -> bool:
        research_enabled = (
            os.getenv("DWP_DEEP_RESEARCH_WORKER_ENABLED", "false")
            .strip()
            .lower()
            == "true"
        )
        tried_research = research_enabled and self._research_turn
        self._research_turn = not self._research_turn
        if tried_research and research_run_worker.process_once():
            return True
        lease = outbox.claim_any(topics=self.topics, lease_seconds=300)
        if lease is None:
            return bool(
                research_enabled
                and not tried_research
                and research_run_worker.process_once()
            )
        try:
            if lease.topic == "ai.artifact.export-requested.v1":
                export_worker.process(lease)
            elif lease.topic == "ai.personal-data.deletion-requested.v1":
                deletion_worker.process(lease)
            elif lease.topic in {
                "ai.research.delivery-requested.v1",
                "RESEARCH_DELIVERY",
            }:
                research_delivery_worker.process(lease)
            elif lease.topic == "ADMIN_CONTROL_COMMAND":
                admin_control_worker.process(lease)
            elif lease.topic == "ai.secure-attachment.processing-requested.v1":
                secure_attachment_worker.process(lease)
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
                retry_state = outbox.retry(
                    lease,
                    safe_error_code="GOVERNED_WORKER_RETRY",
                    retry_after_seconds=5,
                )
                if (
                    retry_state == "DEAD_LETTER"
                    and lease.topic
                    in {"ai.research.delivery-requested.v1", "RESEARCH_DELIVERY"}
                ):
                    research_delivery_worker.mark_retry_exhausted(lease)
                elif retry_state == "DEAD_LETTER" and lease.topic == "ADMIN_CONTROL_COMMAND":
                    admin_control_worker.fail_dead_letter(
                        lease, safe_error_code="ADMIN_EXECUTION_RETRY_EXHAUSTED"
                    )
                elif (
                    retry_state == "DEAD_LETTER"
                    and lease.topic == "ai.secure-attachment.processing-requested.v1"
                ):
                    secure_attachment_worker.mark_retry_exhausted(lease)
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
                          AND to_regclass('ai_admin_control_resource_versions') IS NOT NULL
                          AND to_regclass('ai_secure_attachments') IS NOT NULL
                          AND EXISTS (
                              SELECT 1 FROM sys_schema_history
                               WHERE version = 'V70'
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
