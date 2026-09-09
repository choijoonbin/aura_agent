from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from starlette.responses import JSONResponse


AsgiReceive = Callable[[], Awaitable[dict[str, Any]]]
AsgiSend = Callable[[dict[str, Any]], Awaitable[None]]


def headers(scope: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for raw_name, raw_value in scope.get("headers", []):
        try:
            name = raw_name.decode("latin-1").lower()
            value = raw_value.decode("latin-1")
        except UnicodeDecodeError:
            continue
        result.setdefault(name, []).append(value)
    return result


def exact_header(headers: dict[str, list[str]], name: str) -> str | None:
    values = headers.get(name.lower(), [])
    if len(values) != 1:
        return None
    value = values[0]
    if (
        not value
        or value != value.strip()
        or len(value) > 200
        or "," in value
        or "\r" in value
        or "\n" in value
    ):
        return None
    return value


def present(headers: dict[str, list[str]], name: str) -> bool:
    return bool(headers.get(name.lower()))


def header_tokens(headers: dict[str, list[str]], name: str) -> set[str] | None:
    values = headers.get(name.lower(), [])
    if not values:
        return set()
    if len(values) != 1:
        return None
    value = values[0]
    if (
        not value
        or value != value.strip()
        or len(value) > 4_000
        or "\r" in value
        or "\n" in value
    ):
        return None
    tokens = value.split(",")
    if any(
        not token or token != token.strip() or token != token.upper()
        for token in tokens
    ):
        return None
    canonical = set(tokens)
    return canonical if len(canonical) == len(tokens) else None


def positive_identifier(value: str | None) -> int | None:
    import re

    if value is None or re.fullmatch(r"[1-9][0-9]*", value) is None:
        return None
    return int(value)


def future_instant(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed <= datetime.now(timezone.utc):
        return None
    return parsed


async def reject(
    scope: dict[str, Any],
    receive: AsgiReceive,
    send: AsgiSend,
    status_code: int,
    detail: str,
) -> None:
    response = JSONResponse(
        status_code=status_code,
        content={"detail": detail},
        headers={"Cache-Control": "no-store"},
    )
    await response(scope, receive, send)
