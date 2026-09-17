from __future__ import annotations

import os

from .artifact_collaboration_contracts import TeamArtifactCapabilities
from .artifact_collaboration_provider import ArtifactCollaborationProviderConfiguration
from .dwaion_workflow_contracts import WorkflowCapability
from .governed_domain_core import GovernedFingerprints, GovernedPayloadCodec


def artifact_collaboration_runtime_capabilities() -> TeamArtifactCapabilities:
    provider_ready = ArtifactCollaborationProviderConfiguration.from_environment().configured
    database_ready = bool(os.getenv("DWP_AGENT_DATABASE_URL", "").strip())
    security_ready = False
    if provider_ready and database_ready:
        try:
            GovernedPayloadCodec()
            GovernedFingerprints.load()
            security_ready = True
        except Exception:
            security_ready = False
    available = provider_ready and database_ready and security_ready
    if not provider_ready:
        state = "NOT_CONFIGURED"
        hint = "Configure and attest the artifact ACL broker."
    elif not database_ready:
        state = "DATABASE_NOT_CONFIGURED"
        hint = "Configure the governed artifact collaboration database."
    elif not security_ready:
        state = "SECURITY_NOT_CONFIGURED"
        hint = "Configure the governed payload encryption and fingerprint keys."
    else:
        state = "AVAILABLE"
        hint = None
    return TeamArtifactCapabilities(
        team_workspace_available=available,
        acl_preflight_available=available,
        access_request_available=available,
        collaboration_available=available,
        conflict_resolution_available=available,
        internal_sharing_available=available,
        external_sharing_available=False,
        share_expiry_available=available,
        share_revocation_available=available,
        automatic_masking=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ARTIFACT_AUTOMATIC_MASKING_NOT_CONFIGURED",
            recovery_hint=(
                "Configure an attested artifact masking provider before applying "
                "automatic redaction."
            ),
        ),
        synthetic_replacement=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ARTIFACT_SYNTHETIC_REPLACEMENT_NOT_CONFIGURED",
            recovery_hint=(
                "Configure an attested synthetic-data provider before replacing "
                "restricted values."
            ),
        ),
        review_notification=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ARTIFACT_REVIEW_NOTIFICATION_NOT_CONFIGURED",
            recovery_hint=(
                "Configure the governed review notification provider before resending "
                "a review request."
            ),
        ),
        review_rejection=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ARTIFACT_REVIEW_REJECTION_NOT_CONFIGURED",
            recovery_hint=(
                "Configure the governed review workflow provider before rejecting "
                "a submitted review."
            ),
        ),
        provider_state=state,
        recovery_hint=hint,
    )
