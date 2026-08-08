from __future__ import annotations

from hashlib import sha256
from json import dumps

from contracts import (
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
) -> PlanPreviewResponse:
    normalized_roles = sorted({role.strip() for role in roles if role.strip()})
    canonical_request = dumps(
        {
            "tenantId": tenant_id,
            "userId": user_id,
            "roles": normalized_roles,
            **request.model_dump(mode="json", by_alias=True),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    plan_hash = sha256(canonical_request.encode("utf-8")).hexdigest()
    digest = plan_hash[:16]

    return PlanPreviewResponse(
        run_id=f"run-ref-{digest}",
        audit_id=f"AUD-REF-{digest.upper()}",
        plan_hash=plan_hash,
        correlation_id=correlation_id,
        state=PlanState.REVIEW,
        risk_tier=RiskTier.L2,
        approval_required=True,
        mutation_allowed=False,
        summary=f"Prepare a governed {request.action} preview.",
        steps=[
            PlanStep(
                id="verify-sources",
                title="Verify source permissions and freshness",
                tool="policy.check",
                description="Stop if a source is missing, stale, or outside the user scope.",
            ),
            PlanStep(
                id="prepare-preview",
                title=f"Prepare the {request.action} preview",
                tool="tool.preview",
                description="Build a reversible preview without changing the source system.",
            ),
            PlanStep(
                id="human-gate",
                title="Wait for explicit user approval",
                tool="workflow.human-approval",
                description="A separate approved command is required before any mutation.",
            ),
        ],
        source_references=list(request.source_references),
        reference_mode=True,
    )
