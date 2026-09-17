from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NoReturn

from fastapi import HTTPException, Request, Response, status

from .product_surface_pep import (
    ACCESS_MODE_HEADER,
    CONTEXT_HEADER,
    CURRENT_REVALIDATE_AT_HEADER,
    CURRENT_REVISION_HEADER,
    EXPECTED_REVISION_HEADER,
    RESPONSE_REVISION_HEADER,
    ROLLOUT_COHORT_HEADER,
    ROLLOUT_REVISION_HEADER,
    ROLLOUT_STATE_HEADER,
    ROUTE_HEADER,
    SCOPE_HEADER,
    SUPPORT_SESSION_HEADER,
    resolve_binding,
    scope_key,
)
from .product_surface_pep_http import (
    exact_header,
    future_instant,
    header_tokens,
    headers,
    positive_identifier,
    present,
)


@dataclass(frozen=True)
class _SurfaceBinding:
    route_contract_key: str
    action: bool
    permission_sets: tuple[frozenset[str], ...]


_BINDINGS = {
    ("GET", "/v1/admin/ai-control"): _SurfaceBinding(
        "route.dwaion.management.ai-control.page",
        False,
        (
            frozenset({"ADMIN.DWAION_SAFETY:VIEW"}),
            frozenset({"ADMIN.DWAION_SAFETY:MANAGE"}),
        ),
    ),
    ("POST", "/v1/admin/ai-control/bootstrap"): _SurfaceBinding(
        "route.dwaion.management.ai-control-bootstrap.action",
        True,
        (
            frozenset({"ADMIN.DWAION_SAFETY:UPDATE"}),
            frozenset({"ADMIN.DWAION_SAFETY:MANAGE"}),
        ),
    ),
    ("PUT", "/v1/admin/ai-control/policy"): _SurfaceBinding(
        "route.dwaion.management.ai-control-update.action",
        True,
        (
            frozenset({"ADMIN.DWAION_SAFETY:UPDATE"}),
            frozenset({"ADMIN.DWAION_SAFETY:MANAGE"}),
        ),
    ),
    ("POST", "/v1/admin/ai-control/emergency"): _SurfaceBinding(
        "route.dwaion.management.ai-control-emergency.action",
        True,
        (frozenset({"ADMIN.DWAION_SAFETY:MANAGE"}),),
    ),
}
_CONTEXT = re.compile(r"^psc-[a-f0-9]{64}$")
_ROLLOUT_REVISION = re.compile(r"^rollout-[a-f0-9]{64}$")
_DECISION_REVISION = re.compile(r"^psr-[a-f0-9]{64}$")


def require_ai_control_product_surface(
    request: Request,
    response: Response,
) -> None:
    binding = _BINDINGS.get((request.method.upper(), request.url.path))
    if binding is None:
        _deny(status.HTTP_503_SERVICE_UNAVAILABLE, "Invalid AI control route binding.")
    values = headers(request.scope)
    _require_rollout(values)
    route = exact_header(values, ROUTE_HEADER)
    context = exact_header(values, CONTEXT_HEADER)
    selected_scope = exact_header(values, SCOPE_HEADER)
    access_mode = exact_header(values, ACCESS_MODE_HEADER)
    if (
        route != binding.route_contract_key
        or context is None
        or _CONTEXT.fullmatch(context) is None
        or selected_scope is None
        or access_mode not in {"NORMAL", "ELEVATED"}
    ):
        _deny(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Trusted AI control route, context, scope and access mode are invalid.",
        )
    if present(values, SUPPORT_SESSION_HEADER):
        _deny(
            status.HTTP_403_FORBIDDEN,
            "Provider support cannot assume tenant AI control authority.",
        )
    tenant_id = positive_identifier(exact_header(values, "X-DWP-Tenant-ID"))
    user_id = positive_identifier(exact_header(values, "X-DWP-User-ID"))
    identity_plane = exact_header(values, "X-DWP-Identity-Plane")
    roles = header_tokens(values, "X-DWP-Roles")
    permissions = header_tokens(values, "X-DWP-Permissions")
    if (
        tenant_id is None
        or user_id is None
        or identity_plane != "TENANT"
        or roles is None
        or any(role.startswith("PROVIDER_") for role in roles)
        or permissions is None
        or not any(required.issubset(permissions) for required in binding.permission_sets)
    ):
        _deny(status.HTTP_403_FORBIDDEN, "The exact tenant AI control authority is missing.")
    accepted_scopes = {
        scope_key(
            tenant_id,
            user_id,
            surface_key="dwaion.management",
            source=source,
            kind="RESOURCE_SET",
        )
        for source in ("APP_RESOURCE_SET:RS_DWAION", "RS_DWAION")
    }
    if selected_scope not in accepted_scopes:
        _deny(status.HTTP_403_FORBIDDEN, "The selected AI control scope is invalid.")
    current_revision = exact_header(values, CURRENT_REVISION_HEADER)
    revalidate_at = future_instant(exact_header(values, CURRENT_REVALIDATE_AT_HEADER))
    if (
        current_revision is None
        or _DECISION_REVISION.fullmatch(current_revision) is None
        or revalidate_at is None
    ):
        _deny(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Trusted current AI control authority is missing or expired.",
        )
    if binding.action:
        expected = exact_header(values, EXPECTED_REVISION_HEADER)
        if expected != current_revision:
            _deny(
                status.HTTP_409_CONFLICT,
                "AI control authority changed after the client decision.",
            )
    if resolve_binding(request.method.upper(), request.url.path) is None:
        response.headers[RESPONSE_REVISION_HEADER] = current_revision


def _require_rollout(values: dict[str, list[str]]) -> None:
    state = exact_header(values, ROLLOUT_STATE_HEADER)
    revision = exact_header(values, ROLLOUT_REVISION_HEADER)
    cohort = exact_header(values, ROLLOUT_COHORT_HEADER)
    if (
        state not in {"110", "111"}
        or revision is None
        or _ROLLOUT_REVISION.fullmatch(revision) is None
        or cohort
        not in {"baseline", "holdout", "full", "eligible-10", "eligible-25", "eligible-50", "eligible-90"}
    ):
        _deny(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Trusted AI control rollout evidence is missing or invalid.",
        )


def _deny(status_code: int, detail: str) -> NoReturn:
    raise HTTPException(status_code=status_code, detail=detail)
