from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from starlette.responses import JSONResponse


ROUTE_HEADER = "X-DWP-Route-Contract-Key"
CURRENT_REVISION_HEADER = "X-DWP-Current-Decision-Revision"
CURRENT_REVALIDATE_AT_HEADER = "X-DWP-Current-Revalidate-At"
EXPECTED_REVISION_HEADER = "X-DWP-Expected-Decision-Revision"
RESPONSE_REVISION_HEADER = "X-DWP-Decision-Revision"
CONTEXT_HEADER = "X-DWP-Context-Key"
SCOPE_HEADER = "X-DWP-Context-Scope-Key"
ACCESS_MODE_HEADER = "X-DWP-Active-Access-Mode"
ROLLOUT_STATE_HEADER = "X-DWP-Rollout-State"
ROLLOUT_REVISION_HEADER = "X-DWP-Rollout-Revision"
ROLLOUT_COHORT_HEADER = "X-DWP-Rollout-Cohort"
SUPPORT_SESSION_HEADER = "X-DWP-Support-Session-ID"

PRODUCT_KEY = "dwaion"
SURFACE_KEY = "dwaion.work"
ACCESS_POLICY_KEY = "dwaion.work-access.v1"
OWNER_SERVICE = "dwp-agent-runtime"

PAGE_ROUTE = "route.dwaion.work.home.page"
DATA_ROUTE = "route.dwaion.work.conversation.data"
ACTION_ROUTE = "route.dwaion.work.ask.action"

_ROLLOUT_STATES = {"000", "100", "110", "111"}
_ROLLOUT_COHORTS = {
    "baseline",
    "holdout",
    "full",
    "eligible-10",
    "eligible-25",
    "eligible-50",
    "eligible-90",
}
_WORK_ACCESS_MODES = {"NORMAL", "ELEVATED"}
_CONTEXT = re.compile(r"^psc-[a-f0-9]{64}$")
_ROLLOUT_REVISION = re.compile(r"^rollout-[a-f0-9]{64}$")
_DECISION_REVISION = re.compile(r"^psr-[a-f0-9]{64}$")
_DATA_CANDIDATE = re.compile(r"^/v1/conversations/([^/]+)$")
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

AsgiReceive = Callable[[], Awaitable[dict[str, Any]]]
AsgiSend = Callable[[dict[str, Any]], Awaitable[None]]


class ProductSurfacePepMiddleware:
    """Owner PEP for the exact DWAI PAGE, DATA and ACTION draft candidates."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))
        if not owns_candidate(method, path):
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        state = _exact_header(headers, ROLLOUT_STATE_HEADER)
        enabled = _enabled()
        if state is None and not enabled and not _present(headers, ROLLOUT_STATE_HEADER):
            await self.app(scope, receive, send)
            return

        rollout_revision = _exact_header(headers, ROLLOUT_REVISION_HEADER)
        cohort = _exact_header(headers, ROLLOUT_COHORT_HEADER)
        if (
            state not in _ROLLOUT_STATES
            or rollout_revision is None
            or _ROLLOUT_REVISION.fullmatch(rollout_revision) is None
            or cohort not in _ROLLOUT_COHORTS
        ):
            await _reject(
                scope,
                receive,
                send,
                503,
                "Trusted DWAI rollout evidence is missing or invalid.",
            )
            return
        if state[1] != "1":
            await self.app(scope, receive, send)
            return
        if not enabled:
            await _reject(
                scope,
                receive,
                send,
                503,
                "DWAI product authorization v4 is not ready for enforcement.",
            )
            return

        binding = resolve_binding(method, path)
        route = _exact_header(headers, ROUTE_HEADER)
        context = _exact_header(headers, CONTEXT_HEADER)
        selected_scope = _exact_header(headers, SCOPE_HEADER)
        access_mode = _exact_header(headers, ACCESS_MODE_HEADER)
        if (
            binding is None
            or route != binding.route_contract_key
            or context is None
            or _CONTEXT.fullmatch(context) is None
            or selected_scope is None
            or access_mode is None
        ):
            await _reject(
                scope,
                receive,
                send,
                503,
                "Trusted DWAI route, context, scope and access mode are invalid.",
            )
            return
        if (
            access_mode not in _WORK_ACCESS_MODES
            or _present(headers, SUPPORT_SESSION_HEADER)
        ):
            await _reject(
                scope,
                receive,
                send,
                403,
                "Provider support cannot assume normal DWAI authority.",
            )
            return

        tenant_id = _positive_identifier(_exact_header(headers, "X-DWP-Tenant-ID"))
        user_id = _positive_identifier(_exact_header(headers, "X-DWP-User-ID"))
        identity_plane = _exact_header(headers, "X-DWP-Identity-Plane")
        roles = _header_tokens(headers, "X-DWP-Roles")
        permissions = _header_tokens(headers, "X-DWP-Permissions")
        if (
            tenant_id is None
            or user_id is None
            or identity_plane != "TENANT"
            or roles is None
            or any(role.startswith("PROVIDER_") for role in roles)
            or permissions is None
            or "APP.ASK:VIEW" not in permissions
        ):
            await _reject(scope, receive, send, 403, "The exact DWAI authority is missing.")
            return
        if selected_scope != self_scope_key(tenant_id, user_id):
            await _reject(
                scope,
                receive,
                send,
                403,
                "The selected DWAI owner scope is no longer valid.",
            )
            return

        current_revision = _exact_header(headers, CURRENT_REVISION_HEADER)
        revalidate_at = _future_instant(
            _exact_header(headers, CURRENT_REVALIDATE_AT_HEADER)
        )
        if (
            current_revision is None
            or _DECISION_REVISION.fullmatch(current_revision) is None
            or revalidate_at is None
        ):
            await _reject(
                scope,
                receive,
                send,
                503,
                "Trusted current DWAI authority is missing or expired.",
            )
            return
        if binding.route_kind == "ACTION":
            expected_revision = _exact_header(headers, EXPECTED_REVISION_HEADER)
            if expected_revision != current_revision:
                await _reject(
                    scope,
                    receive,
                    send,
                    409,
                    "DWAI authority changed after the client decision.",
                )
                return

        async def send_with_revision(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append(
                    (RESPONSE_REVISION_HEADER.lower().encode("ascii"), current_revision.encode())
                )
                message = {**message, "headers": response_headers}
            await send(message)

        await self.app(scope, receive, send_with_revision)


class Binding:
    def __init__(self, route_contract_key: str, route_kind: str) -> None:
        self.route_contract_key = route_contract_key
        self.route_kind = route_kind


def install_product_surface_pep(app: FastAPI) -> None:
    app.add_middleware(ProductSurfacePepMiddleware)


def owns_candidate(method: str, path: str) -> bool:
    if method == "POST" and path == "/v1/ask":
        return True
    if method == "GET" and path == "/v1/conversations":
        return True
    return method == "GET" and _DATA_CANDIDATE.fullmatch(path) is not None


def resolve_binding(method: str, path: str) -> Binding | None:
    if method == "POST" and path == "/v1/ask":
        return Binding(ACTION_ROUTE, "ACTION")
    if method == "GET" and path == "/v1/conversations":
        return Binding(PAGE_ROUTE, "PAGE")
    match = _DATA_CANDIDATE.fullmatch(path) if method == "GET" else None
    if match is None or _UUID.fullmatch(match.group(1)) is None:
        return None
    try:
        UUID(match.group(1))
    except ValueError:
        return None
    return Binding(DATA_ROUTE, "DATA")


def self_scope_key(tenant_id: int, user_id: int) -> str:
    material = (
        f"{tenant_id}\n{user_id}\n{PRODUCT_KEY}\n{SURFACE_KEY}\nSELF\nSELF"
    ).encode()
    return "scope-" + hashlib.sha256(material).hexdigest()[:32]


def _enabled() -> bool:
    return os.getenv("DWP_AGENT_PRODUCT_AUTHORIZATION_V4_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _headers(scope: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for raw_name, raw_value in scope.get("headers", []):
        try:
            name = raw_name.decode("latin-1").lower()
            value = raw_value.decode("latin-1")
        except UnicodeDecodeError:
            continue
        result.setdefault(name, []).append(value)
    return result


def _exact_header(headers: dict[str, list[str]], name: str) -> str | None:
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


def _present(headers: dict[str, list[str]], name: str) -> bool:
    return bool(headers.get(name.lower()))


def _header_tokens(headers: dict[str, list[str]], name: str) -> set[str] | None:
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


def _positive_identifier(value: str | None) -> int | None:
    if value is None or re.fullmatch(r"[1-9][0-9]*", value) is None:
        return None
    return int(value)


def _future_instant(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed <= datetime.now(timezone.utc):
        return None
    return parsed


async def _reject(
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
