from __future__ import annotations

from fastapi import HTTPException, status

from .ai_control_activation import ai_runtime_control_enforcement_enabled
from .ai_control_runtime import (
    AIControlConflict,
    AIControlDenied,
    AIControlPolicyNotConfigured,
    AIControlUnavailable,
    AIRuntimeControl,
)
from .ai_control_store import get_ai_control_store
from .ask_runtime import AskRuntime
from .conversation_store import ConversationNotFound
from .registry import RegistryResolutionError
from .run_store import RequestIdConflict, RunInProgress, RunStoreUnavailable


AI_CONTROL_ERRORS = (
    AIControlConflict,
    AIControlDenied,
    AIControlPolicyNotConfigured,
    AIControlUnavailable,
)


def controlled_ask_runtime() -> AskRuntime:
    if not ai_runtime_control_enforcement_enabled():
        return AskRuntime()
    try:
        return AskRuntime(ai_runtime_control=AIRuntimeControl(get_ai_control_store()))
    except AIControlUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


def ai_control_http_exception(error: Exception) -> HTTPException:
    if isinstance(error, AIControlDenied):
        code = (
            status.HTTP_429_TOO_MANY_REQUESTS
            if "BUDGET" in error.code
            else status.HTTP_403_FORBIDDEN
        )
        return HTTPException(status_code=code, detail=error.code)
    if isinstance(error, AIControlConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error))


def stream_error_code(error: Exception) -> str:
    if isinstance(error, AIControlDenied):
        return error.code
    if isinstance(error, AIControlPolicyNotConfigured):
        return "AI_RUNTIME_POLICY_NOT_CONFIGURED"
    if isinstance(error, AIControlConflict):
        return "AI_RUNTIME_POLICY_CHANGED"
    if isinstance(error, AIControlUnavailable):
        return "AI_RUNTIME_CONTROL_UNAVAILABLE"
    if isinstance(error, ConversationNotFound):
        return "CONVERSATION_NOT_FOUND"
    if isinstance(error, RegistryResolutionError):
        return "AGENT_REGISTRY_UNAVAILABLE"
    if isinstance(error, RunInProgress):
        return "RUN_IN_PROGRESS"
    if isinstance(error, RequestIdConflict):
        return "REQUEST_ID_CONFLICT"
    if isinstance(error, RunStoreUnavailable):
        return "AGENT_STORE_UNAVAILABLE"
    return "ASK_STREAM_FAILED"
