from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from .governed_domain_core import GovernedDomainUnavailable
from .artifact_store import get_artifact_store
from .home_widget_contracts import (
    DwaionArtifactHomePayload,
    DwaionArtifactHomeProjection,
    HOME_WIDGET_BATCH_PATH,
    HOME_WIDGET_COMMAND_PATH,
    HOME_WIDGET_SCHEMA_VERSION,
    HomeWidgetAction,
    HomeWidgetActionKind,
    HomeWidgetBatchRequest,
    HomeWidgetBatchResponse,
    HomeWidgetRequest,
    HomeWidgetResult,
    HomeWidgetSourceState,
    HomeWidgetState,
)
from .home_widget_security import HomeWidgetRecipient, authorize_home_widget_request
from .personal_domain_security import PersonalDomainIdentity


DEFINITION_KEY = "dwaion.artifact"
SOURCE_KEY = "DWAION_HOME"
SOURCE_ROUTE = "/dwaion/artifacts"
REQUIRED_PERMISSIONS = frozenset(
    {"APP.ASK:VIEW", "APP.DWAION_ARTIFACTS:VIEW"}
)

router = APIRouter(tags=["home-widget-provider"])


@router.post(HOME_WIDGET_BATCH_PATH, response_model=HomeWidgetBatchResponse)
def batch_home_widgets(
    body: HomeWidgetBatchRequest,
    recipient: Annotated[HomeWidgetRecipient, Depends(authorize_home_widget_request)],
) -> HomeWidgetBatchResponse:
    if any(widget.definition_key != DEFINITION_KEY for widget in body.widgets):
        _request_error(
            status.HTTP_400_BAD_REQUEST,
            "HOME_PROVIDER_DEFINITION_NOT_OWNED",
            "DWAI-ON does not own a requested widget definition.",
        )
    if not REQUIRED_PERMISSIONS.issubset(recipient.permissions):
        results = [_forbidden(widget) for widget in body.widgets]
    elif os.getenv(
        "DWP_DWAION_HOME_TITLE_PROJECTION_READY", "false"
    ).strip().lower() != "true":
        results = [
            _unavailable(
                widget,
                reason_code="PROVIDER_DWAION_ARTIFACT_PROJECTION_NOT_ACTIVATED",
            )
            for widget in body.widgets
        ]
    else:
        try:
            identity = PersonalDomainIdentity(
                tenant_id=recipient.tenant_id,
                user_id=str(recipient.user_id),
                correlation_id=f"home:{recipient.authority_decision_revision}",
                auth_session_id=f"home:{recipient.authority_decision_revision}",
                roles=recipient.roles,
                permissions=recipient.permissions,
            )
            projection = get_artifact_store().home_projection(
                identity,
                limit=max(widget.item_limit for widget in body.widgets),
            )
            recipient.current()
            results = [
                _result(widget, projection)
                for widget in body.widgets
            ]
        except GovernedDomainUnavailable:
            results = [_unavailable(widget) for widget in body.widgets]
    recipient.current()
    return HomeWidgetBatchResponse(
        tenant_id=recipient.tenant_id,
        user_id=recipient.user_id,
        authority_decision_revision=recipient.authority_decision_revision,
        results=results,
    )


@router.post(HOME_WIDGET_COMMAND_PATH)
def reject_home_widget_commands(
    _: Annotated[HomeWidgetRecipient, Depends(authorize_home_widget_request)],
) -> None:
    _request_error(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "HOME_PROVIDER_COMMAND_NOT_DECLARED",
        "The DWAI-ON Home widget exposes a source route only.",
    )


def _result(
    widget: HomeWidgetRequest,
    projection: DwaionArtifactHomeProjection,
) -> HomeWidgetResult:
    if projection.visible_count == 0:
        return _empty(widget)
    prefix = projection.slots[: widget.item_limit]
    items = [item for item in prefix if item is not None]
    unreadable_count = len(prefix) - len(items)
    if unreadable_count and not items:
        return _unavailable(widget, reason_code="PROVIDER_DWAION_ARTIFACT_TITLE_UNREADABLE")
    payload_model = DwaionArtifactHomePayload(
        visible_count=projection.visible_count,
        items=items,
    )
    payload = payload_model.model_dump(mode="json", by_alias=True)
    now = datetime.now(timezone.utc)
    version = _result_version(widget, payload)
    return HomeWidgetResult(
        instance_id=widget.instance_id,
        definition_key=widget.definition_key,
        definition_manifest_hash=widget.definition_manifest_hash,
        renderer_binding_revision=widget.renderer_binding_revision,
        state=(
            HomeWidgetState.PARTIAL
            if unreadable_count
            else HomeWidgetState.AVAILABLE
        ),
        source=_source(
            now,
            reason_code=(
                "PROVIDER_DWAION_ARTIFACT_TITLE_PARTIAL"
                if unreadable_count
                else None
            ),
            result_version=version,
            last_success_at=now,
        ),
        payload=payload,
        actions=[_source_action()],
        redactions=(
            ["ARTIFACT_TITLE_PROJECTION_UNREADABLE"]
            if unreadable_count
            else []
        ),
    )


def _empty(widget: HomeWidgetRequest) -> HomeWidgetResult:
    now = datetime.now(timezone.utc)
    return HomeWidgetResult(
        instance_id=widget.instance_id,
        definition_key=widget.definition_key,
        definition_manifest_hash=widget.definition_manifest_hash,
        renderer_binding_revision=widget.renderer_binding_revision,
        state=HomeWidgetState.EMPTY,
        source=_source(
            now,
            result_version=_result_version(widget, {}),
            last_success_at=now,
        ),
        payload={},
        actions=[_source_action()],
    )


def _forbidden(widget: HomeWidgetRequest) -> HomeWidgetResult:
    now = datetime.now(timezone.utc)
    return HomeWidgetResult(
        instance_id=widget.instance_id,
        definition_key=widget.definition_key,
        definition_manifest_hash=widget.definition_manifest_hash,
        renderer_binding_revision=widget.renderer_binding_revision,
        state=HomeWidgetState.FORBIDDEN,
        source=_source(now, reason_code="AUTHORIZATION_DWAION_ARTIFACT_REQUIRED"),
        payload={},
        actions=[],
    )


def _unavailable(
    widget: HomeWidgetRequest,
    *,
    reason_code: str = "PROVIDER_DWAION_ARTIFACT_PROJECTION_UNAVAILABLE",
) -> HomeWidgetResult:
    now = datetime.now(timezone.utc)
    return HomeWidgetResult(
        instance_id=widget.instance_id,
        definition_key=widget.definition_key,
        definition_manifest_hash=widget.definition_manifest_hash,
        renderer_binding_revision=widget.renderer_binding_revision,
        state=HomeWidgetState.UNAVAILABLE,
        source=_source(
            now,
            reason_code=reason_code,
            retryable=True,
        ),
        payload={},
        actions=[],
    )


def _source(
    now: datetime,
    *,
    reason_code: str | None = None,
    retryable: bool = False,
    result_version: str | None = None,
    last_success_at: datetime | None = None,
) -> HomeWidgetSourceState:
    return HomeWidgetSourceState(
        source_key=SOURCE_KEY,
        generated_at=now,
        expires_at=now + timedelta(seconds=30),
        last_success_at=last_success_at,
        reason_code=reason_code,
        retryable=retryable,
        result_version=result_version,
    )


def _source_action() -> HomeWidgetAction:
    return HomeWidgetAction(
        action_id="open-source",
        label_key="home.action.openSource",
        kind=HomeWidgetActionKind.SOURCE_ROUTE,
        source_route=SOURCE_ROUTE,
    )


def _result_version(widget: HomeWidgetRequest, payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    prefix = (
        f"{widget.definition_manifest_hash}\n{widget.renderer_binding_revision}\n"
    ).encode("utf-8")
    return f"v1:{hashlib.sha256(prefix + canonical).hexdigest()[:32]}"


def _request_error(http_status: int, reason_code: str, message: str) -> None:
    raise HTTPException(
        status_code=http_status,
        detail={"reasonCode": reason_code, "message": message},
    )
