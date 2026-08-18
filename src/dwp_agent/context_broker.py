from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from .contracts import AskCitation, CitationSourceType
from .policy import AskIdentity, contains_privileged_data


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
        self.transport = transport

    def collect(
        self,
        query: str,
        *,
        identity: AskIdentity,
        locale: str,
    ) -> GroundedContext:
        if not self.platform_url or not self.service_token:
            raise ContextBrokerUnavailable("Workspace context service is not configured.")

        permissions = {permission.upper() for permission in identity.permissions}
        candidates: list[dict[str, Any]] = []
        attempted: list[str] = []
        unavailable: list[str] = []
        headers = {
            "X-DWP-Service-Token": self.service_token,
            "X-DWP-User-ID": identity.user_id,
            "X-DWP-Tenant-ID": identity.tenant_id,
            "X-DWP-Roles": ",".join(identity.roles),
            "X-DWP-Permissions": ",".join(identity.permissions),
            "X-Correlation-ID": identity.correlation_id,
            "Accept-Language": locale,
            "Accept": "application/json",
        }

        with httpx.Client(transport=self.transport, timeout=3.0) as client:
            if "APP.WORK:VIEW" in permissions:
                attempted.append("WORK_ITEM")
                try:
                    response = client.get(
                        f"{self.platform_url}/v1/workspace/work-items",
                        headers=headers,
                    )
                    candidates.extend(self._work_items(response))
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    unavailable.append("WORK_ITEM")

            if "APP.MAIL:VIEW" in permissions:
                attempted.append("MAIL")
                try:
                    response = client.get(
                        f"{self.platform_url}/v1/mail/threads",
                        headers=headers,
                        params={"page": 0, "pageSize": 50},
                    )
                    candidates.extend(self._mail_threads(response))
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    unavailable.append("MAIL")

        query_tokens = _tokens(query)
        ranked = sorted(
            candidates,
            key=lambda item: (
                -_relevance(query_tokens, item["title"], item["evidence"]),
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


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in TOKEN_PATTERN.findall(value)}


def _relevance(query_tokens: set[str], title: str, evidence: str) -> int:
    candidate_tokens = _tokens(f"{title} {evidence}")
    return len(query_tokens & candidate_tokens) * 10 + min(len(candidate_tokens), 20)


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


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
