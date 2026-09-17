from __future__ import annotations

import os

from psycopg import Error as PsycopgError, connect

from .dwaion_workflow_contracts import (
    ResearchDeliveryCapabilities,
    WorkflowCapability,
)
from .governed_worker_runtime import governed_worker_available
from .personal_domain_security import PersonalDomainIdentity
from .research_downstream_provider import research_downstream_capability
from .dwaion_workflow_contracts import ResearchDeliveryType


def research_delivery_capabilities(
    database_url: str, identity: PersonalDomainIdentity
) -> ResearchDeliveryCapabilities:
    worker_configured = (
        os.getenv("DWP_GOVERNED_WORKERS_ENABLED", "false").strip().lower() == "true"
    )
    worker_live = governed_worker_available("RESEARCH_DELIVERY")
    retention_configured = _artifact_retention_configured(
        database_url, identity.tenant_id
    )

    if not retention_configured:
        artifact = _unavailable(
            "ARTIFACT_RETENTION_POLICY_REQUIRED",
            "Configure an explicit ARTIFACT retention policy before creating a governed research artifact.",
            configured=False,
        )
    else:
        artifact = _worker_capability(worker_configured, worker_live)

    worker = _worker_capability(worker_configured, worker_live)
    handoff_provider = research_downstream_capability(ResearchDeliveryType.HANDOFF)
    share_provider = research_downstream_capability(ResearchDeliveryType.SHARE)
    routine_ready = _routine_delivery_configured(
        database_url, identity.tenant_id, identity.user_id
    )
    routine = (
        worker
        if routine_ready
        else _unavailable(
            "RESEARCH_ROUTINE_DEFINITION_REQUIRED",
            "Enable the Work source and configure ROUTINE retention before registering a monthly research routine.",
        )
    )

    return ResearchDeliveryCapabilities(
        artifact=artifact,
        export=worker,
        proposal=worker,
        handoff=_combined_capability(worker, handoff_provider),
        share=_combined_capability(worker, share_provider),
        routine=routine,
    )


def _combined_capability(
    worker: WorkflowCapability, provider: WorkflowCapability
) -> WorkflowCapability:
    if not worker.available:
        return worker
    return provider


def _worker_capability(configured: bool, live: bool) -> WorkflowCapability:
    if live:
        return WorkflowCapability(available=True, configured=True)
    if configured:
        return _unavailable(
            "RESEARCH_DELIVERY_WORKER_UNAVAILABLE",
            "Restore the governed research delivery worker and retry.",
            configured=True,
        )
    return _unavailable(
        "RESEARCH_DELIVERY_WORKER_NOT_CONFIGURED",
        "Enable the governed worker runtime before requesting research delivery.",
    )


def _unavailable(
    code: str, hint: str, *, configured: bool = False
) -> WorkflowCapability:
    return WorkflowCapability(
        available=False,
        configured=configured,
        reason_code=code,
        recovery_hint=hint,
    )


def _artifact_retention_configured(database_url: str, tenant_id: int) -> bool:
    if not database_url:
        return False
    try:
        with connect(database_url) as connection:
            row = connection.execute(
                """SELECT 1 FROM ai_domain_retention_policies
                    WHERE tenant_id = %s AND domain_key = 'ARTIFACT'""",
                (tenant_id,),
            ).fetchone()
        return row is not None
    except PsycopgError:
        return False


def _routine_delivery_configured(
    database_url: str, tenant_id: int, user_id: str
) -> bool:
    if not database_url:
        return False
    try:
        with connect(database_url) as connection:
            retention = connection.execute(
                """SELECT 1 FROM ai_domain_retention_policies
                    WHERE tenant_id = %s AND domain_key = 'ROUTINE'""",
                (tenant_id,),
            ).fetchone()
            source = connection.execute(
                """SELECT 1 FROM ai_user_ai_source_preferences
                    WHERE tenant_id = %s AND user_id = %s
                      AND source_key = 'WORK_ITEM' AND enabled = TRUE""",
                (tenant_id, user_id),
            ).fetchone()
        return retention is not None and source is not None
    except PsycopgError:
        return False
