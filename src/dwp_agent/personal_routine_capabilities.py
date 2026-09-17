from __future__ import annotations

import os

from .dwaion_workflow_contracts import WorkflowCapability
from .governed_worker_runtime import governed_worker_available
from .personal_routine_contracts import RoutineCapabilities
from .personal_routine_execution_provider import (
    RoutineExecutionProviderConfiguration,
)
from .personal_routine_advanced_contracts import RoutineAdvancedCommandKind
from .personal_routine_advanced_provider import routine_advanced_capability


def routine_runtime_capabilities() -> RoutineCapabilities:
    configuration = RoutineExecutionProviderConfiguration.from_environment()
    configured = configuration.configured
    worker_enabled = (
        os.getenv("DWP_ROUTINE_EXECUTION_WORKER_ENABLED", "false").strip().lower()
        == "true"
    )
    worker_alive = governed_worker_available("ROUTINE_EXECUTION")
    available = configured and worker_enabled and worker_alive
    if not configured:
        state = "NOT_CONFIGURED"
        hint = "Configure and attest the governed routine execution broker."
    elif not worker_enabled:
        state = "WORKER_DISABLED"
        hint = "Enable the governed routine execution worker."
    elif not worker_alive:
        state = "WORKER_UNAVAILABLE"
        hint = "Start or recover the governed routine execution worker."
    else:
        state = "AVAILABLE"
        hint = None
    return RoutineCapabilities(
        activation_available=available,
        scheduling_available=available,
        webhook_trigger_available=available,
        background_execution_available=available,
        dry_run_available=True,
        pause_resume_available=True,
        one_time_schedule_available=available,
        active_window_preview_available=True,
        quiet_hours_preview_available=True,
        quiet_hours_delivery_enforcement_available=available,
        cost_budget_available=available,
        runtime_budget_available=available,
        notification_delivery_available=available,
        proposal_delivery_available=available,
        external_write_available=False,
        agent_kernel_binding=_supported(
            "The governed Deep Research routine kernel is bound server-side."
        ),
        whitelisted_source_binding=_supported(
            "Routine sources are restricted to permission-checked bindings."
        ),
        blocked_source_policy=_supported(
            "Unauthorized or user-disabled sources are rejected before execution."
        ),
        zero_write_policy=_supported(
            "Routine execution can create approval-gated proposals and performs no direct write."
        ),
        semantic_version_diff=_supported(
            "Immutable encrypted routine revisions and integrity fingerprints are available."
        ),
        runtime_budget_retry=WorkflowCapability(
            available=available,
            configured=configured,
            reason_code=None if available else "ROUTINE_RUNTIME_GOVERNANCE_UNAVAILABLE",
            recovery_hint=None if available else hint,
        ),
        automatic_quarantine=_supported(
            "The governed worker atomically pauses and versions the exact routine revision "
            "when its bounded retry policy is exhausted."
        ),
        change_approval=routine_advanced_capability(
            RoutineAdvancedCommandKind.CHANGE_APPROVAL
        ),
        agent_switching=routine_advanced_capability(
            RoutineAdvancedCommandKind.AGENT_ENGINE_SWITCH
        ),
        worm_delivery=routine_advanced_capability(
            RoutineAdvancedCommandKind.WORM_EVIDENCE_DELIVERY
        ),
        oauth_reauthorization=routine_advanced_capability(
            RoutineAdvancedCommandKind.OAUTH_REAUTHORIZATION
        ),
        temporary_budget_increase=routine_advanced_capability(
            RoutineAdvancedCommandKind.TEMPORARY_BUDGET_INCREASE
        ),
        operator_escalation=routine_advanced_capability(
            RoutineAdvancedCommandKind.OPERATOR_ESCALATION
        ),
        provider_rollback=routine_advanced_capability(
            RoutineAdvancedCommandKind.PROVIDER_ROLLBACK
        ),
        execution_provider_state=state,
        recovery_hint=hint,
    )


def _supported(evidence: str) -> WorkflowCapability:
    return WorkflowCapability(
        available=True,
        configured=True,
        reason_code=None,
        recovery_hint=evidence,
    )
