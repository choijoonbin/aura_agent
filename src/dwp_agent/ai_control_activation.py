from __future__ import annotations

import os

from .ai_control_contracts import EnforcementActivationState


ENVIRONMENT_KEY = "DWP_AI_RUNTIME_CONTROL_ENFORCEMENT_ENABLED"


def ai_runtime_control_enforcement_enabled() -> bool:
    return os.getenv(ENVIRONMENT_KEY, "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def ai_runtime_control_activation_state() -> EnforcementActivationState:
    return (
        EnforcementActivationState.ENABLED
        if ai_runtime_control_enforcement_enabled()
        else EnforcementActivationState.DISABLED
    )
