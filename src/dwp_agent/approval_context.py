from __future__ import annotations

import re
from datetime import datetime, timezone
from time import monotonic_ns
from typing import Any, Callable

import httpx

from .contracts import CitationSourceType
from .policy import contains_privileged_data, contains_prompt_injection
from .run_observability import (
    RunSourceHealthStatus,
    SourceHealthObservation,
)


MAX_EVIDENCE_TEXT = 1_200
MODEL_ALLOWED_CLASSIFICATIONS = frozenset({"PUBLIC", "INTERNAL"})


def collect_approval_context(
    client: httpx.Client,
    *,
    approval_url: str,
    headers: dict[str, str],
    permissions: set[str],
    requested_scopes: set[CitationSourceType],
    locale: str,
) -> tuple[
    list[dict[str, Any]], list[str], list[str], list[SourceHealthObservation]
]:
    candidates: list[dict[str, Any]] = []
    attempted: list[str] = []
    unavailable: list[str] = []
    health: list[SourceHealthObservation] = []
    sources: tuple[
        tuple[
            CitationSourceType,
            bool,
            str,
            dict[str, Any] | None,
            Callable[[httpx.Response], list[dict[str, Any]]],
        ],
        ...,
    ] = (
        (
            CitationSourceType.APPROVAL_TASK,
            _has_any(permissions, "ACTION.APPROVAL_TASK", "VIEW", "MANAGE"),
            "/v1/tasks",
            {"view": "INBOX", "limit": 50},
            approval_tasks,
        ),
        (
            CitationSourceType.APPROVAL_REQUEST,
            _has_any(permissions, "ACTION.APPROVAL_REQUEST", "VIEW", "MANAGE"),
            "/v1/requests",
            {"view": "SUBMITTED", "limit": 50},
            approval_requests,
        ),
        (
            CitationSourceType.APPROVAL_FORM,
            _has_any(
                permissions,
                "ACTION.APPROVAL_REQUEST",
                "VIEW",
                "CREATE",
                "MANAGE",
            ),
            "/v1/catalog/forms",
            None,
            approval_forms,
        ),
        (
            CitationSourceType.APPROVAL_OPERATION,
            _has_any(permissions, "ADMIN.APPROVAL_OPERATIONS", "VIEW", "MANAGE"),
            "/v1/admin/operations",
            None,
            lambda response: approval_operations(response, locale),
        ),
    )
    for source_type, permitted, path, params, parser in sources:
        if source_type not in requested_scopes or not permitted:
            continue
        attempted.append(source_type.value)
        started = monotonic_ns()
        try:
            response = client.get(
                f"{approval_url}{path}",
                headers=headers,
                params=params,
            )
            candidates.extend(parser(response))
            outcome = RunSourceHealthStatus.SUCCESS
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            unavailable.append(source_type.value)
            outcome = RunSourceHealthStatus.UNAVAILABLE
        health.append(SourceHealthObservation(
            source_type=source_type.value,
            status=outcome,
            observed_at=datetime.now(timezone.utc),
            latency_ms=max(0, (monotonic_ns() - started) // 1_000_000),
        ))
    return candidates, attempted, unavailable, health


def approval_tasks(response: httpx.Response) -> list[dict[str, Any]]:
    return _approval_work_items(response, CitationSourceType.APPROVAL_TASK, "taskId")


def approval_requests(response: httpx.Response) -> list[dict[str, Any]]:
    return _approval_work_items(response, CitationSourceType.APPROVAL_REQUEST, "requestId")


def _approval_work_items(
    response: httpx.Response,
    source_type: CitationSourceType,
    identifier_key: str,
) -> list[dict[str, Any]]:
    response.raise_for_status()
    items = response.json()["data"]
    if not isinstance(items, list):
        raise ValueError("Approval work response is invalid.")
    result: list[dict[str, Any]] = []
    for item in items:
        classification = _clean(item.get("dataClassification"), 32).upper()
        if classification not in MODEL_ALLOWED_CLASSIFICATIONS:
            continue
        title = _clean(item.get("title"), 300)
        if not title:
            continue
        evidence = _clean(
            " | ".join(
                str(value)
                for value in (
                    item.get("summary"),
                    f"requestNumber={item.get('requestNumber')}"
                    if item.get("requestNumber")
                    else None,
                    f"status={item.get('status')}" if item.get("status") else None,
                    f"priority={item.get('priority')}" if item.get("priority") else None,
                    f"currentStep={item.get('currentStepName') or item.get('stepName')}"
                    if item.get("currentStepName") or item.get("stepName")
                    else None,
                    f"requester={item.get('requesterName')}"
                    if item.get("requesterName")
                    else None,
                    f"dueAt={item.get('dueAt')}" if item.get("dueAt") else None,
                )
                if value
            ),
            MAX_EVIDENCE_TEXT,
        )
        if contains_privileged_data(f"{title} {evidence}"):
            continue
        if contains_prompt_injection(f"{title} {evidence}"):
            continue
        identifier = item.get(identifier_key)
        route = (
            f"/approvals/inbox?task={identifier}"
            if source_type == CitationSourceType.APPROVAL_TASK
            else f"/approvals/requests/submitted?request={identifier}"
        )
        result.append(
            {
                "sourceType": source_type,
                "sourceSystem": "DWP Approval",
                "title": title,
                "evidence": evidence or title,
                "route": route if _uuid(identifier) else None,
                "occurredAt": _datetime(item.get("submittedAt")),
                "sortTime": item.get("dueAt") or item.get("submittedAt") or "",
            }
        )
    return result


def approval_forms(response: httpx.Response) -> list[dict[str, Any]]:
    response.raise_for_status()
    items = response.json()["data"]
    if not isinstance(items, list):
        raise ValueError("Approval form response is invalid.")
    result: list[dict[str, Any]] = []
    for item in items:
        title = _clean(item.get("nameKo") or item.get("nameEn"), 300)
        if not title:
            continue
        evidence = _clean(
            " | ".join(
                str(value)
                for value in (
                    item.get("descriptionKo") or item.get("descriptionEn"),
                    f"category={item.get('categoryNameKo') or item.get('categoryNameEn')}"
                    if item.get("categoryNameKo") or item.get("categoryNameEn")
                    else None,
                    f"fields={item.get('fieldCount')}"
                    if item.get("fieldCount") is not None
                    else None,
                    f"routes={item.get('routeCount')}"
                    if item.get("routeCount") is not None
                    else None,
                )
                if value
            ),
            MAX_EVIDENCE_TEXT,
        )
        if contains_prompt_injection(f"{title} {evidence}"):
            continue
        result.append(
            {
                "sourceType": CitationSourceType.APPROVAL_FORM,
                "sourceSystem": "DWP Approval Catalog",
                "title": title,
                "evidence": evidence or title,
                "route": "/approvals/requests/new" if _uuid(item.get("formId")) else None,
                "occurredAt": _datetime(item.get("updatedAt")),
                "sortTime": item.get("updatedAt") or "",
            }
        )
    return result


def approval_operations(
    response: httpx.Response,
    locale: str,
) -> list[dict[str, Any]]:
    response.raise_for_status()
    data = response.json()["data"]
    signals = data.get("signals") if isinstance(data, dict) else None
    if not isinstance(signals, list):
        raise ValueError("Approval operation response is invalid.")
    korean = locale.lower().startswith("ko")
    return [
        {
            "sourceType": CitationSourceType.APPROVAL_OPERATION,
            "sourceSystem": "DWP Approval Operations",
            "title": _clean(
                signal.get("titleKo") if korean else signal.get("titleEn"),
                300,
            )
            or _clean(signal.get("key"), 300),
            "evidence": _clean(
                f"state={signal.get('state')} | count={signal.get('count')} | "
                f"{signal.get('detailKo') if korean else signal.get('detailEn')}",
                MAX_EVIDENCE_TEXT,
            ),
            "route": "/approvals/admin/operations",
            "occurredAt": _datetime(data.get("generatedAt")),
            "sortTime": data.get("generatedAt") or "",
        }
        for signal in signals
        if isinstance(signal, dict)
        and not contains_prompt_injection(
            f"{signal.get('titleKo')} {signal.get('titleEn')} "
            f"{signal.get('detailKo')} {signal.get('detailEn')}"
        )
    ]


def _has_any(permissions: set[str], resource: str, *actions: str) -> bool:
    return any(f"{resource}:{action}" in permissions for action in actions)


def _uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
            r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
            value,
        )
    )


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", " ").split())[:limit]


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
