from __future__ import annotations

import re
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie

from fastapi import Request


_BEARER_PATTERN = re.compile(r"^Bearer [!-~]+$", re.IGNORECASE)
_NORMAL_ACCESS_MODES = {"NORMAL", "ELEVATED"}
_SESSION_COOKIE = "DWP_SESSION"


@dataclass(frozen=True)
class WorkspaceRequestAuthorization:
    cookie_header: str | None = field(default=None, repr=False)
    authorization_header: str | None = field(default=None, repr=False)
    blocked: bool = False
    session_family_id: str | None = field(default=None, repr=False)

    @property
    def available(self) -> bool:
        return not self.blocked and bool(
            self.cookie_header or self.authorization_header
        )

    def outbound_headers(self) -> dict[str, str]:
        if not self.available:
            return {}
        headers: dict[str, str] = {}
        if self.cookie_header is not None:
            headers["Cookie"] = self.cookie_header
        if self.authorization_header is not None:
            headers["Authorization"] = self.authorization_header
        return headers


def resolve_workspace_request_authorization(
    request: Request,
) -> WorkspaceRequestAuthorization:
    access_modes = request.headers.getlist("X-DWP-Active-Access-Mode")
    support_sessions = request.headers.getlist("X-DWP-Support-Session-ID")
    if len(access_modes) > 1 or len(support_sessions) > 1:
        return WorkspaceRequestAuthorization(blocked=True)
    if support_sessions and support_sessions[0].strip():
        return WorkspaceRequestAuthorization(blocked=True)
    if access_modes:
        access_mode = access_modes[0].strip().upper()
        if access_mode not in _NORMAL_ACCESS_MODES:
            return WorkspaceRequestAuthorization(blocked=True)

    sessions = request.headers.getlist("X-DWP-Auth-Session-ID")
    if len(sessions) > 1 or (sessions and (
        not sessions[0].strip() or len(sessions[0]) > 160 or _has_control(sessions[0])
    )):
        return WorkspaceRequestAuthorization(blocked=True)

    authorization_values = request.headers.getlist("Authorization")
    if len(authorization_values) > 1:
        return WorkspaceRequestAuthorization(blocked=True)
    authorization = authorization_values[0] if authorization_values else None
    if authorization is not None and not _valid_bearer(authorization):
        return WorkspaceRequestAuthorization(blocked=True)

    cookie, invalid_cookie = _session_cookie(request.headers.getlist("Cookie"))
    if invalid_cookie:
        return WorkspaceRequestAuthorization(blocked=True)
    return WorkspaceRequestAuthorization(
        cookie_header=cookie,
        authorization_header=authorization,
        session_family_id=sessions[0] if sessions else None,
    )


def _session_cookie(cookie_headers: list[str]) -> tuple[str | None, bool]:
    sessions: list[str] = []
    for raw in cookie_headers:
        if len(raw) > 16_384 or _has_control(raw):
            return None, True
        parsed: SimpleCookie[str] = SimpleCookie()
        try:
            parsed.load(raw)
        except CookieError:
            return None, True
        morsel = parsed.get(_SESSION_COOKIE)
        if morsel is not None:
            sessions.append(morsel.value)
        elif _SESSION_COOKIE in raw:
            return None, True
    if len(sessions) > 1:
        return None, True
    if not sessions:
        return None, False
    value = sessions[0]
    if not value or len(value) > 4_096 or _has_control(value):
        return None, True
    return f"{_SESSION_COOKIE}={value}", False


def _valid_bearer(value: str) -> bool:
    return len(value) <= 8_192 and _BEARER_PATTERN.fullmatch(value) is not None


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
