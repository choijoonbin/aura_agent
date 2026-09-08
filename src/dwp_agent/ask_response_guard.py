from __future__ import annotations

import hashlib
import hmac
import os

from .contracts import AskRequest, AskResponse, AskState
from .model_gateway import ModelConfigurationRequired
from .policy import AskIdentity
from .workspace_authorization import WorkspaceRequestAuthorization


def selected_response_guard(
    broker, request: AskRequest, identity: AskIdentity, response: AskResponse,
    authorization: WorkspaceRequestAuthorization | None,
) -> AskResponse:
    if request.page_context is None or request.page_context.selected_work is None:
        return response
    if response.state != AskState.COMPLETED:
        return response
    current = broker.collect(
        request.query, identity=identity, locale=request.locale, agent_key=request.agent_key,
        source_scopes=request.source_scopes, page_context=request.page_context,
        workspace_authorization=authorization,
    )
    if not current.sources:
        return response.model_copy(update={
            "state": AskState.ABSTAINED, "answer": None, "confidence": None,
            "citations": [], "source_count": 0,
            "status_code": current.status_code or "SELECTED_WORK_UNAVAILABLE",
        })
    return response


def safety_identifier(identity: AskIdentity) -> str:
    secret = os.getenv("DWP_AGENT_SAFETY_SECRET", "").strip()
    if not secret:
        secret = os.getenv("DWP_AGENT_PRIVACY_HASH_SECRET", "").strip()
    if not secret:
        raise ModelConfigurationRequired("Agent safety identifier secret is required.")
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{identity.tenant_id}:{identity.user_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"dwp_{digest[:48]}"


def model_provider_label(model_gateway: object) -> str:
    provider = getattr(model_gateway, "provider_label", "OPENAI")
    return str(provider).strip().upper()[:40] or "OPENAI"


def safe_status_code(error: Exception) -> str:
    value = str(error).strip().upper() or type(error).__name__.upper()
    safe = "".join(character if character.isalnum() or character in "_.-" else "_" for character in value)
    return safe[:120]
