from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal
from uuid import uuid4


LOGGER = logging.getLogger("uvicorn.error.dwp.audit.delivery")

_DeliveryDisposition = Literal["DELIVERED", "PERMANENT_FAILURE", "RETRYABLE_FAILURE"]


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
        recovery_succeeded = self._recover_staged_quarantines()
        files = sorted(self.spool_dir.glob("*.json"))[: self.batch_size]
        if not files:
            return recovery_succeeded

        readable_entries: list[tuple[Path, object]] = []
        local_handling_succeeded = recovery_succeeded
        for path in files:
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                quarantined = self._quarantine(
                    path,
                    reason="INVALID_JSON",
                )
                local_handling_succeeded = quarantined and local_handling_succeeded
            except OSError as error:
                LOGGER.warning(
                    "Audit spool read deferred; file=%r error=%s",
                    path.name,
                    type(error).__name__,
                )
                local_handling_succeeded = False
            else:
                readable_entries.append((path, self._normalize_legacy_outcome(event)))

        delivery_succeeded = self._deliver_entries(readable_entries)
        return local_handling_succeeded and delivery_succeeded

    def _deliver_entries(self, entries: list[tuple[Path, object]]) -> bool:
        if not entries:
            return True

        disposition, status = self._send_events([event for _, event in entries])
        if disposition == "DELIVERED":
            deleted_all = True
            for path, _ in entries:
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    LOGGER.warning(
                        "Delivered audit spool cleanup deferred; file=%r error=%s",
                        path.name,
                        type(error).__name__,
                    )
                    deleted_all = False
            return deleted_all

        if disposition == "RETRYABLE_FAILURE":
            return False

        if len(entries) == 1:
            path, _ = entries[0]
            return self._quarantine(
                path,
                reason="COLLECTOR_PERMANENT_4XX",
                collector_status=status,
            )

        midpoint = len(entries) // 2
        left_succeeded = self._deliver_entries(entries[:midpoint])
        right_succeeded = self._deliver_entries(entries[midpoint:])
        return left_succeeded and right_succeeded

    def _send_events(self, events: list[object]) -> tuple[_DeliveryDisposition, int | None]:
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
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                status = int(response.status)
        except urllib.error.HTTPError as error:
            status = int(error.code)
        except (OSError, urllib.error.URLError) as error:
            LOGGER.warning(
                "Audit collector delivery deferred; error=%s",
                type(error).__name__,
            )
            return "RETRYABLE_FAILURE", None

        if 200 <= status < 300:
            return "DELIVERED", status
        # Only payload-specific responses are safe to isolate. Authentication,
        # routing and rate-limit failures apply to the collector as a whole and
        # must retain every event for an operator/configuration retry.
        if status in {400, 413, 422}:
            return "PERMANENT_FAILURE", status

        LOGGER.warning("Audit collector delivery deferred; status=%s", status)
        return "RETRYABLE_FAILURE", status

    def _quarantine(
        self,
        path: Path,
        *,
        reason: str,
        collector_status: int | None = None,
    ) -> bool:
        quarantine_root = self.spool_dir / "quarantine"
        quarantine_id = str(uuid4())
        staging_dir = quarantine_root / f".staging-{quarantine_id}"
        final_dir = quarantine_root / quarantine_id
        original_dir = staging_dir / "original"
        manifest_path = staging_dir / "manifest.json"
        manifest_temporary = staging_dir / "manifest.tmp"
        manifest: dict[str, object] = {
            "schemaVersion": 1,
            "quarantineId": quarantine_id,
            "quarantinedAtUnixMs": int(time.time() * 1000),
            "originalFileName": path.name,
            "reason": reason,
        }
        if collector_status is not None:
            manifest["collectorStatus"] = collector_status

        try:
            original_dir.mkdir(parents=True, exist_ok=False)
            with manifest_temporary.open("x", encoding="utf-8") as file:
                json.dump(manifest, file, ensure_ascii=True, separators=(",", ":"))
                file.flush()
                os.fsync(file.fileno())
            os.replace(manifest_temporary, manifest_path)
            self._fsync_directory(staging_dir)
            os.replace(path, original_dir / path.name)
            self._fsync_directory(original_dir)
            self._fsync_directory(self.spool_dir)
            os.replace(staging_dir, final_dir)
            self._fsync_directory(quarantine_root)
        except OSError as error:
            LOGGER.critical(
                "Audit quarantine operation requires recovery; quarantine_id=%s "
                "reason=%s error=%s",
                quarantine_id,
                reason,
                type(error).__name__,
            )
            if path.exists():
                if not self._discard_incomplete_staging(staging_dir):
                    LOGGER.critical(
                        "Incomplete audit quarantine staging cleanup failed; staging=%r",
                        staging_dir.name,
                    )
            return False

        LOGGER.critical(
            "Audit event quarantined; quarantine_id=%s reason=%s collector_status=%s",
            quarantine_id,
            reason,
            collector_status,
        )
        return True

    def _recover_staged_quarantines(self) -> bool:
        quarantine_root = self.spool_dir / "quarantine"
        if not quarantine_root.exists():
            return True

        recovered_all = True
        for staging_dir in sorted(quarantine_root.glob(".staging-*")):
            if not staging_dir.is_dir():
                continue
            manifest_path = staging_dir / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    LOGGER.critical(
                        "Staged audit quarantine has an invalid manifest; staging=%r",
                        staging_dir.name,
                    )
                    recovered_all = False
                    continue
                original_name = manifest.get("originalFileName")
                if (
                    not isinstance(original_name, str)
                    or not original_name
                    or original_name in {".", ".."}
                    or Path(original_name).name != original_name
                ):
                    LOGGER.critical(
                        "Staged audit quarantine has an unsafe original file name; staging=%r",
                        staging_dir.name,
                    )
                    recovered_all = False
                    continue
                original_path = staging_dir / "original" / original_name
                if not original_path.is_file():
                    active_path = self.spool_dir / original_name
                    if active_path.is_file():
                        if self._discard_incomplete_staging(staging_dir):
                            LOGGER.warning(
                                "Discarded incomplete audit quarantine staging; staging=%r",
                                staging_dir.name,
                            )
                        else:
                            LOGGER.critical(
                                "Incomplete audit quarantine staging cleanup failed; staging=%r",
                                staging_dir.name,
                            )
                            recovered_all = False
                        continue
                    LOGGER.critical(
                        "Staged audit quarantine is missing its original event; staging=%r",
                        staging_dir.name,
                    )
                    recovered_all = False
                    continue
                quarantine_id = staging_dir.name.removeprefix(".staging-")
                final_dir = quarantine_root / quarantine_id
                if final_dir.exists():
                    LOGGER.critical(
                        "Staged audit quarantine destination already exists; staging=%r",
                        staging_dir.name,
                    )
                    recovered_all = False
                    continue
                os.replace(staging_dir, final_dir)
                self._fsync_directory(quarantine_root)
                reason = manifest.get("reason")
                safe_reason = (
                    reason
                    if isinstance(reason, str)
                    and reason in {"INVALID_JSON", "COLLECTOR_PERMANENT_4XX"}
                    else "UNKNOWN"
                )
                LOGGER.critical(
                    "Recovered staged audit quarantine; quarantine_id=%s reason=%s",
                    quarantine_id,
                    safe_reason,
                )
            except (OSError, ValueError) as error:
                LOGGER.critical(
                    "Staged audit quarantine recovery deferred; staging=%r error=%s",
                    staging_dir.name,
                    type(error).__name__,
                )
                recovered_all = False
        return recovered_all

    @staticmethod
    def _discard_incomplete_staging(staging_dir: Path) -> bool:
        try:
            (staging_dir / "manifest.tmp").unlink(missing_ok=True)
            (staging_dir / "manifest.json").unlink(missing_ok=True)
            (staging_dir / "original").rmdir()
            staging_dir.rmdir()
        except OSError:
            return False
        return True

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

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
