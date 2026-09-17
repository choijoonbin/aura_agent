from __future__ import annotations

import os

from .dwaion_workflow_contracts import WorkflowCapability
from .governed_worker_runtime import governed_worker_available
from .personal_routine_contracts import RoutineCapabilities
from .personal_routine_execution_provider import (
    RoutineExecutionProviderConfiguration,
)


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
        automatic_quarantine=_unsupported(
            "ROUTINE_AUTOMATIC_QUARANTINE_NOT_CONFIGURED",
            "Configure an attested quarantine policy and operator recovery workflow.",
        ),
        change_approval=_unsupported(
            "ROUTINE_CHANGE_APPROVAL_NOT_CONFIGURED",
            "Configure an independent maker-checker provider for routine definition changes.",
        ),
        agent_switching=_unsupported(
            "ROUTINE_AGENT_SWITCHING_NOT_CONFIGURED",
            "Configure an approved agent registry before switching the routine execution kernel.",
        ),
        worm_delivery=_unsupported(
            "ROUTINE_WORM_DELIVERY_NOT_CONFIGURED",
            "Configure an immutable artifact retention provider before enabling WORM delivery.",
        ),
        oauth_reauthorization=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ROUTINE_OAUTH_REAUTHORIZATION_NOT_CONFIGURED",
            recovery_hint=(
                "Configure a governed OAuth reauthorization provider before requesting "
                "credential recovery."
            ),
        ),
        temporary_budget_increase=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ROUTINE_TEMPORARY_BUDGET_PROVIDER_NOT_CONFIGURED",
            recovery_hint=(
                "Configure a maker-checker budget exception provider before requesting "
                "a temporary limit increase."
            ),
        ),
        operator_escalation=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ROUTINE_OPERATOR_ESCALATION_NOT_CONFIGURED",
            recovery_hint=(
                "Configure the governed operator escalation queue before escalating a run."
            ),
        ),
        provider_rollback=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ROUTINE_PROVIDER_ROLLBACK_NOT_CONFIGURED",
            recovery_hint=(
                "Configure an attested provider rollback adapter before restoring an "
                "external side effect."
            ),
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


def _unsupported(reason_code: str, recovery_hint: str) -> WorkflowCapability:
    return WorkflowCapability(
        available=False,
        configured=False,
        reason_code=reason_code,
        recovery_hint=recovery_hint,
    )
