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
    ReplyTeamArtifactCommentRequest,
    ResolveTeamArtifactCommentRequest,
    RunTeamArtifactPreflightRequest,
    TeamArtifactMember,
)
from dwp_agent.artifact_collaboration_provider import (
    ArtifactAclAccessRequestResult,
    ArtifactAclPreflightResult,
)
from dwp_agent.artifact_collaboration_store import PostgresArtifactCollaborationStore
from dwp_agent.artifact_contracts import ArtifactDraftContent, CreateArtifactRequest
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
