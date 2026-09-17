from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg import connect

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.canonical_json import canonical_json_bytes
from dwp_agent.domain_retention_store import PostgresDomainRetentionStore
from dwp_agent.governed_domain_contracts import (
    DomainKey,
    LegalHoldDirective,
    RequestDeletionRequest,
    UpsertRetentionPolicyRequest,
)
from dwp_agent.governed_domain_core import GovernedDomainConflict, GovernedDomainNotFound
from dwp_agent.personal_data_evidence_contracts import (
    CreatePersonalDataEvidenceCommandRequest,
    PersonalDataEvidenceAction,
    PersonalDataEvidenceCommandState,
)
from dwp_agent.personal_data_certificate_attestation import (
    KEY_ID_ENV,
    PUBLIC_KEY_ENV,
    certificate_signing_payload,
)
from dwp_agent.personal_data_evidence_provider import (
    PersonalDataEvidenceProviderContext,
    PersonalDataEvidenceProviderError,
    PersonalDataEvidenceProviderResponse,
)
from dwp_agent.personal_data_evidence_store import PersonalDataEvidenceStore
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
    name = urlparse(DATABASE_URL).path.removeprefix("/")
    if not name.endswith(("_integration", "_test", "_verify")):
        pytest.fail("Personal-data evidence tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)


class _Provider:
    configured = True

    def __init__(
        self,
        *,
        fail: bool = False,
        signing_key: Ed25519PrivateKey | None = None,
        tamper_job_binding: bool = False,
        tamper_outcome: bool = False,
        tamper_hold_binding: bool = False,
    ) -> None:
        self.fail = fail
        self.signing_key = signing_key
        self.tamper_job_binding = tamper_job_binding
        self.tamper_outcome = tamper_outcome
        self.tamper_hold_binding = tamper_hold_binding
        self.calls: list[PersonalDataEvidenceProviderContext] = []

    def execute(
        self, context: PersonalDataEvidenceProviderContext
    ) -> PersonalDataEvidenceProviderResponse:
        self.calls.append(context)
        if self.fail:
            raise PersonalDataEvidenceProviderError(
                "PROVIDER_TEMPORARILY_UNAVAILABLE",
                "Retry with the same command ID after the provider recovers.",
            )
        provider_receipt_id = f"receipt-{context.command_id}"
        values: dict[str, object] = {
            "commandId": context.command_id,
            "deletionJobId": context.deletion_job_id,
            "action": context.action,
            "evidenceDigest": context.evidence_digest,
            "providerReceiptId": provider_receipt_id,
            "state": "COMPLETED",
            "result": _provider_outcome(context),
        }
        if context.action == PersonalDataEvidenceAction.SIGNED_CERTIFICATE:
            assert self.signing_key is not None
            signature = self.signing_key.sign(
                certificate_signing_payload(context, provider_receipt_id)
            )
            values.update(
                signature=base64.urlsafe_b64encode(signature).rstrip(b"=").decode(),
                signingKeyId="tenant-key-1",
                signingAlgorithm="ED25519",
            )
        response = PersonalDataEvidenceProviderResponse.model_validate(values)
        if self.tamper_job_binding:
            response = response.model_copy(update={"deletion_job_id": uuid4()})
        if self.tamper_outcome:
            response = response.model_copy(update={"result": {}})
        if self.tamper_hold_binding:
            response = response.model_copy(
                update={"result": {**response.result, "holdId": str(uuid4())}}
            )
        return response


def _provider_outcome(context: PersonalDataEvidenceProviderContext) -> dict[str, object]:
    observed_at = "2026-09-17T00:00:00Z"
    if context.action == PersonalDataEvidenceAction.BACKUP_LEDGER:
        return {
            "schemaVersion": 1,
            "ledgerScope": context.parameters.get("ledgerScope", "latest"),
            "destroyedPartitionIds": ["backup-partition-1"],
            "retainedPartitionIds": [],
            "ledgerEntryIds": ["ledger-entry-1"],
            "observedAt": observed_at,
        }
    if context.action == PersonalDataEvidenceAction.SRE_ESCALATION:
        return {
            "schemaVersion": 1,
            "caseId": "case-1",
            "queue": "privacy-sre",
            "severity": context.parameters.get("priority", "P2"),
            "state": "ACCEPTED",
            "acceptedAt": observed_at,
        }
    if context.action == PersonalDataEvidenceAction.LEGAL_HOLD_APPEAL:
        deletion_job = context.evidence.get("deletionJob", {})
        holds = deletion_job.get("legalHolds", []) if isinstance(deletion_job, dict) else []
        hold_id = next(
            (
                hold.get("holdId")
                for hold in holds
                if isinstance(hold, dict)
                and hold.get("available") is True
                and hold.get("state") == "ACTIVE"
            ),
            str(uuid4()),
        )
        return {
            "schemaVersion": 1,
            "holdId": hold_id,
            "appealId": "appeal-1",
            "requestParametersSha256": hashlib.sha256(
                canonical_json_bytes(dict(context.parameters))
            ).hexdigest(),
            "state": "SUBMITTED",
            "submittedAt": observed_at,
        }
    if context.action == PersonalDataEvidenceAction.SIEM_SYNC:
        return {
            "schemaVersion": 1,
            "syncId": "sync-1",
            "destination": context.parameters.get("destination", "tenant-siem"),
            "acceptedEventCount": 1,
            "sourceDigest": context.evidence_digest,
            "acceptedAt": observed_at,
        }
    return {}


def test_internal_json_downloads_are_owner_bound_fingerprinted_and_audited() -> None:
    tenant = 730_000_000 + uuid4().int % 10_000_000
    identity = _identity(tenant)
    retention, job_id = _job(identity)
    evidence = PersonalDataEvidenceStore(retention)

    legal = evidence.legal_hold_snapshot(identity, job_id)
    index = evidence.receipt_index(identity)

    legal_value = json.loads(legal.content)
    index_value = json.loads(index.content)
    assert legal.media_type == "application/json"
    assert legal_value["tenantId"] == tenant
    assert legal_value["userId"] == identity.user_id
    assert legal_value["deletionJobId"] == str(job_id)
    assert {item["deletionJobId"] for item in index_value["receipts"]} >= {str(job_id)}
    assert len(legal.fingerprint) == len(index.fingerprint) == 64

    with pytest.raises(GovernedDomainNotFound):
        evidence.legal_hold_snapshot(_identity(tenant, user="other-user"), job_id)
    with connect(DATABASE_URL) as connection:
        events = connection.execute(
            """SELECT download_type, evidence_fingerprint
                 FROM ai_personal_data_evidence_download_events
                WHERE tenant_id = %s AND user_id = %s""",
            (tenant, identity.user_id),
        ).fetchall()
    assert {row[0] for row in events} >= {"LEGAL_HOLD_SNAPSHOT", "RECEIPT_INDEX"}
    assert all(len(row[1]) == 64 for row in events)


def test_provider_actions_are_encrypted_idempotent_and_tenant_fenced() -> None:
    tenant = 740_000_000 + uuid4().int % 10_000_000
    identity = _identity(tenant)
    retention, job_id = _job(identity)
    store = PersonalDataEvidenceStore(retention)
    provider = _Provider()
    command_id = uuid4()
    request = CreatePersonalDataEvidenceCommandRequest(
        commandId=command_id,
        expectedRevision=0,
        reasonCode="USER_BACKUP_EVIDENCE",
        changeReason="Retrieve the signed backup destruction ledger for this request.",
        parameters={"ledgerScope": "latest"},
    )

    result = store.execute(
        identity,
        job_id,
        PersonalDataEvidenceAction.BACKUP_LEDGER,
        request,
        provider=provider,
    )
    replay = store.execute(
        identity,
        job_id,
        PersonalDataEvidenceAction.BACKUP_LEDGER,
        request,
        provider=provider,
    )

    assert result.state == PersonalDataEvidenceCommandState.COMPLETED
    assert result == replay
    assert result.receipt_id and result.result_fingerprint
    assert len(provider.calls) == 1
    with pytest.raises(GovernedDomainConflict):
        store.execute(
            _identity(tenant, session="other-session"),
            job_id,
            PersonalDataEvidenceAction.BACKUP_LEDGER,
            request,
            provider=provider,
        )
    with pytest.raises(GovernedDomainNotFound):
        store.get(_identity(tenant, user="other-user"), job_id, command_id)

    with connect(DATABASE_URL) as connection:
        row = connection.execute(
            """SELECT request_envelope, result_envelope, state
                 FROM ai_personal_data_evidence_commands WHERE command_id = %s""",
            (command_id,),
        ).fetchone()
        events = connection.execute(
            """SELECT event_type FROM ai_personal_data_evidence_command_events
                WHERE command_id = %s ORDER BY occurred_at""",
            (command_id,),
        ).fetchall()
    assert row[0].startswith("dwp2.") and row[1].startswith("dwp2.")
    assert row[2] == "COMPLETED"
    assert [value[0] for value in events] == ["REQUESTED", "COMPLETED"]


def test_provider_failure_has_retryable_terminal_receipt() -> None:
    tenant = 750_000_000 + uuid4().int % 10_000_000
    identity = _identity(tenant)
    retention, job_id = _job(identity)
    command = PersonalDataEvidenceStore(retention).execute(
        identity,
        job_id,
        PersonalDataEvidenceAction.SRE_ESCALATION,
        CreatePersonalDataEvidenceCommandRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_SRE_ESCALATION",
            changeReason="Escalate the failed personal-data deletion with governed evidence.",
            parameters={"priority": "P2"},
        ),
        provider=_Provider(fail=True),
    )
    assert command.state == PersonalDataEvidenceCommandState.FAILED
    assert command.receipt_id and command.result_fingerprint
    assert command.safe_error_code == "PROVIDER_TEMPORARILY_UNAVAILABLE"
    assert "same command ID" in (command.recovery_hint or "")


@pytest.mark.parametrize(
    ("provider", "safe_error_code"),
    [
        (
            _Provider(tamper_job_binding=True),
            "PERSONAL_DATA_EVIDENCE_PROVIDER_BINDING_INVALID",
        ),
        (
            _Provider(tamper_outcome=True),
            "PERSONAL_DATA_EVIDENCE_PROVIDER_RECEIPT_INVALID",
        ),
    ],
)
def test_store_rejects_injected_provider_receipts_that_bypass_model_validation(
    provider: _Provider,
    safe_error_code: str,
) -> None:
    tenant = 755_000_000 + uuid4().int % 10_000_000
    identity = _identity(tenant)
    retention, job_id = _job(identity)
    command = PersonalDataEvidenceStore(retention).execute(
        identity,
        job_id,
        PersonalDataEvidenceAction.BACKUP_LEDGER,
        CreatePersonalDataEvidenceCommandRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_BACKUP_EVIDENCE",
            changeReason="Reject a provider result that is not bound to this deletion job.",
            parameters={"ledgerScope": "latest"},
        ),
        provider=provider,
    )
    assert command.state == PersonalDataEvidenceCommandState.FAILED
    assert command.safe_error_code == safe_error_code
    assert command.provider_receipt_id is None


def test_legal_hold_appeal_binds_active_hold_and_normalized_reason() -> None:
    tenant = 757_000_000 + uuid4().int % 10_000_000
    identity = _identity(tenant)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    retention.upsert_policy(
        identity,
        DomainKey.MEMORY,
        UpsertRetentionPolicyRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="DPO_LEGAL_HOLD",
            changeReason="Apply a governed legal hold before testing its appeal binding.",
            retentionDays=365,
            deletionGraceDays=7,
            legalHold=True,
            legalHoldDirective=LegalHoldDirective(
                authorityReference="legal-case-2026-0917",
                dpoSubjectId="dpo@example.com",
                reasonCode="ACTIVE_INVESTIGATION",
                effectiveAt=datetime.now(UTC),
            ),
        ),
    )
    job = retention.request_deletion(
        identity,
        RequestDeletionRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_DATA_DELETE",
            changeReason="Request deletion to establish the active legal-hold evidence.",
            domains=[DomainKey.MEMORY],
        ),
    )
    request = CreatePersonalDataEvidenceCommandRequest(
        commandId=uuid4(),
        expectedRevision=0,
        reasonCode="USER_LEGAL_HOLD_APPEAL",
        changeReason="Submit a governed appeal against the active legal hold.",
        parameters={"appealReason": "The deletion owner requests DPO review."},
    )
    completed = PersonalDataEvidenceStore(retention).execute(
        identity,
        job.deletion_job_id,
        PersonalDataEvidenceAction.LEGAL_HOLD_APPEAL,
        request,
        provider=_Provider(),
    )
    rejected = PersonalDataEvidenceStore(retention).execute(
        identity,
        job.deletion_job_id,
        PersonalDataEvidenceAction.LEGAL_HOLD_APPEAL,
        request.model_copy(update={"command_id": uuid4()}),
        provider=_Provider(tamper_hold_binding=True),
    )

    assert completed.state == PersonalDataEvidenceCommandState.COMPLETED
    assert completed.result and completed.result["state"] == "SUBMITTED"
    assert rejected.state == PersonalDataEvidenceCommandState.FAILED
    assert (
        rejected.safe_error_code
        == "PERSONAL_DATA_EVIDENCE_PROVIDER_RECEIPT_INVALID"
    )


def test_signed_certificate_is_kms_bound_downloadable_and_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = 760_000_000 + uuid4().int % 10_000_000
    identity = _identity(tenant)
    retention, job_id = _job(identity)
    store = PersonalDataEvidenceStore(retention)
    signing_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        PUBLIC_KEY_ENV,
        signing_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )
    monkeypatch.setenv(KEY_ID_ENV, "tenant-key-1")
    command = store.execute(
        identity,
        job_id,
        PersonalDataEvidenceAction.SIGNED_CERTIFICATE,
        CreatePersonalDataEvidenceCommandRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_SIGNED_CERTIFICATE",
            changeReason="Generate a tenant-key signed personal-data deletion certificate.",
            parameters={"locale": "en-US"},
        ),
        provider=_Provider(signing_key=signing_key),
    )
    download = store.certificate(identity, job_id, command.command_id)

    assert command.download_available is True
    assert command.result and command.result["signingKeyId"] == "tenant-key-1"
    assert len(command.result["signedPayloadSha256"]) == 64
    assert len(command.result["signingKeyFingerprint"]) == 64
    assert download.content.startswith(b"%PDF-1.4")
    assert download.media_type == "application/pdf"
    assert len(download.fingerprint) == 64
    with connect(DATABASE_URL) as connection:
        audit = connection.execute(
            """SELECT download_type FROM ai_personal_data_evidence_download_events
                WHERE command_id = %s""",
            (command.command_id,),
        ).fetchone()
    assert audit == ("SIGNED_CERTIFICATE",)


def test_signed_certificate_rejects_an_untrusted_provider_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = 770_000_000 + uuid4().int % 10_000_000
    identity = _identity(tenant)
    retention, job_id = _job(identity)
    trusted_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        PUBLIC_KEY_ENV,
        trusted_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )
    monkeypatch.setenv(KEY_ID_ENV, "tenant-key-1")

    command = PersonalDataEvidenceStore(retention).execute(
        identity,
        job_id,
        PersonalDataEvidenceAction.SIGNED_CERTIFICATE,
        CreatePersonalDataEvidenceCommandRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_SIGNED_CERTIFICATE",
            changeReason="Reject a certificate that is not signed by the trusted tenant key.",
            parameters={"locale": "en-US"},
        ),
        provider=_Provider(signing_key=Ed25519PrivateKey.generate()),
    )

    assert command.state == PersonalDataEvidenceCommandState.FAILED
    assert command.download_available is False
    assert command.safe_error_code == "SIGNED_DELETION_CERTIFICATE_SIGNATURE_INVALID"
    assert command.result == {"providerAccepted": False}


def _identity(
    tenant: int,
    *,
    user: str = "member-1",
    session: str = "session-1",
) -> PersonalDomainIdentity:
    return PersonalDomainIdentity(
        tenant_id=tenant,
        user_id=user,
        correlation_id=f"evidence-{uuid4()}",
        auth_session_id=session,
        roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset(
            {
                "APP.ASK:VIEW",
                "APP.DWAION_PRIVACY:VIEW",
                "APP.DWAION_PRIVACY:MANAGE",
            }
        ),
    )


def _job(identity: PersonalDomainIdentity) -> tuple[PostgresDomainRetentionStore, object]:
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    retention.upsert_policy(
        identity,
        DomainKey.MEMORY,
        UpsertRetentionPolicyRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_RETENTION_POLICY",
            changeReason="Set the personal memory retention policy before deletion.",
            retentionDays=365,
            deletionGraceDays=7,
            legalHold=False,
        ),
    )
    job = retention.request_deletion(
        identity,
        RequestDeletionRequest(
            commandId=uuid4(),
            expectedRevision=0,
            reasonCode="USER_DATA_DELETE",
            changeReason="Delete governed personal memory under the configured policy.",
            domains=[DomainKey.MEMORY],
        ),
    )
    return retention, job.deletion_job_id
