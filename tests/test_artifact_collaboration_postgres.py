from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import Error as PsycopgError, connect

from dwp_agent.artifact_collaboration_contracts import (
    CreateTeamArtifactAccessRequest,
    CreateTeamArtifactCommentRequest,
    CreateTeamArtifactWorkspaceRequest,
    DecideTeamArtifactReviewStageRequest,
    ExecuteTeamArtifactRemediationRequest,
    ReplyTeamArtifactCommentRequest,
    ResolveTeamArtifactCommentRequest,
    RunTeamArtifactPreflightRequest,
    TeamArtifactMember,
)
from dwp_agent.artifact_collaboration_provider import (
    ArtifactAclAccessRequestResult,
    ArtifactAclPreflightResult,
    ArtifactReviewNotificationResult,
)
from dwp_agent.artifact_collaboration_store import PostgresArtifactCollaborationStore
from dwp_agent.artifact_review_notification_contracts import (
    review_notification_result_sha256,
)
from dwp_agent.artifact_contracts import (
    ArtifactDraftContent,
    ArtifactMetadata,
    CreateArtifactRequest,
)
from dwp_agent.artifact_postgres_store import PostgresArtifactStore
from dwp_agent.database_migrations import apply_migrations
from dwp_agent.domain_retention_store import PostgresDomainRetentionStore
from dwp_agent.governed_domain_contracts import (
    DomainKey,
    UpsertRetentionPolicyRequest,
)
from dwp_agent.governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
)
from dwp_agent.personal_domain_security import PersonalDomainIdentity


DATABASE_URL = os.getenv("DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DWP_AGENT_PERSONAL_DOMAIN_TEST_DATABASE_URL is not configured.",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    if not DATABASE_URL:
        return
    database_name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not database_name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Artifact collaboration tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)


class _DeniedAccessProvider:
    def preflight(self, **request: object) -> ArtifactAclPreflightResult:
        return ArtifactAclPreflightResult(
            artifactId=request["artifact_id"],
            teamId=request["team_id"],
            decisionRevision=1,
            members=[
                TeamArtifactMember(
                    subjectId="member-2",
                    role="EDITOR",
                    allowed=False,
                    deniedSourceCount=1,
                    reasonCode="ACL_DENIED",
                )
            ],
            allowedSources=[],
            excludedSources=[],
            evidenceSha256=hashlib.sha256(b"denied").hexdigest(),
            expiresAt=datetime.now(UTC) + timedelta(minutes=5),
        )

    def request_access(self, **request: object) -> ArtifactAclAccessRequestResult:
        return ArtifactAclAccessRequestResult(
            accessRequestId=uuid4(),
            artifactId=request["artifact_id"],
            teamId=request["team_id"],
            preflightId=request["preflight_id"],
            state="PENDING",
            submissionEvidenceSha256=hashlib.sha256(b"submitted").hexdigest(),
        )


class _AllowedAccessProvider:
    def preflight(self, **request: object) -> ArtifactAclPreflightResult:
        return ArtifactAclPreflightResult(
            artifactId=request["artifact_id"],
            teamId=request["team_id"],
            decisionRevision=7,
            members=[
                TeamArtifactMember(
                    subjectId=member.subject_id,
                    role=member.role,
                    allowed=True,
                    deniedSourceCount=0,
                )
                for member in request["members"]
            ],
            allowedSources=request["sources"],
            excludedSources=[],
            evidenceSha256=hashlib.sha256(b"allowed").hexdigest(),
            expiresAt=datetime.now(UTC) + timedelta(minutes=5),
        )

    def request_access(self, **_request: object) -> ArtifactAclAccessRequestResult:
        raise AssertionError("An allowed preflight must not request access.")


class _RemediationProvider(_AllowedAccessProvider):
    def notify_review(self, **request: object) -> ArtifactReviewNotificationResult:
        delivered_at = datetime.now(UTC)
        provider_receipt_id = f"review-notification:{request['command_id']}"
        evidence = review_notification_result_sha256(
            command_id=request["command_id"],
            artifact_id=request["artifact_id"],
            workspace_id=request["workspace_id"],
            stage_id=request["stage_id"],
            assignee_subject_id=request["assignee_subject_id"],
            workspace_revision=request["workspace_revision"],
            reason_code=request["reason_code"],
            change_reason=request["change_reason"],
            delivery_state="DELIVERED",
            provider_receipt_id=provider_receipt_id,
            delivered_at=delivered_at,
        )
        return ArtifactReviewNotificationResult(
            commandId=request["command_id"],
            artifactId=request["artifact_id"],
            workspaceId=request["workspace_id"],
            stageId=request["stage_id"],
            assigneeSubjectId=request["assignee_subject_id"],
            workspaceRevision=request["workspace_revision"],
            reasonCode=request["reason_code"],
            changeReason=request["change_reason"],
            deliveryState="DELIVERED",
            providerReceiptId=provider_receipt_id,
            resultSha256=evidence,
            deliveredAt=delivered_at,
        )


def test_denied_preflight_access_request_is_durable_audited_and_idempotent() -> None:
    tenant_id = 900_000_000 + uuid4().int % 90_000_000
    identity = PersonalDomainIdentity(
        tenant_id=tenant_id,
        user_id="member-1",
        correlation_id=str(uuid4()),
        auth_session_id="session-1",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset(),
    )
    PostgresDomainRetentionStore(DATABASE_URL).upsert_policy(
        identity,
        DomainKey.ARTIFACT,
        UpsertRetentionPolicyRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="TENANT_RETENTION_BOOTSTRAP",
            changeReason="Set explicit artifact retention for verification.",
            retentionDays=365,
            deletionGraceDays=7,
            legalHold=False,
        ),
    )
    artifact = PostgresArtifactStore(DATABASE_URL).create(
        identity,
        CreateArtifactRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_ARTIFACT_CREATE",
            artifactType="DOCUMENT",
            content=ArtifactDraftContent(
                title="ACL test",
                body="Verify governed team access request.",
            ),
        ),
    )
    team_id = uuid4()
    store = PostgresArtifactCollaborationStore(
        DATABASE_URL,
        provider=_DeniedAccessProvider(),
    )
    preflight = store.preflight(
        identity,
        artifact.artifact_id,
        RunTeamArtifactPreflightRequest(
            commandId=uuid4(),
            expectedRevision=artifact.revision,
            reasonCode="TEAM_ACL_PREFLIGHT",
            teamId=team_id,
            artifactRevision=artifact.revision,
            members=[{"subjectId": "member-2", "role": "EDITOR"}],
        ),
    )
    command_id = uuid4()
    request = CreateTeamArtifactAccessRequest(
        commandId=command_id,
        expectedRevision=artifact.revision,
        reasonCode="TEAM_ACL_REQUEST",
        changeReason="Request reviewed access for the denied team member.",
        preflightId=preflight.preflight_id,
    )

    submitted = store.request_access(identity, artifact.artifact_id, request)
    replay = store.request_access(identity, artifact.artifact_id, request)

    with connect(DATABASE_URL) as connection:
        row_count = connection.execute(
            """SELECT COUNT(*) FROM ai_artifact_team_access_requests
                WHERE access_request_id = %s""",
            (submitted.access_request_id,),
        ).fetchone()[0]
        event = connection.execute(
            """SELECT event_type, current_state
                 FROM ai_artifact_collaboration_events
                WHERE command_id = %s""",
            (command_id,),
        ).fetchone()

    assert preflight.state == "PERMISSION_DENIED"
    assert submitted.state == "PENDING"
    assert replay.access_request_id == submitted.access_request_id
    assert row_count == 1
    assert event == ("ACCESS_REQUESTED", "PENDING")


def test_inline_comments_are_encrypted_scoped_audited_and_replay_safe() -> None:
    tenant_id = 800_000_000 + uuid4().int % 90_000_000
    owner = PersonalDomainIdentity(
        tenant_id=tenant_id,
        user_id="comment-owner",
        correlation_id=str(uuid4()),
        auth_session_id="owner-session",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset(),
    )
    reviewer = PersonalDomainIdentity(
        tenant_id=tenant_id,
        user_id="comment-reviewer",
        correlation_id=str(uuid4()),
        auth_session_id="reviewer-session",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset(),
    )
    PostgresDomainRetentionStore(DATABASE_URL).upsert_policy(
        owner,
        DomainKey.ARTIFACT,
        UpsertRetentionPolicyRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="TENANT_RETENTION_BOOTSTRAP",
            changeReason="Set explicit artifact retention for comment verification.",
            retentionDays=365,
            deletionGraceDays=7,
            legalHold=False,
        ),
    )
    artifact = PostgresArtifactStore(DATABASE_URL).create(
        owner,
        CreateArtifactRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_ARTIFACT_CREATE",
            artifactType="DOCUMENT",
            content=ArtifactDraftContent(
                title="Comment test",
                body="Verify governed inline comments.",
            ),
        ),
    )
    team_id = uuid4()
    store = PostgresArtifactCollaborationStore(
        DATABASE_URL,
        provider=_AllowedAccessProvider(),
    )
    preflight = store.preflight(
        owner,
        artifact.artifact_id,
        RunTeamArtifactPreflightRequest(
            commandId=uuid4(),
            expectedRevision=artifact.revision,
            reasonCode="TEAM_ACL_PREFLIGHT",
            teamId=team_id,
            artifactRevision=artifact.revision,
            members=[{"subjectId": reviewer.user_id, "role": "REVIEWER"}],
        ),
    )
    workspace = store.create_workspace(
        owner,
        artifact.artifact_id,
        CreateTeamArtifactWorkspaceRequest(
            commandId=uuid4(),
            expectedRevision=artifact.revision,
            reasonCode="TEAM_WORKSPACE_CREATE",
            changeReason="Create the reviewed team artifact workspace.",
            teamId=team_id,
            preflightId=preflight.preflight_id,
        ),
    )
    create_request = CreateTeamArtifactCommentRequest(
        commandId=uuid4(),
        expectedRevision=workspace.revision,
        reasonCode="TEAM_ARTIFACT_COMMENT",
        body="Confirm the source citation before publishing.",
        anchor="Evidence section",
    )
    comment = store.create_comment(owner, artifact.artifact_id, create_request)
    replay = store.create_comment(owner, artifact.artifact_id, create_request)
    reply_request = ReplyTeamArtifactCommentRequest(
        commandId=uuid4(),
        expectedRevision=comment.revision,
        reasonCode="TEAM_ARTIFACT_COMMENT_REPLY",
        body="Citation checked against the governed source.",
    )
    replied = store.reply_comment(
        reviewer,
        artifact.artifact_id,
        comment.comment_id,
        reply_request,
    )
    reviewer_replay = store.reply_comment(
        reviewer,
        artifact.artifact_id,
        comment.comment_id,
        reply_request,
    )

    with pytest.raises(GovernedDomainConflict):
        store.resolve_comment(
            reviewer,
            artifact.artifact_id,
            comment.comment_id,
            ResolveTeamArtifactCommentRequest(
                commandId=uuid4(),
                expectedRevision=replied.revision,
                reasonCode="TEAM_ARTIFACT_COMMENT_RESOLVE",
                changeReason="Resolve the reviewed artifact comment.",
            ),
        )

    resolve_request = ResolveTeamArtifactCommentRequest(
        commandId=uuid4(),
        expectedRevision=replied.revision,
        reasonCode="TEAM_ARTIFACT_COMMENT_RESOLVE",
        changeReason="Resolve the reviewed artifact comment.",
    )
    resolved = store.resolve_comment(
        owner,
        artifact.artifact_id,
        comment.comment_id,
        resolve_request,
    )
    listed = store.list_comments(reviewer, artifact.artifact_id)

    wrong_session = PersonalDomainIdentity(
        tenant_id=tenant_id,
        user_id=reviewer.user_id,
        correlation_id=str(uuid4()),
        auth_session_id="different-reviewer-session",
        roles=reviewer.roles,
        permissions=reviewer.permissions,
    )
    with pytest.raises(GovernedDomainConflict):
        store.reply_comment(
            wrong_session,
            artifact.artifact_id,
            comment.comment_id,
            reply_request,
        )

    other_tenant = PersonalDomainIdentity(
        tenant_id=tenant_id + 1,
        user_id=owner.user_id,
        correlation_id=str(uuid4()),
        auth_session_id="other-tenant-session",
        roles=owner.roles,
        permissions=owner.permissions,
    )
    with pytest.raises(GovernedDomainNotFound):
        store.list_comments(other_tenant, artifact.artifact_id)

    with connect(DATABASE_URL) as connection:
        encrypted = connection.execute(
            """SELECT body_envelope, anchor_envelope
                 FROM ai_artifact_team_comments
                WHERE comment_id = %s""",
            (comment.comment_id,),
        ).fetchone()
        events = connection.execute(
            """SELECT event_type
                 FROM ai_artifact_collaboration_events
                WHERE command_id IN (%s, %s, %s)
                ORDER BY occurred_at ASC""",
            (
                create_request.command_id,
                reply_request.command_id,
                resolve_request.command_id,
            ),
        ).fetchall()
        actual_events = connection.execute(
            """SELECT event_type
                 FROM ai_artifact_collaboration_events
                WHERE artifact_id = %s
                  AND event_type LIKE 'COMMENT_%%'
                ORDER BY occurred_at ASC""",
            (artifact.artifact_id,),
        ).fetchall()
        with pytest.raises(PsycopgError):
            with connection.transaction():
                connection.execute(
                    """UPDATE ai_artifact_team_comment_replies
                          SET body_envelope = body_envelope
                        WHERE reply_id = %s""",
                    (replied.replies[0].reply_id,),
                )

    assert replay.comment_id == comment.comment_id
    assert reviewer_replay.revision == replied.revision
    assert replied.revision == 2
    assert len(replied.replies) == 1
    assert resolved.state == "RESOLVED"
    assert resolved.revision == 3
    assert listed == [resolved]
    assert encrypted[0].startswith("dwp2.")
    assert "Confirm the source" not in encrypted[0]
    assert encrypted[1].startswith("dwp2.")
    assert [event[0] for event in events] == [
        "COMMENT_CREATED",
        "COMMENT_REPLIED",
        "COMMENT_RESOLVED",
    ]
    assert [event[0] for event in actual_events] == [
        "COMMENT_CREATED",
        "COMMENT_REPLIED",
        "COMMENT_RESOLVED",
    ]


def test_artifact_metadata_and_staged_review_are_tenant_bound_and_null_safe() -> None:
    tenant_id = 700_000_000 + uuid4().int % 90_000_000
    owner = PersonalDomainIdentity(
        tenant_id=tenant_id,
        user_id="review-owner",
        correlation_id=str(uuid4()),
        auth_session_id="review-owner-session",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset(),
    )
    reviewer = PersonalDomainIdentity(
        tenant_id=tenant_id,
        user_id="primary-reviewer",
        correlation_id=str(uuid4()),
        auth_session_id="primary-reviewer-session",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset(),
    )
    PostgresDomainRetentionStore(DATABASE_URL).upsert_policy(
        owner,
        DomainKey.ARTIFACT,
        UpsertRetentionPolicyRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="TENANT_RETENTION_BOOTSTRAP",
            changeReason="Set artifact retention for staged review verification.",
            retentionDays=365,
            deletionGraceDays=7,
            legalHold=False,
        ),
    )
    sla_due_at = datetime.now(UTC) + timedelta(days=2)
    artifact = PostgresArtifactStore(DATABASE_URL).create(
        owner,
        CreateArtifactRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_ARTIFACT_CREATE",
            artifactType="DOCUMENT",
            content=ArtifactDraftContent(
                title="Staged review test",
                body="Verify metadata and governed review decisions.",
            ),
            metadata=ArtifactMetadata(
                tags=["Quarterly", "Risk"],
                projectKey="FIN-Q4",
                reviewSlaDueAt=sla_due_at,
            ),
        ),
    )
    assert artifact.author_subject_id == owner.user_id
    assert artifact.metadata.tags == ["Quarterly", "Risk"]
    assert artifact.metadata.project_key == "FIN-Q4"
    assert artifact.metadata.review_sla_due_at == sla_due_at

    store = PostgresArtifactCollaborationStore(
        DATABASE_URL,
        provider=_AllowedAccessProvider(),
    )
    team_id = uuid4()
    preflight = store.preflight(
        owner,
        artifact.artifact_id,
        RunTeamArtifactPreflightRequest(
            commandId=uuid4(),
            expectedRevision=artifact.revision,
            reasonCode="TEAM_ACL_PREFLIGHT",
            teamId=team_id,
            artifactRevision=artifact.revision,
            members=[{"subjectId": reviewer.user_id, "role": "REVIEWER"}],
        ),
    )
    workspace = store.create_workspace(
        owner,
        artifact.artifact_id,
        CreateTeamArtifactWorkspaceRequest(
            commandId=uuid4(),
            expectedRevision=artifact.revision,
            reasonCode="TEAM_WORKSPACE_CREATE",
            changeReason="Create a tenant-bound staged review workspace.",
            teamId=team_id,
            preflightId=preflight.preflight_id,
        ),
    )
    primary = next(
        stage for stage in workspace.review_stages if stage.stage_key == "PRIMARY_REVIEW"
    )
    assert primary.assignee_subject_id == reviewer.user_id
    assert primary.state == "PENDING"
    assert len(workspace.governance_gates) == 4
    assert workspace.review_sla_due_at == sla_due_at
    assert workspace.signature_evidence.capability.available is False

    with connect(DATABASE_URL) as connection:
        with pytest.raises(PsycopgError):
            with connection.transaction():
                connection.execute(
                    """UPDATE ai_artifact_review_stages
                          SET stage_state = 'APPROVED', revision = revision + 1,
                              decided_by_user_id = %s, decided_at = CURRENT_TIMESTAMP,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE stage_id = %s""",
                    (reviewer.user_id, primary.stage_id),
                )
        tenant_boundary = connection.execute(
            """SELECT pg_get_constraintdef(oid)
                 FROM pg_constraint
                WHERE conname = 'fk_ai_artifact_review_stage_boundary'"""
        ).fetchone()[0]
    assert (
        "FOREIGN KEY (workspace_id, artifact_id, tenant_id)" in tenant_boundary
    )
    assert (
        "REFERENCES ai_artifact_team_workspaces(workspace_id, artifact_id, tenant_id)"
        in tenant_boundary
    )

    other_tenant = PersonalDomainIdentity(
        tenant_id=tenant_id + 1,
        user_id=reviewer.user_id,
        correlation_id=str(uuid4()),
        auth_session_id="other-tenant-review-session",
        roles=reviewer.roles,
        permissions=reviewer.permissions,
    )
    with pytest.raises(GovernedDomainNotFound):
        store.get_workspace(other_tenant, artifact.artifact_id)
    with pytest.raises(GovernedDomainNotFound):
        store.decide_review_stage(
            other_tenant,
            artifact.artifact_id,
            primary.stage_id,
            DecideTeamArtifactReviewStageRequest(
                commandId=uuid4(),
                expectedRevision=primary.revision,
                reasonCode="TEAM_ARTIFACT_REVIEW_DECISION",
                changeReason="A different tenant must not see this review stage.",
                decision="APPROVE",
            ),
        )

    request = DecideTeamArtifactReviewStageRequest(
        commandId=uuid4(),
        expectedRevision=primary.revision,
        reasonCode="TEAM_ARTIFACT_REVIEW_DECISION",
        changeReason="Approve after verifying the four governance gates.",
        decision="APPROVE",
    )
    decided = store.decide_review_stage(
        reviewer,
        artifact.artifact_id,
        primary.stage_id,
        request,
    )
    replay = store.decide_review_stage(
        reviewer,
        artifact.artifact_id,
        primary.stage_id,
        request,
    )
    approved = next(
        stage for stage in decided.review_stages if stage.stage_id == primary.stage_id
    )
    assert approved.state == "APPROVED"
    assert approved.evidence_fingerprint is not None
    assert approved.decided_by_subject_id == reviewer.user_id
    assert replay == decided

    with connect(DATABASE_URL) as connection:
        reason_envelope, event_type = connection.execute(
            """SELECT s.decision_reason_envelope, e.event_type
                 FROM ai_artifact_review_stages s
                 JOIN ai_artifact_collaboration_events e
                   ON e.workspace_id = s.workspace_id
                  AND e.command_id = %s
                WHERE s.stage_id = %s""",
            (request.command_id, primary.stage_id),
        ).fetchone()
    assert reason_envelope.startswith("dwp2.")
    assert "Approve after" not in reason_envelope
    assert event_type == "REVIEW_APPROVED"


def test_remediation_updates_encrypted_draft_and_notification_records_provider_receipt() -> None:
    tenant_id = 780_000_000 + uuid4().int % 90_000_000
    owner = PersonalDomainIdentity(
        tenant_id=tenant_id,
        user_id="remediation-owner",
        correlation_id=str(uuid4()),
        auth_session_id="remediation-owner-session",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset(),
    )
    reviewer = PersonalDomainIdentity(
        tenant_id=tenant_id,
        user_id="remediation-reviewer",
        correlation_id=str(uuid4()),
        auth_session_id="remediation-reviewer-session",
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset(),
    )
    PostgresDomainRetentionStore(DATABASE_URL).upsert_policy(
        owner,
        DomainKey.ARTIFACT,
        UpsertRetentionPolicyRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="TENANT_RETENTION_BOOTSTRAP",
            changeReason="Set artifact retention for remediation verification.",
            retentionDays=365,
            deletionGraceDays=7,
            legalHold=False,
        ),
    )
    artifacts = PostgresArtifactStore(DATABASE_URL)
    artifact = artifacts.create(
        owner,
        CreateArtifactRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_ARTIFACT_CREATE",
            artifactType="DOCUMENT",
            content=ArtifactDraftContent(
                title="Contact owner@example.com",
                body="Call 010-1234-5678 and verify 900101-1234567.",
            ),
        ),
    )
    with connect(DATABASE_URL) as connection:
        source_content_fingerprint = connection.execute(
            "SELECT content_fingerprint FROM ai_artifact_drafts WHERE artifact_id = %s",
            (artifact.artifact_id,),
        ).fetchone()[0]
    store = PostgresArtifactCollaborationStore(
        DATABASE_URL,
        provider=_RemediationProvider(),
    )
    mask_request = ExecuteTeamArtifactRemediationRequest(
        commandId=uuid4(),
        expectedRevision=artifact.revision,
        reasonCode="TEAM_ARTIFACT_REMEDIATION",
        changeReason="Mask detected identifiers before sharing this artifact.",
        action="AUTOMATIC_MASKING",
    )

    masked = store.remediate(owner, artifact.artifact_id, mask_request)
    masked_replay = store.remediate(owner, artifact.artifact_id, mask_request)
    current = artifacts.get(owner, artifact.artifact_id)

    assert masked_replay == masked
    assert masked.affected_count == 3
    assert masked.artifact_revision == artifact.revision + 1
    assert masked.provider_receipt_id is None
    assert current.revision == masked.artifact_revision
    assert masked.source_content_fingerprint == source_content_fingerprint
    assert masked.finding_manifest_sha256 is not None
    assert masked.result_content_sha256 is not None
    assert masked.residual_finding_count == 0
    assert masked.remediated_codes == [
        "EMAIL_ADDRESS",
        "KOREAN_RESIDENT_ID",
        "PHONE_NUMBER",
    ]
    assert current.content.title == "Contact [MASKED_EMAIL_ADDRESS]"
    assert current.content.body == (
        "Call [MASKED_PHONE_NUMBER] and verify [MASKED_KOREAN_RESIDENT_ID]."
    )
    with pytest.raises(GovernedDomainConflict, match="No current artifact DLP findings"):
        store.remediate(
            owner,
            artifact.artifact_id,
            mask_request.model_copy(
                update={
                    "command_id": uuid4(),
                    "expected_revision": current.revision,
                }
            ),
        )

    source_owner = PersonalDomainIdentity(
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        correlation_id=owner.correlation_id,
        auth_session_id=owner.auth_session_id,
        roles=owner.roles,
        permissions=frozenset({"APP.MAIL:VIEW"}),
    )
    unverified = artifacts.create(
        source_owner,
        CreateArtifactRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_ARTIFACT_CREATE",
            artifactType="DOCUMENT",
            content=ArtifactDraftContent(
                title="Unverified owner@example.com",
                body="A governed source must be verified first.",
            ),
            sources=[{"sourceType": "MAIL", "reference": "mail:unverified-1"}],
        ),
    )
    with pytest.raises(GovernedDomainConflict, match="Verify every current artifact source"):
        store.remediate(
            source_owner,
            unverified.artifact_id,
            mask_request.model_copy(
                update={
                    "command_id": uuid4(),
                    "expected_revision": unverified.revision,
                }
            ),
        )

    team_id = uuid4()
    preflight = store.preflight(
        owner,
        artifact.artifact_id,
        RunTeamArtifactPreflightRequest(
            commandId=uuid4(),
            expectedRevision=current.revision,
            reasonCode="TEAM_ACL_PREFLIGHT",
            teamId=team_id,
            artifactRevision=current.revision,
            members=[{"subjectId": reviewer.user_id, "role": "REVIEWER"}],
        ),
    )
    workspace = store.create_workspace(
        owner,
        artifact.artifact_id,
        CreateTeamArtifactWorkspaceRequest(
            commandId=uuid4(),
            expectedRevision=current.revision,
            reasonCode="TEAM_WORKSPACE_CREATE",
            changeReason="Create the governed workspace for remediation review.",
            teamId=team_id,
            preflightId=preflight.preflight_id,
        ),
    )
    primary = next(
        stage for stage in workspace.review_stages if stage.stage_key == "PRIMARY_REVIEW"
    )
    notification_request = ExecuteTeamArtifactRemediationRequest(
        commandId=uuid4(),
        expectedRevision=current.revision,
        reasonCode="TEAM_REVIEW_NOTIFICATION",
        changeReason="Resend the governed request to the assigned primary reviewer.",
        action="REVIEW_NOTIFICATION",
        stageId=primary.stage_id,
    )

    notified = store.remediate(
        owner, artifact.artifact_id, notification_request
    )
    notified_replay = store.remediate(
        owner, artifact.artifact_id, notification_request
    )

    assert notified_replay == notified
    assert notified.artifact_revision == current.revision
    assert notified.workspace_revision == workspace.revision
    assert notified.provider_receipt_id == (
        f"review-notification:{notification_request.command_id}"
    )
    assert notified.affected_count == 1
    with pytest.raises(GovernedDomainNotFound):
        store.remediate(
            reviewer,
            artifact.artifact_id,
            notification_request.model_copy(update={"command_id": uuid4()}),
        )

    with connect(DATABASE_URL) as connection:
        command_rows = connection.execute(
            """SELECT command_type, result_envelope
                 FROM ai_artifact_collaboration_commands
                WHERE command_id IN (%s, %s) ORDER BY command_type""",
            (mask_request.command_id, notification_request.command_id),
        ).fetchall()
        event_rows = connection.execute(
            """SELECT event_type, current_state
                 FROM ai_artifact_collaboration_events
                WHERE command_id IN (%s, %s) ORDER BY event_type""",
            (mask_request.command_id, notification_request.command_id),
        ).fetchall()
        content_envelope = connection.execute(
            """SELECT content_envelope FROM ai_artifact_drafts
                WHERE artifact_id = %s""",
            (artifact.artifact_id,),
        ).fetchone()[0]

    assert [row[0] for row in command_rows] == [
        "REMEDIATE_MASK",
        "REVIEW_NOTIFICATION",
    ]
    assert all(row[1].startswith("dwp2.") for row in command_rows)
    assert event_rows == [
        ("REMEDIATION_APPLIED", "DRAFT"),
        ("REVIEW_NOTIFICATION_SENT", "DRAFT"),
    ]
    assert content_envelope.startswith("dwp2.")
    assert "owner@example.com" not in content_envelope
