from fastapi.testclient import TestClient

from dwp_agent import observability
from dwp_agent.main import app


def test_agent_history_is_trace_linked_and_privacy_minimized(monkeypatch) -> None:
    events: list[dict[str, object]] = []
    monkeypatch.setattr(observability.PUBLISHER, "publish", events.append)
    monkeypatch.setenv("DWP_API_HISTORY_PRIVACY_HASH_SECRET", "test-privacy-secret")

    response = TestClient(app).get(
        "/health?token=must-not-be-recorded",
        headers={
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "X-Correlation-ID": "test-correlation",
            "User-Agent": "sensitive-agent-value/1.0",
        },
    )

    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "test-correlation"
    assert len(events) == 1
    event = events[0]
    assert event["traceId"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert event["parentSpanId"] == "00f067aa0ba902b7"
    assert event["routeTemplate"] == "/health"
    assert event["requestPath"] == "/health"
    assert event["userAgentFamily"] == "OTHER"
    serialized = str(event)
    assert "must-not-be-recorded" not in serialized
    assert "sensitive-agent-value" not in serialized


def test_event_masks_dynamic_path_values() -> None:
    client = TestClient(app)
    request = client.build_request(
        "GET",
        "/v1/people/123456?email=private@example.com",
        headers={"X-DWP-Tenant-ID": "7", "X-DWP-User-ID": "42"},
    )
    # Starlette's Request is built by the middleware in production; the route-level
    # normalization itself is deterministic and tested directly here.
    assert observability._normalize_path(request.url.path) == "/v1/people/{id}"
    assert observability._normalize_path("/users/private@example.com") == "/users/{value}"


def test_invalid_traceparent_creates_new_trace() -> None:
    trace_id, span_id, parent_span_id = observability._trace_context("invalid")

    assert len(trace_id) == 32
    assert len(span_id) == 16
    assert parent_span_id is None
    int(trace_id, 16)
    int(span_id, 16)
