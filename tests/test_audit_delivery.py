from __future__ import annotations

import json
from pathlib import Path

import dwp_agent.audit as audit_module
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


def test_record_ask_failure_publishes_canonical_failed_outcome(monkeypatch) -> None:
    published: list[dict[str, object]] = []

    class Publisher:
        enabled = True

        @staticmethod
        def publish(event: dict[str, object]) -> None:
            published.append(event)

    monkeypatch.setattr(audit_module, "AUDIT_PUBLISHER", Publisher())

    audit_module.record_ask_failure(
        run_id="run-failed-1",
        audit_id="audit-failed-1",
        tenant_id="85",
        user_id="user-1",
        correlation_id="correlation-1",
        agent_key="work-assistant",
        agent_revision=7,
        risk_tier="L2",
        roles=["USER", " USER ", ""],
    )

    assert len(published) == 1
    assert published[0]["action"] == "agent.ask.failed"
    assert published[0]["outcome"] == "FAILED"
    assert published[0]["actorRoles"] == ["USER"]
    assert published[0]["metadata"] == {
        "userId": "user-1",
        "state": "FAILED",
        "statusCode": "ASK_RUNTIME_FAILED",
        "riskTier": "L2",
        "agentKey": "work-assistant",
        "agentRevision": 7,
    }


def test_flush_normalizes_only_legacy_top_level_outcome_and_deletes_after_success(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DWP_AUDIT_COLLECTOR_URL", "http://audit.test/internal/events")
    monkeypatch.setenv("DWP_AUDIT_INGEST_TOKEN", "test-token")
    monkeypatch.setenv("DWP_AUDIT_SPOOL_DIR", str(tmp_path))
    publisher = DurableAuditPublisher()
    legacy_file = tmp_path / "001-legacy.json"
    canonical_file = tmp_path / "002-canonical.json"
    legacy_file.write_text(
        json.dumps(
            {
                "eventId": "legacy-event",
                "outcome": "FAILURE",
                "metadata": {"outcome": "FAILURE"},
                "description": "FAILURE",
            }
        ),
        encoding="utf-8",
    )
    canonical_file.write_text(
        json.dumps({"eventId": "canonical-event", "outcome": "SUCCESS"}),
        encoding="utf-8",
    )
    requests: list[object] = []

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def urlopen(request, timeout):
        requests.append(request)
        assert timeout == 3
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    assert publisher.flush_once() is True

    assert len(requests) == 1
    delivered = json.loads(requests[0].data.decode("utf-8"))
    assert delivered == [
        {
            "eventId": "legacy-event",
            "outcome": "FAILED",
            "metadata": {"outcome": "FAILURE"},
            "description": "FAILURE",
        },
        {"eventId": "canonical-event", "outcome": "SUCCESS"},
    ]
    assert not list(tmp_path.glob("*.json"))


def test_failed_flush_preserves_original_legacy_spool_file(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DWP_AUDIT_COLLECTOR_URL", "http://audit.test/internal/events")
    monkeypatch.setenv("DWP_AUDIT_INGEST_TOKEN", "test-token")
    monkeypatch.setenv("DWP_AUDIT_SPOOL_DIR", str(tmp_path))
    publisher = DurableAuditPublisher()
    legacy_event = {
        "eventId": "legacy-event",
        "outcome": "FAILURE",
        "metadata": {"outcome": "FAILURE"},
    }
    legacy_file = tmp_path / "001-legacy.json"
    legacy_file.write_text(json.dumps(legacy_event), encoding="utf-8")

    class Response:
        status = 422

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())

    assert publisher.flush_once() is False
    assert json.loads(legacy_file.read_text(encoding="utf-8")) == legacy_event
