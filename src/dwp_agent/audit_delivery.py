from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4


LOGGER = logging.getLogger("uvicorn.error.dwp.audit.delivery")


class DurableAuditPublisher:
    """Disk-backed audit delivery used when the central collector is unavailable."""

    def __init__(self) -> None:
        self.collector_url = os.getenv("DWP_AUDIT_COLLECTOR_URL", "").strip()
        self.ingest_token = os.getenv("DWP_AUDIT_INGEST_TOKEN", "").strip()
        self.service_name = os.getenv("DWP_AUDIT_SERVICE_NAME", "dwp-agent-runtime").strip()
        self.spool_dir = Path(
            os.getenv("DWP_AUDIT_SPOOL_DIR", str(Path.home() / ".dwp/audit-spool/agent"))
        ).expanduser()
        self.batch_size = max(1, min(100, int(os.getenv("DWP_AUDIT_BATCH_SIZE", "50"))))
        self.maximum_files = max(100, int(os.getenv("DWP_AUDIT_SPOOL_MAX_FILES", "10000")))
        self._wake = threading.Event()
        self._started = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.collector_url and self.ingest_token)

    def publish(self, event: dict[str, object]) -> None:
        if not self.enabled:
            return
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            files = list(self.spool_dir.glob("*.json"))
            if len(files) >= self.maximum_files:
                LOGGER.critical(
                    "Audit spool capacity reached; operator action required; files=%s", len(files)
                )
                return
            target = self.spool_dir / f"{time.time_ns()}-{uuid4()}.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(event, ensure_ascii=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, target)
            self._start_worker()
        self._wake.set()

    def flush_once(self) -> bool:
        if not self.enabled or not self.spool_dir.exists():
            return True
        files = sorted(self.spool_dir.glob("*.json"))[: self.batch_size]
        if not files:
            return True
        try:
            events = [
                self._normalize_legacy_outcome(
                    json.loads(path.read_text(encoding="utf-8"))
                )
                for path in files
            ]
            request = urllib.request.Request(
                self.collector_url,
                data=json.dumps(events, separators=(",", ":")).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-DWP-Audit-Token": self.ingest_token,
                    "X-DWP-Audit-Service": self.service_name,
                },
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                if not 200 <= response.status < 300:
                    return False
            for path in files:
                path.unlink(missing_ok=True)
            return True
        except (OSError, ValueError, urllib.error.URLError) as error:
            LOGGER.warning("Audit collector delivery deferred; error=%s", type(error).__name__)
            return False

    @staticmethod
    def _normalize_legacy_outcome(event: object) -> object:
        if isinstance(event, dict) and event.get("outcome") == "FAILURE":
            return {**event, "outcome": "FAILED"}
        return event

    def _start_worker(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, name="dwp-agent-audit-relay", daemon=True).start()

    def _run(self) -> None:
        while True:
            delivered = self.flush_once()
            self._wake.wait(2 if delivered else 10)
            self._wake.clear()


AUDIT_PUBLISHER = DurableAuditPublisher()
