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
    if database_ready:
        try:
            GovernedPayloadCodec()
            GovernedFingerprints.load()
            security_ready = True
        except Exception:
            security_ready = False
    available = provider_ready and database_ready and security_ready
    local_remediation_available = database_ready and security_ready
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
        inline_comments=WorkflowCapability(
            available=available,
            configured=available,
            reason_code=(
                None if available else "ARTIFACT_INLINE_COMMENTS_NOT_AVAILABLE"
            ),
            recovery_hint=(
                None
                if available
                else "Configure the governed artifact collaboration database, "
                "encryption keys, fingerprints, and ACL broker before using comments."
            ),
        ),
        staged_review=WorkflowCapability(
            available=available,
            configured=available,
            reason_code=(None if available else "ARTIFACT_STAGED_REVIEW_NOT_AVAILABLE"),
            recovery_hint=(
                None
                if available
                else "Configure the governed artifact collaboration database, encryption keys, "
                "fingerprints, and ACL broker before using staged review."
            ),
        ),
        signed_worm_receipt=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ARTIFACT_SIGNED_WORM_RECEIPT_NOT_CONFIGURED",
            recovery_hint=(
                "Configure an attested immutable-ledger provider and tenant signing key "
                "before issuing signed WORM receipts."
            ),
        ),
        automatic_masking=WorkflowCapability(
            available=local_remediation_available,
            configured=local_remediation_available,
            reason_code=(
                None
                if local_remediation_available
                else "ARTIFACT_AUTOMATIC_MASKING_NOT_CONFIGURED"
            ),
            recovery_hint=(
                None
                if local_remediation_available
                else "Configure the governed artifact database and security keys before "
                "applying deterministic automatic redaction."
            ),
        ),
        synthetic_replacement=WorkflowCapability(
            available=local_remediation_available,
            configured=local_remediation_available,
            reason_code=(
                None
                if local_remediation_available
                else "ARTIFACT_SYNTHETIC_REPLACEMENT_NOT_CONFIGURED"
            ),
            recovery_hint=(
                None
                if local_remediation_available
                else "Configure the governed artifact database and security keys before "
                "replacing detected identifiers with deterministic synthetic placeholders."
            ),
        ),
        review_notification=WorkflowCapability(
            available=available,
            configured=available,
            reason_code=(None if available else "ARTIFACT_REVIEW_NOTIFICATION_NOT_CONFIGURED"),
            recovery_hint=(
                None
                if available
                else "Configure the governed review notification provider before resending "
                "a review request."
            ),
        ),
        review_rejection=WorkflowCapability(
            available=available,
            configured=available,
            reason_code=(
                None if available else "ARTIFACT_REVIEW_REJECTION_NOT_AVAILABLE"
            ),
            recovery_hint=(
                "Review rejection is recorded by the governed staged-review ledger."
                if available
                else "Configure the governed artifact collaboration database, encryption "
                "keys, fingerprints, and ACL broker before deciding a review."
            ),
        ),
        provider_state=state,
        recovery_hint=hint,
    )
