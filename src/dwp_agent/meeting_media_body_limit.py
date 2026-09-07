from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI
from starlette.responses import JSONResponse


MEETING_MEDIA_REQUEST_LIMIT_BYTES = 64 * 1_024
_PREFIXES = ("/internal/v1/meeting-recording/", "/internal/v1/meeting-transcripts/")

AsgiReceive = Callable[[], Awaitable[dict[str, Any]]]
AsgiSend = Callable[[dict[str, Any]], Awaitable[None]]


class MeetingMediaBodyLimitMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        path = str(scope.get("path", ""))
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or not path.startswith(_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return
        declared = _content_length(scope)
        if declared is not None and declared > MEETING_MEDIA_REQUEST_LIMIT_BYTES:
            await _reject(scope, receive, send)
            return
        buffered: deque[dict[str, Any]] = deque()
        received = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                buffered.append(message)
                break
            received += len(message.get("body", b""))
            if received > MEETING_MEDIA_REQUEST_LIMIT_BYTES:
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


def install_meeting_media_body_limit(app: FastAPI) -> None:
    app.add_middleware(MeetingMediaBodyLimitMiddleware)


def _content_length(scope: dict[str, Any]) -> int | None:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() != b"content-length":
            continue
        try:
            value = int(raw_value.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return MEETING_MEDIA_REQUEST_LIMIT_BYTES + 1
        return value if value >= 0 else MEETING_MEDIA_REQUEST_LIMIT_BYTES + 1
    return None


async def _reject(
    scope: dict[str, Any], receive: AsgiReceive, send: AsgiSend
) -> None:
    response = JSONResponse(
        status_code=413,
        content={"detail": "Meeting media request is too large."},
        headers={"Cache-Control": "no-store"},
    )
    await response(scope, receive, send)
