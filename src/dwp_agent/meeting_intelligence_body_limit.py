from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI
from starlette.responses import JSONResponse


MEETING_INTELLIGENCE_REQUEST_LIMIT_BYTES = 2 * 1_024 * 1_024
_ANALYZE_PATH = "/internal/v1/meeting-intelligence/analyze"

AsgiReceive = Callable[[], Awaitable[dict[str, Any]]]
AsgiSend = Callable[[dict[str, Any]], Awaitable[None]]


class MeetingIntelligenceBodyLimitMiddleware:
    """Reject oversized internal analysis bodies before auth or JSON parsing buffers them."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        if scope.get("type") != "http" or scope.get("path") != _ANALYZE_PATH:
            await self.app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if content_length is not None and content_length > MEETING_INTELLIGENCE_REQUEST_LIMIT_BYTES:
            await _reject(scope, receive, send)
            return

        buffered: deque[dict[str, Any]] = deque()
        received = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                buffered.append(message)
                break
            body = message.get("body", b"")
            received += len(body)
            if received > MEETING_INTELLIGENCE_REQUEST_LIMIT_BYTES:
                await _reject(scope, receive, send)
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break

        async def replay() -> dict[str, Any]:
            if buffered:
                return buffered.popleft()
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)


def install_meeting_intelligence_body_limit(app: FastAPI) -> None:
    app.add_middleware(MeetingIntelligenceBodyLimitMiddleware)


def _content_length(scope: dict[str, Any]) -> int | None:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() != b"content-length":
            continue
        try:
            value = int(raw_value.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return MEETING_INTELLIGENCE_REQUEST_LIMIT_BYTES + 1
        return value if value >= 0 else MEETING_INTELLIGENCE_REQUEST_LIMIT_BYTES + 1
    return None


async def _reject(
    scope: dict[str, Any],
    receive: AsgiReceive,
    send: AsgiSend,
) -> None:
    response = JSONResponse(
        status_code=413,
        content={"detail": "Meeting intelligence request is too large."},
        headers={"Cache-Control": "no-store"},
    )
    await response(scope, receive, send)
