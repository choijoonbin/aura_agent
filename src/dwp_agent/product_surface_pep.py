from __future__ import annotations

import hashlib
import os
import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import FastAPI, Header
from .product_surface_pep_bindings import ROUTE_BINDING_SPECS
from .product_surface_pep_http import (
    AsgiReceive,
    AsgiSend,
    exact_header as _exact_header,
    future_instant as _future_instant,
    header_tokens as _header_tokens,
    headers as _headers,
    positive_identifier as _positive_identifier,
    present as _present,
    reject as _reject,
)


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
ASK_STREAM_ROUTE = "route.dwaion.work.ask-stream.action"
CONVERSATION_RENAME_ROUTE = "route.dwaion.work.conversation-rename.action"
CONVERSATION_DELETE_ROUTE = "route.dwaion.work.conversation-delete.action"
RUNS_ROUTE = "route.dwaion.work.runs.data"
RUN_DETAIL_ROUTE = "route.dwaion.work.run-detail.data"
ACTIVITY_EVENTS_ROUTE = "route.dwaion.work.activity-events.data"
ACTIVITY_EVENT_ROUTE = "route.dwaion.work.activity-event.data"
ACTIVITY_SUMMARY_ROUTE = "route.dwaion.work.activity-summary.data"

_ROLLOUT_STATES = {"000", "100", "110", "111"}
_ROLLOUT_COHORTS = {
    "baseline", "holdout", "full", "eligible-10", "eligible-25", "eligible-50", "eligible-90"
}
_WORK_ACCESS_MODES = {"NORMAL", "ELEVATED"}
_CONTEXT = re.compile(r"^psc-[a-f0-9]{64}$")
_ROLLOUT_REVISION = re.compile(r"^rollout-[a-f0-9]{64}$")
_DECISION_REVISION = re.compile(r"^psr-[a-f0-9]{64}$")
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

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
        any_version_enabled = _v4_enabled() or _v5_enabled() or _v6_enabled()
        if (
            state is None
            and not any_version_enabled
            and not _present(headers, ROLLOUT_STATE_HEADER)
        ):
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
        binding = resolve_binding(method, path)
        if binding is None:
            await _reject(
                scope,
                receive,
                send,
                503,
                "Trusted DWAI route binding is invalid.",
            )
            return
        if not _enabled_for(binding):
            await _reject(
                scope,
                receive,
                send,
                503,
                f"DWAI product authorization v{binding.introduced_version} "
                "is not ready for enforcement.",
            )
            return

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
            or not any(
                required.issubset(permissions)
                for required in binding.required_permission_sets
            )
        ):
            await _reject(scope, receive, send, 403, "The exact DWAI authority is missing.")
            return
        if not binding.accepts_scope(selected_scope, tenant_id, user_id):
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
    def __init__(
        self,
        route_contract_key: str,
        route_kind: str,
        required_permission_sets: tuple[frozenset[str], ...] = (
            frozenset({"APP.ASK:VIEW"}),
        ),
        introduced_version: int = 4,
        surface_key: str = SURFACE_KEY,
        scope_kind: str = "SELF",
    ) -> None:
        self.route_contract_key = route_contract_key
        self.route_kind = route_kind
        self.required_permission_sets = required_permission_sets
        self.introduced_version = introduced_version
        self.surface_key = surface_key
        self.scope_kind = scope_kind

    def accepts_scope(
        self,
        selected_scope: str,
        tenant_id: int,
        user_id: int,
    ) -> bool:
        if self.scope_kind == "SELF":
            return selected_scope == self_scope_key(
                tenant_id,
                user_id,
                surface_key=self.surface_key,
            )
        return selected_scope in {
            scope_key(
                tenant_id,
                user_id,
                surface_key=self.surface_key,
                source=source,
                kind="RESOURCE_SET",
            )
            for source in ("APP_RESOURCE_SET:RS_DWAION", "RS_DWAION")
        }


class BindingPattern:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.method = str(spec["method"])
        self.path_template = str(spec["path_template"])
        self.parameter_names = tuple(re.findall(r"\{([^{}]+)\}", self.path_template))
        expression = re.escape(self.path_template)
        for name in self.parameter_names:
            expression = expression.replace(re.escape("{" + name + "}"), "([^/]+)")
        self.candidate = re.compile("^" + expression + "$")
        self.validators = dict(spec["parameter_validators"])
        self.binding = Binding(
            str(spec["route_contract_key"]),
            str(spec["route_kind"]),
            tuple(
                frozenset(values)
                for values in spec["required_permission_sets"]
            ),
            int(spec["introduced_version"]),
            str(spec["surface_key"]),
            str(spec["scope_kind"]),
        )

    def match(self, method: str, path: str) -> re.Match[str] | None:
        if method != self.method:
            return None
        return self.candidate.fullmatch(path)

    def parameters_are_valid(self, match: re.Match[str]) -> bool:
        return all(
            _valid_path_parameter(
                match.group(index + 1),
                self.validators.get(name, "segment"),
            )
            for index, name in enumerate(self.parameter_names)
        )


_BINDING_PATTERNS = tuple(BindingPattern(spec) for spec in ROUTE_BINDING_SPECS)


def install_product_surface_pep(app: FastAPI) -> None:
    app.add_middleware(ProductSurfacePepMiddleware)


def install_product_surface_openapi_contract(app: FastAPI) -> None:
    """Document the optimistic authority revision for every governed ACTION."""

    original_openapi = app.openapi

    def governed_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original_openapi()
        paths = schema.get("paths", {})
        for pattern in _BINDING_PATTERNS:
            if pattern.binding.route_kind != "ACTION":
                continue
            candidates = [
                path
                for path in paths
                if _path_shape(path) == _path_shape(pattern.path_template)
            ]
            if len(candidates) != 1:
                raise RuntimeError(
                    "Governed DWAI OpenAPI ACTION binding did not resolve exactly once: "
                    f"{pattern.method} {pattern.path_template}"
                )
            operation = paths[candidates[0]].get(pattern.method.lower())
            if not isinstance(operation, dict):
                raise RuntimeError(
                    "Governed DWAI OpenAPI ACTION method is missing: "
                    f"{pattern.method} {pattern.path_template}"
                )
            parameters = operation.setdefault("parameters", [])
            matches = [
                parameter
                for parameter in parameters
                if parameter.get("in") == "header"
                and parameter.get("name") == EXPECTED_REVISION_HEADER
            ]
            if not matches:
                parameters.append(_expected_revision_openapi_parameter())
            elif len(matches) != 1:
                raise RuntimeError(
                    "Governed DWAI OpenAPI revision header is duplicated: "
                    f"{pattern.method} {pattern.path_template}"
                )
        app.openapi_schema = schema
        return schema

    app.openapi = governed_openapi


def document_expected_decision_revision(
    expected_revision: Annotated[
        str | None,
        Header(
            alias=EXPECTED_REVISION_HEADER,
            min_length=1,
            max_length=200,
            description=(
                "Required for product-authorization rollout states 110/111. The gateway "
                "rejects a missing or stale value before the state-changing request reaches "
                "the Agent owner service; rollout states 000/100 ignore it."
            ),
        ),
    ] = None,
) -> None:
    """Expose the gateway-consumed optimistic authority revision in OpenAPI."""
    del expected_revision


def _expected_revision_openapi_parameter() -> dict[str, Any]:
    return {
        "name": EXPECTED_REVISION_HEADER,
        "in": "header",
        "required": False,
        "description": (
            "Required for product-authorization rollout states 110/111. The gateway "
            "rejects a missing or stale value before the state-changing request reaches "
            "the Agent owner service; rollout states 000/100 ignore it."
        ),
        "schema": {
            "anyOf": [
                {"type": "string", "minLength": 1, "maxLength": 200},
                {"type": "null"},
            ],
            "title": "X-Dwp-Expected-Decision-Revision",
        },
    }


def _path_shape(path: str) -> str:
    return re.sub(r"\{[^{}]+\}", "{}", path)


def owns_candidate(method: str, path: str) -> bool:
    return any(pattern.match(method, path) is not None for pattern in _BINDING_PATTERNS)


def resolve_binding(method: str, path: str) -> Binding | None:
    for pattern in _BINDING_PATTERNS:
        match = pattern.match(method, path)
        if match is None:
            continue
        return pattern.binding if pattern.parameters_are_valid(match) else None
    return None


def self_scope_key(
    tenant_id: int,
    user_id: int,
    *,
    surface_key: str = SURFACE_KEY,
) -> str:
    return scope_key(
        tenant_id,
        user_id,
        surface_key=surface_key,
        source="SELF",
        kind="SELF",
    )


def scope_key(
    tenant_id: int,
    user_id: int,
    *,
    surface_key: str,
    source: str,
    kind: str,
) -> str:
    material = f"{tenant_id}\n{user_id}\n{PRODUCT_KEY}\n{surface_key}\n{source}\n{kind}".encode()
    return "scope-" + hashlib.sha256(material).hexdigest()[:32]


def _valid_path_parameter(value: str, validator: str) -> bool:
    if validator == "uuid":
        if _UUID.fullmatch(value) is None:
            return False
        try:
            UUID(value)
        except ValueError:
            return False
        return True
    if validator == "positive-int":
        return re.fullmatch(r"[1-9][0-9]*", value) is not None
    if validator == "key":
        return re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", value) is not None
    return bool(value and len(value) <= 200)


def _flag_enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def _v4_enabled() -> bool:
    return _flag_enabled("DWP_AGENT_PRODUCT_AUTHORIZATION_V4_ENABLED")


def _v5_enabled() -> bool:
    return _flag_enabled("DWP_AGENT_PRODUCT_AUTHORIZATION_V5_ENABLED")


def _v6_enabled() -> bool:
    return _flag_enabled("DWP_AGENT_PRODUCT_AUTHORIZATION_V6_ENABLED")


def _enabled_for(binding: Binding) -> bool:
    if binding.introduced_version == 6:
        return _v6_enabled()
    if binding.introduced_version == 5:
        return _v5_enabled() or _v6_enabled()
    return _v4_enabled() or _v5_enabled() or _v6_enabled()
