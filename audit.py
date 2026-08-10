from __future__ import annotations

import logging
from datetime import datetime, timezone
from json import dumps

from contracts import PlanPreviewResponse


AUDIT_LOGGER = logging.getLogger("uvicorn.error.dwp.audit")
AUDIT_LOGGER.setLevel(logging.INFO)


def record_plan_preview(
    plan: PlanPreviewResponse,
    *,
    tenant_id: str,
    user_id: str,
    role_count: int,
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
