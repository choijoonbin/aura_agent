from __future__ import annotations

import json
from urllib.parse import quote

import httpx

from .contracts import AskCitation, AskSelectedWork, CitationSourceType
from .grounded_context import GroundedContext, GroundedSource
from .policy import AskIdentity, contains_privileged_data, contains_prompt_injection
from .workspace_authorization import WorkspaceRequestAuthorization


# Only exact owner reads are allowed. Native workspace rows have no single-item read yet.
_OWNERS = {
    "PERSONAL_TASK": ("APP.WORK", "/api/platform/v1/workspace/work-hub/personal-tasks", "taskId"),
    "SERVICE_REQUEST": ("APP.EMPLOYEE_SERVICES", "/api/platform/v1/services/requests", "requestId"),
    "APPROVAL_TASK": ("APP.APPROVALS", "/api/approvals/v1/tasks", "taskId"),
    "APPROVAL_REQUEST": ("APP.APPROVALS", "/api/approvals/v1/requests", "requestId"),
}


def unavailable(code: str) -> GroundedContext:
    return GroundedContext(
        sources=(), attempted_sources=("SELECTED_WORK",),
        unavailable_sources=("SELECTED_WORK",), status_code=f"SELECTED_WORK_{code}",
    )


def collect_selected_work(
    selection: AskSelectedWork, *, identity: AskIdentity, locale: str,
    gateway_url: str, authorization: WorkspaceRequestAuthorization | None,
    transport: httpx.BaseTransport | None = None,
) -> GroundedContext:
    owner = _OWNERS.get(selection.source_system)
    if owner is None:
        return unavailable("UNSUPPORTED")
    app, base, id_field = owner
    required = {"APP.ASK:VIEW", f"{app}:VIEW"}
    if not required.issubset({value.upper() for value in identity.permissions}):
        return unavailable("FORBIDDEN")
    if (not gateway_url or authorization is None or not authorization.available
            or not authorization.session_family_id):
        return unavailable("AUTHORIZATION_REQUIRED")
    headers = {
        **authorization.outbound_headers(), "X-Tenant-ID": identity.tenant_id,
        "X-Correlation-ID": identity.correlation_id, "Accept-Language": locale,
        "Accept": "application/json",
    }
    source_id = str(selection.source_reference)
    suffix = "/detail" if selection.source_system == "APPROVAL_REQUEST" else ""
    try:
        with httpx.Client(transport=transport, timeout=3.0, follow_redirects=False) as client:
            # Re-resolve the original session; never forward service tokens or asserted permissions.
            me = _data(client.get(
                f"{gateway_url}/api/auth/me", headers=headers,
                params={"permissionPrefix": f"APP.ASK,{app}"},
            ))
            if not _authorized(me, identity, required, authorization.session_family_id):
                return unavailable("FORBIDDEN")
            data = _data(client.get(f"{gateway_url}{base}/{source_id}{suffix}", headers=headers))
        record = data
        if selection.source_system == "APPROVAL_TASK":
            record = data["task"]
        elif selection.source_system in {"APPROVAL_REQUEST", "SERVICE_REQUEST"}:
            record = data["request"]
        if not isinstance(record, dict) or record.get(id_field) != source_id:
            return unavailable("INVALID_SOURCE")
        version = record.get("version")
        if type(version) is not int or version != selection.expected_version:
            return unavailable("STALE")
        if selection.source_system == "APPROVAL_TASK" and (
            not selection.obligation_key or record.get("stepKey") != selection.obligation_key
        ):
            return unavailable("STALE")
        classification = (
            "INTERNAL" if selection.source_system == "PERSONAL_TASK"
            else data.get("dataClassification", record.get("dataClassification"))
        )
        if classification not in {"PUBLIC", "INTERNAL"}:
            return unavailable("RESTRICTED")
        return _context(selection, record)
    except httpx.HTTPStatusError as error:
        code = {401: "FORBIDDEN", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "STALE"}.get(
            error.response.status_code, "UNAVAILABLE"
        )
        return unavailable(code)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return unavailable("UNAVAILABLE")


def _data(response: httpx.Response) -> dict:
    response.raise_for_status()
    data = response.json()["data"]
    if not isinstance(data, dict):
        raise ValueError("Invalid owner response")
    return data


def _authorized(me: dict, identity: AskIdentity, required: set[str], session_family_id: str) -> bool:
    if (
        str(me.get("tenantId")) != identity.tenant_id
        or str(me.get("userId")) != identity.user_id
        or me.get("identityPlane") != "TENANT"
        or me.get("sessionFamilyId") != session_family_id
    ):
        return False
    grants: set[str] = set()
    denies: set[str] = set()
    for permission in me.get("permissions", []):
        if not isinstance(permission, dict):
            return False
        key = f"{permission.get('resourceKey', '')}:{permission.get('permissionCode', '')}".upper()
        if permission.get("effect") == "ALLOW":
            grants.add(key)
        elif permission.get("effect") == "DENY":
            denies.add(key)
    return required.issubset(grants - denies)


def _context(selection: AskSelectedWork, record: dict) -> GroundedContext:
    # Owner metadata is evidence only of these fields, never of unseen document bodies or policy.
    title = _text(record.get("title") or record.get("summary"), 300)
    fields = {
        "basis": "OWNER_METADATA_ONLY; original document, attachments and policy not supplied",
        "sourceVersion": selection.expected_version,
        "status": _text(record.get("status"), 80),
        "title": title,
        "summary": _text(record.get("summary") or record.get("description"), 800),
        "dueAt": _text(record.get("dueAt") or record.get("slaDueAt"), 60),
    }
    if not title or not fields["status"]:
        return unavailable("INVALID_SOURCE")
    evidence = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    if contains_privileged_data(evidence) or contains_prompt_injection(evidence):
        return unavailable("RESTRICTED")
    key = ":".join(quote(value, safe="") for value in (
        selection.source_system, str(selection.source_reference), selection.obligation_key or "",
    ))
    source_type = (
        CitationSourceType(selection.source_system)
        if selection.source_system.startswith("APPROVAL_") else CitationSourceType.WORK_ITEM
    )
    return GroundedContext(
        sources=(GroundedSource(
            citation=AskCitation(
                source_id="src-01", source_type=source_type, title=title,
                source_system=f"{selection.source_system} / owner metadata v{selection.expected_version}",
                route=f"/work/queue?work={quote(key, safe='')}", excerpt=evidence[:500],
            ), evidence=evidence, rank=1,
        ),), attempted_sources=("SELECTED_WORK",), unavailable_sources=(),
    )


def _text(value: object, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""
