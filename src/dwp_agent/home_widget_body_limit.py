from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI
from starlette.responses import JSONResponse

from .home_widget_contracts import HOME_WIDGET_BATCH_PATH, HOME_WIDGET_COMMAND_PATH


HOME_WIDGET_REQUEST_LIMIT_BYTES = 256 * 1_024
_PATHS = {HOME_WIDGET_BATCH_PATH, HOME_WIDGET_COMMAND_PATH}

AsgiReceive = Callable[[], Awaitable[dict[str, Any]]]
AsgiSend = Callable[[dict[str, Any]], Awaitable[None]]


class HomeWidgetBodyLimitMiddleware:
    """Bound signed Home bodies before request parsing or identity verification buffers them."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in _PATHS
        ):
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > HOME_WIDGET_REQUEST_LIMIT_BYTES:
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
            if received > HOME_WIDGET_REQUEST_LIMIT_BYTES:
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


def install_home_widget_body_limit(app: FastAPI) -> None:
    app.add_middleware(HomeWidgetBodyLimitMiddleware)


def _content_length(scope: dict[str, Any]) -> int | None:
    values: list[bytes] = []
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == b"content-length":
            values.append(raw_value)
    if len(values) != 1:
        return None if not values else HOME_WIDGET_REQUEST_LIMIT_BYTES + 1
    try:
        value = int(values[0].decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return HOME_WIDGET_REQUEST_LIMIT_BYTES + 1
    return value if value >= 0 else HOME_WIDGET_REQUEST_LIMIT_BYTES + 1


async def _reject(
    scope: dict[str, Any], receive: AsgiReceive, send: AsgiSend
) -> None:
    response = JSONResponse(
        status_code=413,
        content={
            "detail": {
                "reasonCode": "HOME_PROVIDER_BODY_OUT_OF_BOUNDS",
                "message": "The Home provider request body exceeds its signed bound.",
            }
        },
        headers={"Cache-Control": "no-store"},
    )
    await response(scope, receive, send)
