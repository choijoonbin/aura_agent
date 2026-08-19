from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .approval_context import collect_approval_context
from .contracts import AskCitation, AskPageContext, CitationSourceType
from .policy import AskIdentity, contains_privileged_data, contains_prompt_injection


MAX_SOURCES = 12
MAX_EVIDENCE_TEXT = 1_200
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣]{2,}")


class ContextBrokerUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class GroundedSource:
    citation: AskCitation
    evidence: str
    rank: int


@dataclass(frozen=True)
class GroundedContext:
    sources: tuple[GroundedSource, ...]
    attempted_sources: tuple[str, ...]
    unavailable_sources: tuple[str, ...]

    def model_evidence(self) -> str:
        documents = [
            {
                "sourceId": source.citation.source_id,
                "sourceType": source.citation.source_type,
                "sourceSystem": source.citation.source_system,
                "title": source.citation.title,
                "occurredAt": (
                    source.citation.occurred_at.isoformat()
                    if source.citation.occurred_at is not None
                    else None
                ),
                "evidence": source.evidence,
            }
            for source in self.sources
        ]
        return json.dumps(documents, ensure_ascii=False, separators=(",", ":"))


class WorkspaceContextBroker:
    def __init__(
        self,
        *,
        platform_url: str | None = None,
        service_token: str | None = None,
        approval_url: str | None = None,
        approval_service_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.platform_url = (
            platform_url if platform_url is not None else os.getenv("SERVICE_PLATFORM_URL", "")
        ).strip().rstrip("/")
        self.service_token = (
            service_token
            if service_token is not None
            else os.getenv("DWP_PLATFORM_RUNTIME_SERVICE_TOKEN", "")
        ).strip()
        self.approval_url = (
            approval_url if approval_url is not None else os.getenv("SERVICE_APPROVAL_URL", "")
        ).strip().rstrip("/")
        self.approval_service_token = (
            approval_service_token
            if approval_service_token is not None
            else os.getenv("DWP_APPROVAL_RUNTIME_SERVICE_TOKEN", "")
        ).strip()
        self.transport = transport

    def collect(
        self,
        query: str,
        *,
        identity: AskIdentity,
        locale: str,
        agent_key: str = "DWP_ASSISTANT",
        source_scopes: tuple[CitationSourceType, ...] | list[CitationSourceType] | None = None,
        page_context: AskPageContext | None = None,
    ) -> GroundedContext:
        approval_expert = agent_key.strip().upper() == "DWP_APPROVAL_EXPERT"
        if approval_expert:
            if not self.approval_url or not self.approval_service_token:
                raise ContextBrokerUnavailable("Approval context service is not configured.")
        elif not self.platform_url or not self.service_token:
            raise ContextBrokerUnavailable("Workspace context service is not configured.")

        permissions = {permission.upper() for permission in identity.permissions}
        requested_scopes = set(source_scopes or tuple(CitationSourceType))
        candidates: list[dict[str, Any]] = []
        attempted: list[str] = []
        unavailable: list[str] = []
        identity_headers = {
            "X-DWP-User-ID": identity.user_id,
            "X-DWP-Tenant-ID": identity.tenant_id,
            "X-DWP-Roles": ",".join(identity.roles),
            "X-DWP-Permissions": ",".join(identity.permissions),
            "X-Correlation-ID": identity.correlation_id,
            "Accept-Language": locale,
            "Accept": "application/json",
        }
        if identity.person_public_id:
            identity_headers["X-DWP-Person-Public-ID"] = identity.person_public_id
        if identity.display_name_b64:
            identity_headers["X-DWP-Display-Name-B64"] = identity.display_name_b64

        with httpx.Client(transport=self.transport, timeout=3.0) as client:
            if approval_expert:
                approval_candidates, approval_attempted, approval_unavailable = collect_approval_context(
                    client,
                    approval_url=self.approval_url,
                    permissions=permissions,
                    requested_scopes=requested_scopes,
                    headers={
                        **identity_headers,
                        "X-DWP-Service-Token": self.approval_service_token,
                    },
                    locale=locale,
                )
                candidates.extend(approval_candidates)
                attempted.extend(approval_attempted)
                unavailable.extend(approval_unavailable)
            elif (
                CitationSourceType.WORK_ITEM in requested_scopes
                and "APP.WORK:VIEW" in permissions
            ):
                attempted.append("WORK_ITEM")
                try:
                    response = client.get(
                        f"{self.platform_url}/v1/workspace/work-items",
                        headers={**identity_headers, "X-DWP-Service-Token": self.service_token},
                    )
                    candidates.extend(self._work_items(response))
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    unavailable.append("WORK_ITEM")

            if (
                not approval_expert
                and CitationSourceType.MAIL in requested_scopes
                and "APP.MAIL:VIEW" in permissions
            ):
                attempted.append("MAIL")
                try:
                    response = client.get(
                        f"{self.platform_url}/v1/mail/threads",
                        headers={**identity_headers, "X-DWP-Service-Token": self.service_token},
                        params={"page": 0, "pageSize": 50},
                    )
                    candidates.extend(self._mail_threads(response))
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    unavailable.append("MAIL")

            if (
                not approval_expert
                and CitationSourceType.CALENDAR in requested_scopes
                and "APP.CALENDAR:VIEW" in permissions
            ):
                attempted.append("CALENDAR")
                try:
                    now = datetime.now(timezone.utc)
                    response = client.get(
                        f"{self.platform_url}/v1/calendar/events",
                        headers={**identity_headers, "X-DWP-Service-Token": self.service_token},
                        params={
                            "from": now.isoformat(),
                            "to": (now + timedelta(days=30)).isoformat(),
                        },
                    )
                    candidates.extend(self._calendar_events(response))
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    unavailable.append("CALENDAR")

        query_tokens = _tokens(query)
        ranked = sorted(
            candidates,
            key=lambda item: (
                -(
                    _relevance(query_tokens, item["title"], item["evidence"])
                    + _page_context_bonus(item, page_context)
                ),
                item.get("sortTime") or "",
                item["title"],
            ),
            reverse=False,
        )[:MAX_SOURCES]
        sources = tuple(
            GroundedSource(
                citation=AskCitation(
                    source_id=f"src-{index:02d}",
                    source_type=item["sourceType"],
                    title=item["title"],
                    source_system=item["sourceSystem"],
                    route=item.get("route"),
                    occurred_at=item.get("occurredAt"),
                    excerpt=item["evidence"][:500],
                ),
                evidence=item["evidence"],
                rank=index,
            )
            for index, item in enumerate(ranked, start=1)
        )
        return GroundedContext(
            sources=sources,
            attempted_sources=tuple(attempted),
            unavailable_sources=tuple(unavailable),
        )

    def _work_items(self, response: httpx.Response) -> list[dict[str, Any]]:
        response.raise_for_status()
        items = response.json()["data"]["items"]
        if not isinstance(items, list):
            raise ValueError("Workspace work item response is invalid.")
        result: list[dict[str, Any]] = []
        for item in items:
            title = _clean(item.get("title"), 300)
            if not title:
                continue
            evidence = _clean(
                " | ".join(
                    value
                    for value in (
                        item.get("summary"),
                        item.get("reason"),
                        item.get("recommendedNext"),
                        item.get("latestActivity"),
                        f"status={item.get('status')}" if item.get("status") else None,
                        f"priority={item.get('priority')}" if item.get("priority") else None,
                        f"dueAt={item.get('dueAt')}" if item.get("dueAt") else None,
                    )
                    if value
                ),
                MAX_EVIDENCE_TEXT,
            )
            if contains_privileged_data(f"{title} {evidence}"):
                continue
            if _restricted_source(item) or contains_prompt_injection(f"{title} {evidence}"):
                continue
            result.append(
                {
                    "sourceType": CitationSourceType.WORK_ITEM,
                    "sourceSystem": _clean(item.get("sourceSystem"), 100) or "DWP Work",
                    "title": title,
                    "evidence": evidence or title,
                    "route": _safe_route(item.get("sourceRoute")),
                    "occurredAt": _datetime(item.get("updatedAt")),
                    "sortTime": item.get("dueAt") or item.get("updatedAt") or "",
                }
            )
        return result

    def _mail_threads(self, response: httpx.Response) -> list[dict[str, Any]]:
        response.raise_for_status()
        items = response.json()["data"]["items"]
        if not isinstance(items, list):
            raise ValueError("Mail thread response is invalid.")
        result: list[dict[str, Any]] = []
        for item in items:
            title = _clean(item.get("subject"), 300)
            if not title:
                continue
            evidence = _clean(
                " | ".join(
                    value
                    for value in (
                        item.get("preview"),
                        f"importance={item.get('importance')}" if item.get("importance") else None,
                        f"lane={item.get('triageLane')}" if item.get("triageLane") else None,
                        f"state={item.get('workflowState')}" if item.get("workflowState") else None,
                        f"unread={item.get('unread')}" if item.get("unread") is not None else None,
                        (
                            f"latestMessageAt={item.get('latestMessageAt')}"
                            if item.get("latestMessageAt")
                            else None
                        ),
                    )
                    if value
                ),
                MAX_EVIDENCE_TEXT,
            )
            if contains_privileged_data(f"{title} {evidence}"):
                continue
            if _restricted_source(item) or contains_prompt_injection(f"{title} {evidence}"):
                continue
            result.append(
                {
                    "sourceType": CitationSourceType.MAIL,
                    "sourceSystem": _clean(item.get("accountName"), 100) or "DWP Mail",
                    "title": title,
                    "evidence": evidence or title,
                    "route": _mail_route(item.get("threadId")),
                    "occurredAt": _datetime(item.get("latestMessageAt")),
                    "sortTime": item.get("latestMessageAt") or "",
                }
            )
        return result

    def _calendar_events(self, response: httpx.Response) -> list[dict[str, Any]]:
        response.raise_for_status()
        items = response.json()["data"]
        if not isinstance(items, list):
            raise ValueError("Calendar event response is invalid.")
        result: list[dict[str, Any]] = []
        for item in items:
            title = _clean(item.get("title"), 300)
            if not title:
                continue
            evidence = _clean(
                " | ".join(
                    value
                    for value in (
                        item.get("description"),
                        f"startsAt={item.get('startsAt')}" if item.get("startsAt") else None,
                        f"endsAt={item.get('endsAt')}" if item.get("endsAt") else None,
                        f"location={item.get('location')}" if item.get("location") else None,
                        f"status={item.get('status')}" if item.get("status") else None,
                        f"response={item.get('myResponse')}" if item.get("myResponse") else None,
                        f"conflict={item.get('conflict')}",
                    )
                    if value
                ),
                MAX_EVIDENCE_TEXT,
            )
            if contains_privileged_data(f"{title} {evidence}"):
                continue
            if _restricted_source(item) or contains_prompt_injection(f"{title} {evidence}"):
                continue
            result.append(
                {
                    "sourceType": CitationSourceType.CALENDAR,
                    "sourceSystem": _clean(item.get("calendarName"), 100) or "DWP Calendar",
                    "title": title,
                    "evidence": evidence or title,
                    "route": _calendar_route(item.get("eventId")),
                    "occurredAt": _datetime(item.get("startsAt")),
                    "sortTime": item.get("startsAt") or "",
                }
            )
        return result


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in TOKEN_PATTERN.findall(value)}


def _restricted_source(item: dict[str, Any]) -> bool:
    values = {
        str(item.get(key) or "").strip().upper()
        for key in ("dataClassification", "sensitivity", "visibility")
    }
    return bool(values & {"PRIVATE", "RESTRICTED", "HIGHLY_RESTRICTED", "SECRET", "PRIVILEGED"})


def _relevance(query_tokens: set[str], title: str, evidence: str) -> int:
    candidate_tokens = _tokens(f"{title} {evidence}")
    return len(query_tokens & candidate_tokens) * 10 + min(len(candidate_tokens), 20)


def _page_context_bonus(item: dict[str, Any], page_context: AskPageContext | None) -> int:
    if page_context is None:
        return 0
    route = str(item.get("route") or "")
    app_segment = page_context.route.strip("/").split("/", 1)[0]
    return 8 if app_segment and route.startswith(f"/{app_segment}") else 0


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", " ").split())[:limit]


def _safe_route(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    route = value.strip()
    if not route or len(route) > 1_000:
        return None
    if route.startswith("/") or route.startswith("https://"):
        return route
    return None


def _mail_route(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
        value,
    ):
        return None
    return f"/mail/inbox?thread={value}"


def _calendar_route(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
        value,
    ):
        return None
    return f"/calendar/schedule?event={value}"


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
