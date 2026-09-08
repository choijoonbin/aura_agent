from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI


PRIVATE_NO_STORE_CACHE_CONTROL = "private, no-store, max-age=0"
_PRIVATE_API_PREFIXES = ("/v1/activity", "/v1/runs")

AsgiReceive = Callable[[], Awaitable[dict[str, Any]]]
AsgiSend = Callable[[dict[str, Any]], Awaitable[None]]


class PrivateNoStoreMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        path = str(scope.get("path", ""))
        if scope.get("type") != "http" or not any(
            path == prefix or path.startswith(f"{prefix}/")
            for prefix in _PRIVATE_API_PREFIXES
        ):
            await self.app(scope, receive, send)
            return

        async def send_no_store(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"cache-control"
                ]
                headers.append(
                    (
                        b"cache-control",
                        PRIVATE_NO_STORE_CACHE_CONTROL.encode("ascii"),
                    )
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_no_store)


def install_private_no_store(app: FastAPI) -> None:
    app.add_middleware(PrivateNoStoreMiddleware)
