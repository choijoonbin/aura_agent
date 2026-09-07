from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dwp_agent import artifact_api, domain_retention_api, personal_memory_api, personal_routine_api
from dwp_agent.artifact_contracts import GovernedArtifact
from dwp_agent.governed_domain_contracts import RetentionPolicy
from dwp_agent.personal_memory_contracts import PersonalAiControls


class _MemoryStore:
    def __init__(self) -> None:
        self.calls = 0

    def controls(self, _identity):
        self.calls += 1
        return PersonalAiControls(
            memory_state="UNSET",
            revision=0,
            memory_enabled=False,
            memory_effective=False,
        )


class _ListStore:
    def list(self, _identity):
        return []


class _ArtifactStore(_ListStore):
    def __init__(self) -> None:
        self.create_calls = 0

    def create(self, _identity, request):
        self.create_calls += 1
        now = datetime.now(UTC)
        return GovernedArtifact(
            artifact_id="4053a568-7bd0-4bd9-a39c-ee0d6e12e51a",
            artifact_type=request.artifact_type,
            state="DRAFT",
            revision=1,
            draft_revision=1,
            current_version_number=0,
            content=request.content,
            sources=request.sources,
            created_at=now,
            updated_at=now,
        )


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, _MemoryStore, _ArtifactStore]:
    monkeypatch.setenv("DWP_AGENT_SERVICE_TOKEN", "personal-domain-service-token")
    monkeypatch.delenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", raising=False)
    memory = _MemoryStore()
    artifacts = _ArtifactStore()
    monkeypatch.setattr(personal_memory_api, "get_personal_memory_store", lambda: memory)
    monkeypatch.setattr(personal_routine_api, "get_personal_routine_store", lambda: _ListStore())
    monkeypatch.setattr(artifact_api, "get_artifact_store", lambda: artifacts)
    monkeypatch.setattr(domain_retention_api, "get_domain_retention_store", lambda: _RetentionStore())
    app = FastAPI()
    app.include_router(personal_memory_api.router)
    app.include_router(personal_routine_api.router)
    app.include_router(artifact_api.router)
    app.include_router(domain_retention_api.router)
    app.include_router(domain_retention_api.admin_router)
    return TestClient(app), memory, artifacts


class _RetentionStore:
    def policies(self, _identity):
        return []

    def upsert_policy(self, _identity, domain, request):
        return RetentionPolicy(
            domain=domain,
            retention_days=request.retention_days,
            deletion_grace_days=request.deletion_grace_days,
            legal_hold=request.legal_hold,
            revision=1,
            updated_at=datetime.now(UTC),
        )


def _headers(*permissions: str, **overrides: str) -> dict[str, str]:
    headers = {
        "X-DWP-Service-Token": "personal-domain-service-token",
        "X-DWP-Tenant-ID": "7001",
        "X-DWP-User-ID": "member-1",
        "X-Correlation-ID": "personal-domain-test",
        "X-DWP-Auth-Session-ID": "session-1",
        "X-DWP-Identity-Plane": "TENANT",
        "X-DWP-Access-Mode": "NORMAL",
        "X-DWP-Roles": "WORKSPACE_MEMBER",
        "X-DWP-Permissions": ",".join(permissions),
    }
    headers.update(overrides)
    return headers


@pytest.mark.parametrize(
    ("path", "permission"),
    (
        ("/v1/ai-controls", "APP.DWAION_MEMORY:VIEW"),
        ("/v1/routines", "APP.DWAION_ROUTINES:VIEW"),
        ("/v1/artifacts", "APP.DWAION_ARTIFACTS:VIEW"),
        ("/v1/personal-data/retention", "APP.DWAION_PRIVACY:VIEW"),
        ("/v1/personal-data/capabilities", "APP.DWAION_PRIVACY:VIEW"),
    ),
)
def test_exact_personal_domain_grant_allows_each_read_surface(
    client: tuple[TestClient, _MemoryStore, _ArtifactStore], path: str, permission: str
) -> None:
    http, _, _ = client

    response = http.get(path, headers=_headers("APP.ASK:VIEW", permission))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "headers",
    (
        _headers("APP.ASK:VIEW", "APP.DWAION_MEMORY:VIEW", **{"X-DWP-Identity-Plane": "PROVIDER"}),
        _headers("APP.ASK:VIEW", "APP.DWAION_MEMORY:VIEW", **{"X-DWP-Access-Mode": "SUPPORT"}),
        _headers("APP.ASK:VIEW", "APP.DWAION_MEMORY:VIEW", **{"X-DWP-Roles": "PROVIDER_ADMIN"}),
        _headers("APP.ASK:VIEW", "APP.DWAION_MEMORY:VIEW", **{"X-DWP-Support-Session-ID": "support-1"}),
    ),
)
def test_provider_and_support_contexts_fail_closed(
    client: tuple[TestClient, _MemoryStore, _ArtifactStore], headers: dict[str, str]
) -> None:
    http, memory, _ = client

    response = http.get("/v1/ai-controls", headers=headers)

    assert response.status_code == 403
    assert memory.calls == 0


def test_app_ask_alone_cannot_open_new_personal_domains(
    client: tuple[TestClient, _MemoryStore, _ArtifactStore],
) -> None:
    http, memory, _ = client

    response = http.get("/v1/ai-controls", headers=_headers("APP.ASK:VIEW"))

    assert response.status_code == 403
    assert memory.calls == 0


def test_auth_session_is_required_and_bound_before_store_use(
    client: tuple[TestClient, _MemoryStore, _ArtifactStore],
) -> None:
    http, memory, _ = client
    headers = _headers("APP.ASK:VIEW", "APP.DWAION_MEMORY:VIEW")
    del headers["X-DWP-Auth-Session-ID"]

    response = http.get("/v1/ai-controls", headers=headers)

    assert response.status_code == 422
    assert memory.calls == 0


def test_duplicate_tenant_identity_header_is_rejected_before_store_use(
    client: tuple[TestClient, _MemoryStore, _ArtifactStore],
) -> None:
    http, memory, _ = client
    headers = list(
        _headers("APP.ASK:VIEW", "APP.DWAION_MEMORY:VIEW").items()
    )
    headers.append(("X-DWP-Tenant-ID", "7002"))

    response = http.get("/v1/ai-controls", headers=headers)

    assert response.status_code == 403
    assert memory.calls == 0


def test_signed_identity_is_required_when_runtime_signing_is_enabled(
    client: tuple[TestClient, _MemoryStore, _ArtifactStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, memory, _ = client
    monkeypatch.setenv(
        "DWP_AGENT_IDENTITY_SIGNING_SECRET",
        "personal-domain-signing-secret-at-least-32-characters",
    )

    response = http.get(
        "/v1/ai-controls",
        headers=_headers("APP.ASK:VIEW", "APP.DWAION_MEMORY:VIEW"),
    )

    assert response.status_code == 401
    assert memory.calls == 0


def test_personal_privacy_grant_cannot_change_tenant_retention_policy(
    client: tuple[TestClient, _MemoryStore, _ArtifactStore],
) -> None:
    http, _, _ = client

    response = http.put(
        "/v1/admin/personal-data/retention/MEMORY",
        headers=_headers("APP.ASK:VIEW", "APP.DWAION_PRIVACY:MANAGE"),
        json={
            "commandId": "c4a62564-ea34-4f78-b257-11736005747b",
            "expectedRevision": 0,
            "reasonCode": "TENANT_RETENTION_CHANGE",
            "changeReason": "Attempt tenant-wide retention policy change.",
            "retentionDays": 365,
            "deletionGraceDays": 7,
            "legalHold": False,
        },
    )

    assert response.status_code == 403


def test_artifact_create_uses_exact_create_not_update_authority(
    client: tuple[TestClient, _MemoryStore, _ArtifactStore],
) -> None:
    http, _, artifacts = client
    payload = {
        "commandId": "95029013-b2d2-4b03-b7ed-fc6e997c58f1",
        "expectedRevision": 0,
        "reasonCode": "USER_ARTIFACT_CREATE",
        "artifactType": "DOCUMENT",
        "content": {"title": "Review", "body": "Draft for governed review."},
    }

    denied = http.post(
        "/v1/artifacts",
        headers=_headers("APP.ASK:VIEW", "APP.DWAION_ARTIFACTS:UPDATE"),
        json=payload,
    )
    allowed = http.post(
        "/v1/artifacts",
        headers=_headers("APP.ASK:VIEW", "APP.DWAION_ARTIFACTS:CREATE"),
        json=payload,
    )

    assert denied.status_code == 403
    assert allowed.status_code == 201
    assert artifacts.create_calls == 1


def test_retention_admin_reuses_existing_governance_authority(
    client: tuple[TestClient, _MemoryStore, _ArtifactStore],
) -> None:
    http, _, _ = client

    response = http.put(
        "/v1/admin/personal-data/retention/MEMORY",
        headers=_headers(
            "ADMIN.DWAION_RETENTION:MANAGE",
            **{"X-DWP-Roles": "DWAION_GOVERNANCE_MANAGER"},
        ),
        json={
            "commandId": "00e97b16-914a-4227-8b40-6a3aba92ae1d",
            "expectedRevision": 0,
            "reasonCode": "TENANT_RETENTION_CHANGE",
            "changeReason": "Set the governed memory retention period for this tenant.",
            "retentionDays": 365,
            "deletionGraceDays": 7,
            "legalHold": False,
        },
    )

    assert response.status_code == 200
