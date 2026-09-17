from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect

from dwp_agent.artifact_collaboration_contracts import (
    CreateTeamArtifactAccessRequest,
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
