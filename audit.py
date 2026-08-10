from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from json import dumps
from uuid import NAMESPACE_URL, uuid5

from contracts import PlanPreviewResponse
from audit_delivery import AUDIT_PUBLISHER


AUDIT_LOGGER = logging.getLogger("uvicorn.error.dwp.audit")
AUDIT_LOGGER.setLevel(logging.INFO)


def record_plan_preview(
    plan: PlanPreviewResponse,
    *,
    tenant_id: str,
    user_id: str,
    role_count: int,
    roles: list[str] | None = None,
) -> None:
    event = {
        "id": plan.audit_id,
        "source": "urn:dwp:agent-runtime",
        "type": "agent.plan.previewed",
        "specVersion": "1.0",
        "time": datetime.now(timezone.utc).isoformat(),
        "subject": f"plan/{plan.run_id}",
        "tenantId": tenant_id,
        "correlationId": plan.correlation_id,
        "schemaVersion": "1.0",
        "classification": "INTERNAL",
        "data": {
            "userId": user_id,
            "planHash": plan.plan_hash,
            "riskTier": plan.risk_tier,
            "approvalRequired": plan.approval_required,
            "mutationAllowed": plan.mutation_allowed,
            "referenceMode": plan.reference_mode,
            "agentKey": plan.agent_registry.entry_key,
            "agentRevision": plan.agent_registry.revision,
            "registryResolution": plan.agent_registry.resolution,
            "adminCommandKey": (
                plan.admin_command.command_key if plan.admin_command is not None else None
            ),
            "adminCommandRevision": (
                plan.admin_command.catalog_revision if plan.admin_command is not None else None
            ),
            "targetService": (
                plan.admin_command.target_service if plan.admin_command is not None else None
            ),
            "roleCount": role_count,
            "sourceCount": len(plan.source_references),
        },
    }
    AUDIT_LOGGER.info(dumps(event, ensure_ascii=True, separators=(",", ":")))
    if not AUDIT_PUBLISHER.enabled:
        return
    try:
        numeric_tenant_id = int(tenant_id)
    except ValueError:
        AUDIT_LOGGER.error("Audit delivery skipped because tenant identity is not numeric")
        return
    normalized_roles = sorted({role.strip() for role in (roles or []) if role.strip()})
    risk_score = {"L1": 25, "L2": 55, "L3": 85}.get(plan.risk_tier, 40)
    severity = "HIGH" if risk_score >= 70 else "MEDIUM" if risk_score >= 45 else "LOW"
    AUDIT_PUBLISHER.publish(
        {
            "eventId": str(uuid5(NAMESPACE_URL, f"urn:dwp:audit:{plan.audit_id}")),
            "eventVersion": "1.0",
            "occurredAt": datetime.now(timezone.utc).isoformat(),
            "tenantId": numeric_tenant_id,
            "category": "AI_ACTION",
            "action": "agent.plan.previewed",
            "outcome": "SUCCESS",
            "severity": severity,
            "riskScore": risk_score,
            "actorType": "USER",
            "actorId": user_id,
            "actorRoles": normalized_roles,
            "sourceService": "dwp-agent-runtime",
            "sourceModule": "reference-planner",
            "sourceInstance": os.getenv("HOSTNAME", "local"),
            "environment": os.getenv("DWP_ENVIRONMENT", "local"),
            "targetType": "AGENT_PLAN",
            "targetId": plan.run_id,
            "targetDisplayName": plan.agent_registry.entry_key,
            "correlationId": plan.correlation_id,
            "approvalId": None,
            "beforeState": {},
            "afterState": {},
            "metadata": {
                "planHash": plan.plan_hash,
                "riskTier": plan.risk_tier,
                "approvalRequired": plan.approval_required,
                "mutationAllowed": plan.mutation_allowed,
                "referenceMode": plan.reference_mode,
                "agentRevision": plan.agent_registry.revision,
                "adminCommandKey": (
                    plan.admin_command.command_key if plan.admin_command is not None else None
                ),
                "targetService": (
                    plan.admin_command.target_service if plan.admin_command is not None else None
                ),
                "roleCount": role_count,
                "sourceCount": len(plan.source_references),
            },
            "retentionClass": "EXTENDED",
        }
    )
