from __future__ import annotations

from hashlib import sha256
from json import dumps

from admin_commands import resolve_admin_command
from contracts import (
    AdminCommandResolution,
    AgentRegistryResolution,
    PlanPreviewRequest,
    PlanPreviewResponse,
    PlanState,
    PlanStep,
    RiskTier,
)


def build_reference_plan(
    request: PlanPreviewRequest,
    *,
    tenant_id: str,
    user_id: str,
    roles: list[str],
    correlation_id: str,
    agent_registry: AgentRegistryResolution,
) -> PlanPreviewResponse:
    normalized_roles = sorted({role.strip() for role in roles if role.strip()})
    admin_change = request.admin_change
    command_definition = (
        resolve_admin_command(
            admin_change.command_key,
            admin_change.target_type,
            admin_change.parameters,
        )
        if admin_change is not None
        else None
    )
    command_resolution = (
        AdminCommandResolution(
            command_key=command_definition.command_key,
            catalog_revision=command_definition.catalog_revision,
            target_service=command_definition.target_service,
            http_method=command_definition.http_method,
            endpoint_template=command_definition.endpoint_template,
            required_permission=command_definition.required_permission,
        )
        if command_definition is not None
        else None
    )
    canonical_request = dumps(
        {
            "tenantId": tenant_id,
            "userId": user_id,
            "roles": normalized_roles,
            "agentRegistry": agent_registry.model_dump(mode="json", by_alias=True),
            "adminCommandResolution": (
                command_resolution.model_dump(mode="json", by_alias=True)
                if command_resolution is not None
                else None
            ),
            **request.model_dump(mode="json", by_alias=True),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    plan_hash = sha256(canonical_request.encode("utf-8")).hexdigest()
    digest = plan_hash[:16]
    steps = [
        PlanStep(
            id="verify-sources",
            title="Verify source permissions and freshness",
            tool="policy.check",
            description="Stop if a source is missing, stale, or outside the user scope.",
        )
    ]
    if admin_change is not None:
        steps.append(
            PlanStep(
                id="validate-admin-command",
                title="Validate scope, authority, and target revision",
                tool="admin.command.resolve",
                description=(
                    f"Require {command_definition.required_permission} and resolve the versioned "
                    f"{command_definition.target_service} service contract. Stop on tenant scope, "
                    "separation-of-duty, or expected-version mismatch."
                ),
            )
        )
    steps.extend(
        [
            PlanStep(
                id="prepare-preview",
                title=f"Prepare the {request.action} preview",
                tool=(
                    f"{command_definition.target_service}.preview"
                    if command_definition is not None
                    else "tool.preview"
                ),
                description=(
                    f"Prepare {command_definition.http_method} "
                    f"{command_definition.endpoint_template} without invoking the mutation."
                    if command_definition is not None
                    else "Build a reversible preview without changing the source system."
                ),
            ),
            PlanStep(
                id="human-gate",
                title="Wait for explicit user approval",
                tool="workflow.human-approval",
                description="A separate approved command is required before any mutation.",
            ),
        ]
    )

    return PlanPreviewResponse(
        run_id=f"run-ref-{digest}",
        audit_id=f"AUD-REF-{digest.upper()}",
        plan_hash=plan_hash,
        correlation_id=correlation_id,
        state=PlanState.REVIEW,
        risk_tier=RiskTier.L3 if admin_change is not None else RiskTier.L2,
        approval_required=True,
        mutation_allowed=False,
        summary=(
            f"Prepare a governed {admin_change.command_key} administration preview."
            if admin_change is not None
            else f"Prepare a governed {request.action} preview."
        ),
        steps=steps,
        source_references=list(request.source_references),
        reference_mode=True,
        agent_registry=agent_registry,
        admin_command=command_resolution,
    )
