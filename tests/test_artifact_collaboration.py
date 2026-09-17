from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from dwp_agent import artifact_collaboration_api
from dwp_agent.artifact_collaboration_contracts import (
    CreateTeamArtifactAccessRequest,
    ResolveTeamArtifactConflictRequest,
    RunTeamArtifactPreflightRequest,
    SubmitTeamArtifactEditRequest,
    TeamArtifactCapabilities,
    TeamArtifactAccessRequest,
    TeamArtifactMemberRequest,
    TeamArtifactShare,
)
from dwp_agent.artifact_collaboration_provider import (
    ArtifactCollaborationProvider,
    ArtifactCollaborationProviderConfiguration,
    ArtifactCollaborationProviderUnavailable,
)
from dwp_agent.dwaion_workflow_contracts import WorkflowCapability
from dwp_agent.artifact_contracts import ArtifactSourceReference


ROOT = Path(__file__).resolve().parents[1]


def _configuration() -> ArtifactCollaborationProviderConfiguration:
    return ArtifactCollaborationProviderConfiguration(
        enabled=True,
        base_url="https://artifact-acl.internal.example",
        service_token="test-service-token",
        allowed_hosts=frozenset({"artifact-acl.internal.example"}),
        timeout_seconds=5,
    )


def _provider_result(
    artifact_id: UUID,
    team_id: UUID,
    *,
    role: str = "EDITOR",
    duplicate_partition: bool = False,
) -> dict[str, object]:
    source = {"sourceType": "MAIL", "reference": "mail:thread-77"}
    return {
        "artifactId": str(artifact_id),
        "teamId": str(team_id),
        "decisionRevision": 11,
        "members": [
            {
                "subjectId": "member-2",
                "role": role,
                "allowed": True,
                "deniedSourceCount": 0,
            }
        ],
        "allowedSources": [source],
        "excludedSources": [source] if duplicate_partition else [],
        "evidenceSha256": hashlib.sha256(b"acl-evidence").hexdigest(),
        "expiresAt": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }


def test_acl_provider_binds_current_authorization_subject_role_and_sources() -> None:
    artifact_id = uuid4()
    team_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/internal/v1/artifact-acl/preflights"
        assert body["requireCurrentAuthorization"] is True
        assert body["artifactId"] == str(artifact_id)
        assert body["teamId"] == str(team_id)
        assert body["members"] == [{"subjectId": "member-2", "role": "EDITOR"}]
        assert body["sources"] == [
            {"sourceType": "MAIL", "reference": "mail:thread-77"}
        ]
        return httpx.Response(200, json=_provider_result(artifact_id, team_id))

    result = ArtifactCollaborationProvider(
        _configuration(), transport=httpx.MockTransport(handler)
    ).preflight(
        artifact_id=artifact_id,
        team_id=team_id,
        tenant_id=7,
        owner_user_id="member-1",
        artifact_revision=3,
        members=[TeamArtifactMemberRequest(subject_id="member-2", role="EDITOR")],
        sources=[ArtifactSourceReference(source_type="MAIL", reference="mail:thread-77")],
        exclude_inaccessible_sources=False,
        correlation_id="artifact-collaboration-test",
    )

    assert result.decisionRevision == 11
    assert result.members[0].allowed is True


def test_acl_provider_submits_denied_access_request_with_idempotency() -> None:
    artifact_id = uuid4()
    team_id = uuid4()
    preflight_id = uuid4()
    command_id = uuid4()
    access_request_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/internal/v1/artifact-acl/access-requests"
        assert body["commandId"] == str(command_id)
        assert body["preflightId"] == str(preflight_id)
        assert body["deniedSubjectIds"] == ["member-2"]
        assert body["deniedSourceCount"] == 1
        assert body["requireCurrentAuthorization"] is True
        return httpx.Response(
            202,
            json={
                "accessRequestId": str(access_request_id),
                "artifactId": str(artifact_id),
                "teamId": str(team_id),
                "preflightId": str(preflight_id),
                "state": "PENDING",
                "submissionEvidenceSha256": hashlib.sha256(
                    b"access-request"
                ).hexdigest(),
            },
        )

    result = ArtifactCollaborationProvider(
        _configuration(), transport=httpx.MockTransport(handler)
    ).request_access(
        artifact_id=artifact_id,
        team_id=team_id,
        preflight_id=preflight_id,
        tenant_id=7,
        owner_user_id="member-1",
        artifact_revision=3,
        command_id=command_id,
        denied_subject_ids=["member-2"],
        denied_source_count=1,
        reason_code="TEAM_ACL_REQUEST",
        change_reason="Request reviewed access for denied team recipients.",
        correlation_id="artifact-collaboration-test",
    )

    assert result.accessRequestId == access_request_id
    assert result.state == "PENDING"


@pytest.mark.parametrize(
    ("role", "duplicate_partition", "code"),
    (
        ("OWNER", False, "ARTIFACT_ACL_SUBJECT_MISMATCH"),
        ("EDITOR", True, "ARTIFACT_ACL_PROVIDER_UNAVAILABLE"),
    ),
)
def test_acl_provider_fails_closed_on_role_or_partition_mismatch(
    role: str, duplicate_partition: bool, code: str
) -> None:
    artifact_id = uuid4()
    team_id = uuid4()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_provider_result(
                artifact_id,
                team_id,
                role=role,
                duplicate_partition=duplicate_partition,
            ),
        )

    provider = ArtifactCollaborationProvider(
        _configuration(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ArtifactCollaborationProviderUnavailable, match=code):
        provider.preflight(
            artifact_id=artifact_id,
            team_id=team_id,
            tenant_id=7,
            owner_user_id="member-1",
            artifact_revision=3,
            members=[TeamArtifactMemberRequest(subject_id="member-2", role="EDITOR")],
            sources=[
                ArtifactSourceReference(
                    source_type="MAIL", reference="mail:thread-77"
                )
            ],
            exclude_inaccessible_sources=False,
            correlation_id="artifact-collaboration-test",
        )


def test_contracts_require_merged_content_and_sealed_revocation_receipt() -> None:
    common = {
        "commandId": str(uuid4()),
        "expectedRevision": 2,
        "reasonCode": "USER_CONFLICT_RESOLUTION",
        "changeReason": "Resolve the reviewed team artifact conflict.",
    }
    with pytest.raises(ValidationError):
        ResolveTeamArtifactConflictRequest.model_validate(
            {**common, "resolution": "MERGE"}
        )

    now = datetime.now(UTC)
    share = {
        "shareId": str(uuid4()),
        "workspaceId": str(uuid4()),
        "state": "REVOKED",
        "permission": "VIEW",
        "memberCount": 1,
        "expiresAt": (now + timedelta(hours=1)).isoformat(),
        "revokedAt": now.isoformat(),
        "receiptId": str(uuid4()),
        "receiptSha256": "a" * 64,
        "createdAt": now.isoformat(),
    }
    with pytest.raises(ValidationError):
        TeamArtifactShare.model_validate(share)

    valid = TeamArtifactShare.model_validate(
        {
            **share,
            "revocationReceiptId": str(uuid4()),
            "revocationReceiptSha256": "b" * 64,
        }
    )
    assert valid.state == "REVOKED"

    with pytest.raises(ValidationError):
        TeamArtifactAccessRequest.model_validate(
            {
                "accessRequestId": str(uuid4()),
                "artifactId": str(uuid4()),
                "teamId": str(uuid4()),
                "preflightId": str(uuid4()),
                "state": "PENDING",
                "deniedSubjectCount": 0,
                "deniedSourceCount": 0,
                "submissionEvidenceSha256": "c" * 64,
                "createdAt": now.isoformat(),
            }
        )

    request = CreateTeamArtifactAccessRequest.model_validate(
        {
            "commandId": str(uuid4()),
            "expectedRevision": 2,
            "reasonCode": "TEAM_ACL_REQUEST",
            "changeReason": "Request access for denied team recipients.",
            "preflightId": str(uuid4()),
        }
    )
    assert request.expected_revision == 2


def test_preflight_contract_rejects_duplicate_sources() -> None:
    source = {"sourceType": "MAIL", "reference": "mail:thread-77"}
    with pytest.raises(ValidationError):
        RunTeamArtifactPreflightRequest.model_validate(
            {
                "commandId": str(uuid4()),
                "expectedRevision": 3,
                "reasonCode": "TEAM_ACL_PREFLIGHT",
                "teamId": str(uuid4()),
                "artifactRevision": 3,
                "members": [{"subjectId": "member-2", "role": "EDITOR"}],
                "sources": [source, source],
            }
        )


def test_revision_aliases_cannot_bypass_optimistic_concurrency() -> None:
    with pytest.raises(ValidationError):
        RunTeamArtifactPreflightRequest.model_validate(
            {
                "commandId": str(uuid4()),
                "expectedRevision": 2,
                "reasonCode": "TEAM_ACL_PREFLIGHT",
                "teamId": str(uuid4()),
                "artifactRevision": 3,
                "members": [{"subjectId": "member-2", "role": "EDITOR"}],
            }
        )
    with pytest.raises(ValidationError):
        SubmitTeamArtifactEditRequest.model_validate(
            {
                "commandId": str(uuid4()),
                "expectedRevision": 2,
                "reasonCode": "TEAM_ARTIFACT_EDIT",
                "baseRevision": 3,
                "content": {"title": "Plan", "body": "Reviewed content"},
            }
        )


def _capabilities() -> TeamArtifactCapabilities:
    return TeamArtifactCapabilities(
        team_workspace_available=False,
        acl_preflight_available=False,
        access_request_available=False,
        collaboration_available=False,
        conflict_resolution_available=False,
        internal_sharing_available=False,
        external_sharing_available=False,
        share_expiry_available=False,
        share_revocation_available=False,
        automatic_masking=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ARTIFACT_AUTOMATIC_MASKING_NOT_CONFIGURED",
            recovery_hint="Configure masking.",
        ),
        synthetic_replacement=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ARTIFACT_SYNTHETIC_REPLACEMENT_NOT_CONFIGURED",
            recovery_hint="Configure synthetic replacement.",
        ),
        review_notification=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ARTIFACT_REVIEW_NOTIFICATION_NOT_CONFIGURED",
            recovery_hint="Configure review notifications.",
        ),
        review_rejection=WorkflowCapability(
            available=False,
            configured=False,
            reason_code="ARTIFACT_REVIEW_REJECTION_NOT_CONFIGURED",
            recovery_hint="Configure review rejection.",
        ),
        provider_state="NOT_CONFIGURED",
        recovery_hint="Configure and attest the artifact ACL broker.",
    )


def _headers(*permissions: str) -> dict[str, str]:
    return {
        "X-DWP-Service-Token": "artifact-collaboration-service-token",
        "X-DWP-Tenant-ID": "7001",
        "X-DWP-User-ID": "member-1",
        "X-Correlation-ID": "artifact-collaboration-test",
        "X-DWP-Auth-Session-ID": "session-1",
        "X-DWP-Identity-Plane": "TENANT",
        "X-DWP-Access-Mode": "NORMAL",
        "X-DWP-Roles": "WORKSPACE_MEMBER",
        "X-DWP-Permissions": ",".join(permissions),
    }


def test_capability_api_is_private_permissioned_and_truthful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DWP_AGENT_SERVICE_TOKEN", "artifact-collaboration-service-token"
    )
    monkeypatch.delenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", raising=False)
    monkeypatch.setattr(
        artifact_collaboration_api,
        "artifact_collaboration_runtime_capabilities",
        _capabilities,
    )
    app = FastAPI()
    app.include_router(artifact_collaboration_api.router)
    client = TestClient(app)

    denied = client.get(
        "/v1/artifact-collaboration/capabilities",
        headers=_headers("APP.ASK:VIEW"),
    )
    allowed = client.get(
        "/v1/artifact-collaboration/capabilities",
        headers=_headers("APP.ASK:VIEW", "APP.DWAION_ARTIFACTS:VIEW"),
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "no-store"
    assert allowed.json()["data"]["providerState"] == "NOT_CONFIGURED"
    assert allowed.json()["data"]["accessRequestAvailable"] is False
    assert allowed.json()["data"]["externalSharingAvailable"] is False
    assert allowed.json()["data"]["automaticMasking"] == {
        "available": False,
        "configured": False,
        "reasonCode": "ARTIFACT_AUTOMATIC_MASKING_NOT_CONFIGURED",
        "recoveryHint": "Configure masking.",
    }
    assert allowed.json()["data"]["reviewRejection"]["available"] is False


def test_access_request_api_is_permissioned_and_returns_pending_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DWP_AGENT_SERVICE_TOKEN", "artifact-collaboration-service-token"
    )
    monkeypatch.delenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", raising=False)
    now = datetime.now(UTC)
    access_request_id = uuid4()

    class FakeStore:
        def request_access(self, identity, artifact_id, request):
            assert identity.user_id == "member-1"
            assert artifact_id == ARTIFACT_ID
            assert request.preflight_id == PREFLIGHT_ID
            return TeamArtifactAccessRequest(
                access_request_id=access_request_id,
                artifact_id=artifact_id,
                team_id=TEAM_ID,
                preflight_id=request.preflight_id,
                state="PENDING",
                denied_subject_count=1,
                denied_source_count=0,
                submission_evidence_sha256="d" * 64,
                created_at=now,
            )

    ARTIFACT_ID = uuid4()
    TEAM_ID = uuid4()
    PREFLIGHT_ID = uuid4()
    monkeypatch.setattr(
        artifact_collaboration_api,
        "get_artifact_collaboration_store",
        lambda: FakeStore(),
    )
    app = FastAPI()
    app.include_router(artifact_collaboration_api.router)
    client = TestClient(app)
    body = {
        "commandId": str(uuid4()),
        "expectedRevision": 3,
        "reasonCode": "TEAM_ACL_REQUEST",
        "changeReason": "Request access for the denied team member.",
        "preflightId": str(PREFLIGHT_ID),
    }

    denied = client.post(
        f"/v1/artifact-collaboration/{ARTIFACT_ID}/access-requests",
        headers=_headers("APP.ASK:VIEW", "APP.DWAION_ARTIFACTS:VIEW"),
        json=body,
    )
    accepted = client.post(
        f"/v1/artifact-collaboration/{ARTIFACT_ID}/access-requests",
        headers=_headers("APP.ASK:VIEW", "APP.DWAION_ARTIFACTS:UPDATE"),
        json=body,
    )

    assert denied.status_code == 403
    assert accepted.status_code == 202
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.json()["data"] == {
        "accessRequestId": str(access_request_id),
        "artifactId": str(ARTIFACT_ID),
        "teamId": str(TEAM_ID),
        "preflightId": str(PREFLIGHT_ID),
        "state": "PENDING",
        "deniedSubjectCount": 1,
        "deniedSourceCount": 0,
        "submissionEvidenceSha256": "d" * 64,
        "createdAt": now.isoformat().replace("+00:00", "Z"),
    }


def test_v40_migration_seals_team_versions_conflicts_and_share_revocation() -> None:
    sql = (
        ROOT
        / "src/dwp_agent/migrations/V40__collaborate_on_governed_artifacts.sql"
    ).read_text(encoding="utf-8")

    assert "ai_artifact_team_preflights" in sql
    assert "ai_artifact_team_versions" in sql
    assert "uq_ai_artifact_team_open_conflict" in sql
    assert "revocation_receipt_sha256" in sql
    assert "NOT allowed AND reason_code IS NOT NULL" in sql
    assert "expires_at > created_at" in sql
    assert "reject_ai_audit_event_mutation" in sql
    assert "SHARE_REVOKED" in sql
    assert "ai_artifact_team_access_requests" in sql
    assert "REQUEST_ACCESS" in sql
    assert "ACCESS_REQUESTED" in sql
