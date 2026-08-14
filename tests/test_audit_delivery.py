from __future__ import annotations

import json
from pathlib import Path

from dwp_agent.audit_delivery import DurableAuditPublisher


def test_audit_publisher_spools_atomically(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DWP_AUDIT_COLLECTOR_URL", "http://127.0.0.1:9/internal/audit/events")
    monkeypatch.setenv("DWP_AUDIT_INGEST_TOKEN", "test-token")
    monkeypatch.setenv("DWP_AUDIT_SPOOL_DIR", str(tmp_path))
    publisher = DurableAuditPublisher()
    monkeypatch.setattr(publisher, "_start_worker", lambda: None)

    publisher.publish({"eventId": "c7d94d27-b740-4a77-b88f-a1116196910f"})

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(files[0].read_text(encoding="utf-8"))["eventId"].startswith("c7d")


def test_audit_publisher_is_noop_without_collector(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DWP_AUDIT_COLLECTOR_URL", raising=False)
    monkeypatch.delenv("DWP_AUDIT_INGEST_TOKEN", raising=False)
    monkeypatch.setenv("DWP_AUDIT_SPOOL_DIR", str(tmp_path))
    publisher = DurableAuditPublisher()

    publisher.publish({"eventId": "not-delivered"})

    assert not list(tmp_path.glob("*.json"))
