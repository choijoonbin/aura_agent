from __future__ import annotations

import json
import logging
import os
import urllib.error
from pathlib import Path

import dwp_agent.audit as audit_module
import dwp_agent.audit_delivery as audit_delivery_module
from dwp_agent.audit_delivery import DurableAuditPublisher


class Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


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

    def urlopen(request, timeout):
        requests.append(request)
        assert timeout == 3
        return Response(202)

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


def test_retryable_server_failure_preserves_original_legacy_spool_file(
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

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response(503))

    assert publisher.flush_once() is False
    assert json.loads(legacy_file.read_text(encoding="utf-8")) == legacy_event


def test_permanent_batch_failure_bisects_and_quarantines_only_bad_event(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    monkeypatch.setenv("DWP_AUDIT_COLLECTOR_URL", "http://audit.test/internal/events")
    monkeypatch.setenv("DWP_AUDIT_INGEST_TOKEN", "test-token")
    monkeypatch.setenv("DWP_AUDIT_SPOOL_DIR", str(tmp_path))
    publisher = DurableAuditPublisher()
    events = [
        ("001-good.json", {"eventId": "good-1", "outcome": "SUCCESS"}),
        (
            "002-bad.json",
            {
                "eventId": "bad-event",
                "outcome": "SUCCESS",
                "privatePrompt": "TOP-SECRET-PAYLOAD",
            },
        ),
        ("003-good.json", {"eventId": "good-2", "outcome": "SUCCESS"}),
    ]
    for file_name, event in events:
        (tmp_path / file_name).write_text(json.dumps(event), encoding="utf-8")

    attempts: list[list[str]] = []

    def urlopen(request, timeout):
        assert timeout == 3
        delivered = json.loads(request.data.decode("utf-8"))
        event_ids = [event["eventId"] for event in delivered]
        attempts.append(event_ids)
        if "bad-event" in event_ids:
            raise urllib.error.HTTPError(
                request.full_url,
                422,
                "Unprocessable Entity",
                hdrs=None,
                fp=None,
            )
        return Response(202)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    caplog.set_level(logging.CRITICAL, logger="uvicorn.error.dwp.audit.delivery")

    assert publisher.flush_once() is True

    assert attempts == [
        ["good-1", "bad-event", "good-2"],
        ["good-1"],
        ["bad-event", "good-2"],
        ["bad-event"],
        ["good-2"],
    ]
    assert not list(tmp_path.glob("*.json"))
    quarantines = [
        path for path in (tmp_path / "quarantine").iterdir() if not path.name.startswith(".")
    ]
    assert len(quarantines) == 1
    quarantined_event = quarantines[0] / "original" / "002-bad.json"
    assert json.loads(quarantined_event.read_text(encoding="utf-8")) == events[1][1]
    manifest = json.loads((quarantines[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["reason"] == "COLLECTOR_PERMANENT_4XX"
    assert manifest["collectorStatus"] == 422
    assert manifest["originalFileName"] == "002-bad.json"
    assert "TOP-SECRET-PAYLOAD" not in json.dumps(manifest)
    assert "TOP-SECRET-PAYLOAD" not in caplog.text
    assert not list((tmp_path / "quarantine").glob(".staging-*"))


def test_corrupt_json_is_quarantined_without_blocking_readable_events(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    monkeypatch.setenv("DWP_AUDIT_COLLECTOR_URL", "http://audit.test/internal/events")
    monkeypatch.setenv("DWP_AUDIT_INGEST_TOKEN", "test-token")
    monkeypatch.setenv("DWP_AUDIT_SPOOL_DIR", str(tmp_path))
    publisher = DurableAuditPublisher()
    corrupt_contents = '{"eventId":"CORRUPT-SECRET"'
    (tmp_path / "001-corrupt.json").write_text(corrupt_contents, encoding="utf-8")
    (tmp_path / "002-good.json").write_text(
        json.dumps({"eventId": "good-event", "outcome": "SUCCESS"}),
        encoding="utf-8",
    )
    delivered_batches: list[list[dict[str, object]]] = []

    def urlopen(request, timeout):
        assert timeout == 3
        delivered_batches.append(json.loads(request.data.decode("utf-8")))
        return Response(202)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    caplog.set_level(logging.CRITICAL, logger="uvicorn.error.dwp.audit.delivery")

    assert publisher.flush_once() is True

    assert delivered_batches == [[{"eventId": "good-event", "outcome": "SUCCESS"}]]
    assert not list(tmp_path.glob("*.json"))
    quarantines = [
        path for path in (tmp_path / "quarantine").iterdir() if not path.name.startswith(".")
    ]
    assert len(quarantines) == 1
    assert (
        quarantines[0] / "original" / "001-corrupt.json"
    ).read_text(encoding="utf-8") == corrupt_contents
    manifest = json.loads((quarantines[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["reason"] == "INVALID_JSON"
    assert "collectorStatus" not in manifest
    assert "CORRUPT-SECRET" not in json.dumps(manifest)
    assert "CORRUPT-SECRET" not in caplog.text


def test_http_5xx_and_transport_failures_preserve_every_event_for_retry(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DWP_AUDIT_COLLECTOR_URL", "http://audit.test/internal/events")
    monkeypatch.setenv("DWP_AUDIT_INGEST_TOKEN", "test-token")
    monkeypatch.setenv("DWP_AUDIT_SPOOL_DIR", str(tmp_path))
    publisher = DurableAuditPublisher()
    for index in range(3):
        (tmp_path / f"00{index}-event.json").write_text(
            json.dumps({"eventId": f"event-{index}", "outcome": "SUCCESS"}),
            encoding="utf-8",
        )
    calls = 0

    def server_failure(request, timeout):
        nonlocal calls
        assert timeout == 3
        calls += 1
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "Service Unavailable",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("urllib.request.urlopen", server_failure)

    assert publisher.flush_once() is False
    assert calls == 1
    assert len(list(tmp_path.glob("*.json"))) == 3
    assert not (tmp_path / "quarantine").exists()

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError("temporary network failure")
        ),
    )

    assert publisher.flush_once() is False
    assert len(list(tmp_path.glob("*.json"))) == 3
    assert not (tmp_path / "quarantine").exists()


def test_auth_routing_and_rate_limit_4xx_preserve_every_event_for_retry(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DWP_AUDIT_COLLECTOR_URL", "http://audit.test/internal/events")
    monkeypatch.setenv("DWP_AUDIT_INGEST_TOKEN", "test-token")
    monkeypatch.setenv("DWP_AUDIT_SPOOL_DIR", str(tmp_path))
    publisher = DurableAuditPublisher()
    event_file = tmp_path / "001-event.json"
    event_file.write_text(
        json.dumps({"eventId": "event-1", "outcome": "SUCCESS"}),
        encoding="utf-8",
    )

    for status in (401, 403, 404, 408, 429):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda request, timeout, status=status: (_ for _ in ()).throw(
                urllib.error.HTTPError(
                    request.full_url,
                    status,
                    "Collector-wide failure",
                    hdrs=None,
                    fp=None,
                )
            ),
        )
        assert publisher.flush_once() is False
        assert event_file.exists()
        assert not (tmp_path / "quarantine").exists()


def test_interrupted_quarantine_is_recovered_atomically_on_next_flush(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DWP_AUDIT_COLLECTOR_URL", "http://audit.test/internal/events")
    monkeypatch.setenv("DWP_AUDIT_INGEST_TOKEN", "test-token")
    monkeypatch.setenv("DWP_AUDIT_SPOOL_DIR", str(tmp_path))
    publisher = DurableAuditPublisher()
    event_file = tmp_path / "001-bad.json"
    event_file.write_text(
        json.dumps({"eventId": "bad-event", "outcome": "SUCCESS"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response(422))
    real_replace = os.replace
    interrupted = False

    def interrupt_final_directory_replace(source, destination):
        nonlocal interrupted
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not interrupted
            and source_path.name.startswith(".staging-")
            and destination_path.parent == tmp_path / "quarantine"
        ):
            interrupted = True
            raise OSError("simulated final rename interruption")
        return real_replace(source, destination)

    monkeypatch.setattr(audit_delivery_module.os, "replace", interrupt_final_directory_replace)

    assert publisher.flush_once() is False
    assert not event_file.exists()
    assert len(list((tmp_path / "quarantine").glob(".staging-*"))) == 1

    monkeypatch.setattr(audit_delivery_module.os, "replace", real_replace)

    assert publisher.flush_once() is True
    assert not list((tmp_path / "quarantine").glob(".staging-*"))
    recovered = [
        path for path in (tmp_path / "quarantine").iterdir() if not path.name.startswith(".")
    ]
    assert len(recovered) == 1
    assert json.loads(
        (recovered[0] / "original" / "001-bad.json").read_text(encoding="utf-8")
    )["eventId"] == "bad-event"
    assert json.loads((recovered[0] / "manifest.json").read_text(encoding="utf-8"))[
        "reason"
    ] == "COLLECTOR_PERMANENT_4XX"
