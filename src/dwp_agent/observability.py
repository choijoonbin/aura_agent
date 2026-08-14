from __future__ import annotations

import atexit
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request, Response


LOGGER = logging.getLogger(__name__)
COLLECTOR_TOKEN_HEADER = "X-DWP-Observability-Token"
COLLECTOR_SERVICE_HEADER = "X-DWP-Observability-Service"
POLICY_VERSION = "dwp-api-history-v1"
TRACEPARENT = re.compile(
    r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$",
    re.IGNORECASE,
)
UUID_SEGMENT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
INTEGER_SEGMENT = re.compile(r"^[0-9]{1,20}$")
LONG_HEX_SEGMENT = re.compile(r"^[0-9a-f]{13,}$", re.IGNORECASE)
TOKEN_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{25,}$")


class ApiHistoryPublisher:
    """Bounded, fail-open batch publisher used by the agent runtime."""

    def __init__(self) -> None:
        self.collector_url = os.getenv("DWP_API_HISTORY_COLLECTOR_URL", "").strip()
        self.ingest_token = os.getenv("DWP_API_HISTORY_INGEST_TOKEN", "").strip()
        self.enabled = (
            os.getenv("DWP_API_HISTORY_ENABLED", "true").lower() != "false"
            and bool(self.collector_url)
            and bool(self.ingest_token)
        )
        self.batch_size = max(
            1, min(200, _integer_env("DWP_API_HISTORY_BATCH_SIZE", 100))
        )
        self.flush_seconds = max(
            0.1, _duration_seconds(os.getenv("DWP_API_HISTORY_FLUSH_INTERVAL", "PT1S"))
        )
        self.events: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=max(128, _integer_env("DWP_API_HISTORY_QUEUE_CAPACITY", 4096))
        )
        self.dropped_events = 0
        self._running = self.enabled
        self._worker: threading.Thread | None = None
        if self.enabled:
            self._worker = threading.Thread(
                target=self._drain,
                name="dwp-agent-api-history-exporter",
                daemon=True,
            )
            self._worker.start()

    def publish(self, event: dict[str, Any]) -> None:
        if not self.enabled or not self._running:
            return
        try:
            self.events.put_nowait(event)
        except queue.Full:
            self.dropped_events += 1
            if self.dropped_events & (self.dropped_events - 1) == 0:
                LOGGER.warning(
                    "API history queue is full; dropped_events=%s",
                    self.dropped_events,
                )

    def close(self) -> None:
        self._running = False
        if self._worker is not None:
            self._worker.join(timeout=2)

    def _drain(self) -> None:
        while self._running or not self.events.empty():
            try:
                first = self.events.get(timeout=self.flush_seconds)
            except queue.Empty:
                continue
            batch = [first]
            while len(batch) < self.batch_size:
                try:
                    batch.append(self.events.get_nowait())
                except queue.Empty:
                    break
            if not self._send(batch):
                self.dropped_events += len(batch)

    def _send(self, batch: list[dict[str, Any]]) -> bool:
        payload = json.dumps(batch, separators=(",", ":")).encode("utf-8")
        for attempt in range(3):
            request = urllib.request.Request(
                self.collector_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    COLLECTOR_TOKEN_HEADER: self.ingest_token,
                    COLLECTOR_SERVICE_HEADER: "dwp-agent-runtime",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    if 200 <= response.status < 300:
                        return True
                    LOGGER.warning(
                        "API history collector rejected batch; status=%s size=%s",
                        response.status,
                        len(batch),
                    )
            except (OSError, urllib.error.URLError) as error:
                if attempt == 2:
                    LOGGER.warning(
                        "API history collector unavailable; error=%s size=%s",
                        type(error).__name__,
                        len(batch),
                    )
            if attempt < 2 and self._running:
                time.sleep(0.1 * (2**attempt))
        return False


def install_api_history(app: FastAPI) -> None:
    @app.middleware("http")
    async def api_history_middleware(request: Request, call_next: Any) -> Response:
        occurred_at = datetime.now(UTC)
        started = time.perf_counter_ns()
        trace_id, span_id, parent_span_id = _trace_context(
            request.headers.get("traceparent")
        )
        correlation_id = _correlation_id(request.headers.get("X-Correlation-ID"))
        response: Response | None = None
        failure: BaseException | None = None
        try:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        except BaseException as error:
            failure = error
            raise
        finally:
            completed_at = datetime.now(UTC)
            duration_ms = max(0, (time.perf_counter_ns() - started) // 1_000_000)
            event = build_api_history_event(
                request=request,
                response=response,
                occurred_at=occurred_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                failure=failure,
                correlation_id=correlation_id,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
            )
            PUBLISHER.publish(event)


def build_api_history_event(
    *,
    request: Request,
    response: Response | None,
    occurred_at: datetime,
    completed_at: datetime,
    duration_ms: int,
    failure: BaseException | None,
    correlation_id: str,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None,
) -> dict[str, Any]:
    status_code = response.status_code if response is not None else 500
    route = request.scope.get("route")
    route_template = getattr(route, "path", None) or request.url.path
    user_id = _clean_identifier(request.headers.get("X-DWP-User-ID"), 160)
    user_agent = request.headers.get("User-Agent")
    return {
        "historyId": str(uuid.uuid4()),
        "occurredAt": occurred_at.isoformat().replace("+00:00", "Z"),
        "completedAt": completed_at.isoformat().replace("+00:00", "Z"),
        "tenantId": _positive_integer(request.headers.get("X-DWP-Tenant-ID")),
        "actorType": "USER" if user_id else "ANONYMOUS",
        "actorId": user_id,
        "authType": "SERVICE"
        if request.headers.get("X-DWP-Service-Token")
        else "NONE",
        "serviceName": "dwp-agent-runtime",
        "serviceVersion": _clean_identifier(os.getenv("APP_VERSION", "0.2.0"), 60),
        "serviceInstance": _clean_identifier(
            os.getenv("DWP_SERVICE_INSTANCE", os.getenv("HOSTNAME", "local")), 160
        ),
        "environment": _clean_identifier(os.getenv("DWP_ENVIRONMENT", "local"), 40),
        "observationPoint": "SERVICE",
        "routeId": _clean_identifier(getattr(route, "name", None), 120),
        "httpMethod": request.method[:12],
        "routeTemplate": _normalize_template(route_template),
        "requestPath": _normalize_path(request.url.path),
        "httpScheme": request.url.scheme[:12],
        "httpProtocol": _clean_identifier(request.scope.get("http_version"), 20),
        "statusCode": status_code,
        "outcome": _outcome(status_code, failure),
        "durationMs": duration_ms,
        "requestSizeBytes": _content_length(request.headers.get("content-length")),
        "responseSizeBytes": _content_length(
            response.headers.get("content-length") if response is not None else None
        ),
        "correlationId": correlation_id,
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent_span_id,
        "clientAddressHash": _privacy_hash(request.client.host if request.client else None),
        "userAgentFamily": _user_agent_family(user_agent),
        "userAgentHash": _privacy_hash(user_agent),
        "errorType": type(failure).__name__[:80] if failure is not None else None,
        "capturePolicyVersion": POLICY_VERSION,
    }


def _trace_context(traceparent: str | None) -> tuple[str, str, str | None]:
    match = TRACEPARENT.fullmatch(traceparent.strip()) if traceparent else None
    parent_span_id = None
    if match and match.group(1) != "0" * 32 and match.group(2) != "0" * 16:
        trace_id = match.group(1).lower()
        parent_span_id = match.group(2).lower()
    else:
        trace_id = secrets.token_hex(16)
    return trace_id, secrets.token_hex(8), parent_span_id


def _correlation_id(value: str | None) -> str:
    cleaned = _clean_identifier(value, 128)
    if cleaned and re.fullmatch(r"[A-Za-z0-9._:-]+", cleaned):
        return cleaned
    return str(uuid.uuid4())


def _normalize_template(value: str | None) -> str:
    if not value:
        return "/"
    return value.split("?", 1)[0].split("#", 1)[0][:500]


def _normalize_path(value: str | None) -> str:
    if not value:
        return "/"
    segments: list[str] = []
    for raw_segment in value.split("?", 1)[0].split("#", 1)[0].split("/"):
        segment = raw_segment.split(";", 1)[0]
        if not segment:
            continue
        lowered = segment.lower()
        if "@" in segment or "%40" in lowered:
            segment = "{value}"
        elif (
            UUID_SEGMENT.fullmatch(segment)
            or INTEGER_SEGMENT.fullmatch(segment)
            or LONG_HEX_SEGMENT.fullmatch(segment)
        ):
            segment = "{id}"
        elif len(segment) > 64 or TOKEN_SEGMENT.fullmatch(segment):
            segment = "{token}"
        segments.append(segment)
    return ("/" + "/".join(segments))[:500] if segments else "/"


def _clean_identifier(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    cleaned = str(value).replace("\r", "").replace("\n", "").strip()
    return cleaned[:maximum] or None


def _positive_integer(value: str | None) -> int | None:
    try:
        parsed = int(value or "")
        return parsed if parsed > 0 else None
    except ValueError:
        return None


def _content_length(value: str | None) -> int | None:
    try:
        parsed = int(value or "")
        return parsed if parsed >= 0 else None
    except ValueError:
        return None


def _privacy_hash(value: str | None) -> str | None:
    secret = os.getenv("DWP_API_HISTORY_PRIVACY_HASH_SECRET", "").encode("utf-8")
    if not value or not secret:
        return None
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _user_agent_family(value: str | None) -> str:
    lowered = (value or "").lower()
    if not lowered:
        return "UNKNOWN"
    if "edg/" in lowered:
        return "EDGE"
    if "chrome/" in lowered or "chromium/" in lowered:
        return "CHROMIUM"
    if "firefox/" in lowered:
        return "FIREFOX"
    if "safari/" in lowered and "chrome/" not in lowered:
        return "SAFARI"
    if "curl/" in lowered:
        return "CURL"
    if "postman" in lowered:
        return "POSTMAN"
    if "httpx" in lowered:
        return "HTTPX"
    return "OTHER"


def _outcome(status_code: int, failure: BaseException | None) -> str:
    if failure is not None or status_code >= 500:
        return "SERVER_ERROR"
    if status_code >= 400:
        return "CLIENT_ERROR"
    if status_code >= 300:
        return "REDIRECTION"
    return "SUCCESS"


def _integer_env(name: str, fallback: int) -> int:
    try:
        return int(os.getenv(name, str(fallback)))
    except ValueError:
        return fallback


def _duration_seconds(value: str) -> float:
    match = re.fullmatch(r"PT([0-9]+(?:\.[0-9]+)?)S", value.upper())
    return float(match.group(1)) if match else 1.0


PUBLISHER = ApiHistoryPublisher()
atexit.register(PUBLISHER.close)
