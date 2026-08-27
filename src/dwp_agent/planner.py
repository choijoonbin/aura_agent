from __future__ import annotations

from hashlib import sha256
from json import dumps

from .admin_authority import required_admin_preflight_authorities
from .admin_commands import resolve_admin_command
from .contracts import (
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
    identity_plane: str = "TENANT",
    resource_roles: list[str] | None = None,
) -> PlanPreviewResponse:
    normalized_roles = sorted({role.strip() for role in roles if role.strip()})
    normalized_resource_roles = sorted(
        {role.strip() for role in (resource_roles or []) if role.strip()}
    )
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
            authority_kind=command_definition.authority_kind,
            identity_plane=command_definition.identity_plane,
            required_roles=sorted(command_definition.required_roles),
            required_authorities=list(
                required_admin_preflight_authorities(
                    command_definition,
                    admin_change.parameters,
                )
            ),
            body_parameters=sorted(command_definition.resolved_body_parameters()),
            query_parameters=sorted(command_definition.query_parameters),
            header_parameters=dict(sorted(command_definition.header_parameters.items())),
            context_parameters=sorted(command_definition.context_parameters),
            final_authority_service=command_definition.target_service,
        )
        if command_definition is not None
        else None
    )
    canonical_request = dumps(
        {
            "tenantId": tenant_id,
            "userId": user_id,
            "roles": normalized_roles,
            "resourceRoles": normalized_resource_roles,
            "identityPlane": identity_plane,
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
        authority_candidates = " or ".join(command_resolution.required_authorities)
        steps.append(
            PlanStep(
                id="validate-admin-command",
                title="Validate scope, authority, and target revision",
                tool="admin.command.resolve",
                description=(
                    f"Preflight Gateway-verified authority candidate {authority_candidates} and "
                    f"resolve the versioned {command_definition.target_service} service contract. "
                    f"The {command_definition.target_service} service remains the final authority "
                    "and must re-check target scope, lifecycle, separation of duties, and version."
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
        handoff_origin=request.handoff_origin,
    )
