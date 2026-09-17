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
    ResolveTeamArtifactConflictRequest,
    RunTeamArtifactPreflightRequest,
    TeamArtifactCapabilities,
    TeamArtifactMemberRequest,
    TeamArtifactShare,
)
from dwp_agent.artifact_collaboration_provider import (
    ArtifactCollaborationProvider,
    ArtifactCollaborationProviderConfiguration,
    ArtifactCollaborationProviderUnavailable,
)
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


class _CapabilityStore:
    @staticmethod
    def capabilities() -> TeamArtifactCapabilities:
        return TeamArtifactCapabilities(
            team_workspace_available=False,
            acl_preflight_available=False,
            collaboration_available=False,
            conflict_resolution_available=False,
            internal_sharing_available=False,
            external_sharing_available=False,
            share_expiry_available=False,
            share_revocation_available=False,
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
        "get_artifact_collaboration_store",
        lambda: _CapabilityStore(),
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
    assert allowed.json()["data"]["externalSharingAvailable"] is False


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
