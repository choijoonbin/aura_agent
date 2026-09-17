from __future__ import annotations

import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import connect
from psycopg.types.json import Jsonb

from dwp_agent.database_migrations import apply_migrations
from dwp_agent.deletion_retry_store import DeletionRetryStore
from dwp_agent.domain_retention_store import PostgresDomainRetentionStore
from dwp_agent.governed_domain_contracts import (
    DomainKey,
    RequestDeletionRequest,
    RetryDeletionRequest,
    UpsertRetentionPolicyRequest,
)
from dwp_agent.governed_domain_core import GovernedDomainConflict, GovernedDomainNotFound
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
        pytest.fail("Deletion retry tests require a dedicated test database.")
    apply_migrations(DATABASE_URL)


def test_retry_is_owner_scoped_session_bound_and_requeues_only_failed_targets() -> None:
    tenant_id = 880_000_000 + uuid4().int % 80_000_000
    identity = _identity(tenant_id)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    for domain in (DomainKey.ROUTINE, DomainKey.MEMORY, DomainKey.ARTIFACT):
        _policy(retention, identity, domain)
    job = retention.request_deletion(
        identity,
        RequestDeletionRequest(
            commandId=uuid4(), expectedRevision=0, reasonCode="USER_DATA_DELETE",
            changeReason="Delete governed personal data across the selected domains.",
            domains=[DomainKey.ROUTINE, DomainKey.MEMORY, DomainKey.ARTIFACT],
        ),
    )
    disposition_id = _make_partial_job(retention, identity, job.deletion_job_id)
    retention.upsert_policy(
        identity,
        DomainKey.ARTIFACT,
        UpsertRetentionPolicyRequest(
            commandId=uuid4(), expectedRevision=1,
            reasonCode="LEGAL_HOLD_ENABLED",
            changeReason="Preserve artifact records under an approved legal hold.",
            retentionDays=365, deletionGraceDays=7, legalHold=True,
        ),
    )
    command_id = uuid4()
    request = RetryDeletionRequest(
        commandId=command_id, expectedRevision=2,
        reasonCode="USER_RETRY_FAILED_DELETION",
        changeReason="Retry only failed targets after reviewing the prior evidence.",
    )
    retries = DeletionRetryStore(retention)
    result = retries.retry(identity, job.deletion_job_id, request)
    assert result.state.value == "REQUESTED"
    assert {target.domain: target.state.value for target in result.targets} == {
        DomainKey.ARTIFACT: "BLOCKED_LEGAL_HOLD",
        DomainKey.MEMORY: "REQUESTED",
        DomainKey.ROUTINE: "COMPLETED",
    }
    routine = next(target for target in result.targets if target.domain == DomainKey.ROUTINE)
    assert routine.disposition is not None
    assert routine.disposition.disposition_id == disposition_id
    assert retries.retry(identity, job.deletion_job_id, request) == result

    with pytest.raises(GovernedDomainConflict):
        retries.retry(_identity(tenant_id, session="other-session"), job.deletion_job_id, request)
    with pytest.raises(GovernedDomainNotFound):
        retries.retry(_identity(tenant_id, user="other-user"), job.deletion_job_id,
                      request.model_copy(update={"command_id": uuid4()}))

    with connect(DATABASE_URL) as connection:
        retry_row = connection.execute(
            "SELECT resulting_state FROM ai_data_deletion_retry_commands WHERE command_id = %s",
            (command_id,),
        ).fetchone()
        event_count = connection.execute(
            "SELECT COUNT(*) FROM ai_data_deletion_events WHERE deletion_job_id = %s AND event_type = 'RETRY_REQUESTED'",
            (job.deletion_job_id,),
        ).fetchone()[0]
        outbox_rows = connection.execute(
            """SELECT outbox_id, payload_envelope FROM ai_transactional_outbox
                WHERE aggregate_id = %s AND topic = 'ai.personal-data.deletion-requested.v1'
                ORDER BY created_at DESC""",
            (str(job.deletion_job_id),),
        ).fetchall()
    assert retry_row == ("REQUESTED",)
    assert event_count == 1
    payloads = [retention.codec.decrypt_json(
        row[1], tenant_id=tenant_id, resource_type="transactional-outbox",
        resource_id=str(row[0]), field="payload",
    ) for row in outbox_rows]
    payload = next(item for item in payloads if item.get("retryCommandId") == str(command_id))
    assert payload["retryCommandId"] == str(command_id)
    assert payload["domains"] == [DomainKey.MEMORY.value]


def test_retry_rejects_stale_or_nonfailed_jobs_and_history_is_latest_first() -> None:
    tenant_id = 800_000_000 + uuid4().int % 70_000_000
    identity = _identity(tenant_id)
    retention = PostgresDomainRetentionStore(DATABASE_URL)
    _policy(retention, identity, DomainKey.MEMORY)
    first = retention.request_deletion(identity, _delete_request(DomainKey.MEMORY))
    second = retention.request_deletion(identity, _delete_request(DomainKey.MEMORY))
    with connect(DATABASE_URL) as connection:
        connection.execute(
            "UPDATE ai_data_deletion_jobs SET requested_at = CURRENT_TIMESTAMP + INTERVAL '1 second' WHERE deletion_job_id = %s",
            (second.deletion_job_id,),
        )
        connection.execute(
            """UPDATE ai_data_deletion_jobs SET state = 'FAILED', attempt_count = 3,
                      completed_at = CURRENT_TIMESTAMP WHERE deletion_job_id = %s""",
            (first.deletion_job_id,),
        )
        connection.execute(
            """UPDATE ai_data_deletion_targets SET state = 'FAILED', safe_error_code = 'PROVIDER_FAILURE'
                WHERE deletion_job_id = %s""",
            (first.deletion_job_id,),
        )
    retries = DeletionRetryStore(retention)
    history = retries.list(identity)
    assert [item.deletion_job_id for item in history[:2]] == [second.deletion_job_id, first.deletion_job_id]
    assert retries.list(_identity(tenant_id, user="other-user")) == []
    with pytest.raises(GovernedDomainConflict):
        retries.retry(
            identity, first.deletion_job_id,
            RetryDeletionRequest(
                commandId=uuid4(), expectedRevision=2, reasonCode="STALE_RETRY",
                changeReason="Retry with a deliberately stale attempt count.",
            ),
        )
    retried = retries.retry(
        identity, first.deletion_job_id,
        RetryDeletionRequest(
            commandId=uuid4(), expectedRevision=3, reasonCode="RETRY_FAILED_DELETION",
            changeReason="Retry the failed target after checking that no legal hold applies.",
        ),
    )
    assert retried.state.value == "REQUESTED"
    assert [target.state.value for target in retried.targets] == ["REQUESTED"]
    with pytest.raises(GovernedDomainConflict):
        retries.retry(
            identity, second.deletion_job_id,
            RetryDeletionRequest(
                commandId=uuid4(), expectedRevision=0, reasonCode="INVALID_RETRY",
                changeReason="A requested deletion is not eligible for retry.",
            ),
        )


def _make_partial_job(
    store: PostgresDomainRetentionStore,
    identity: PersonalDomainIdentity,
    job_id,
):
    disposition_id = uuid4()
    receipt_payload = {
        "deletionJobId": str(job_id), "domain": DomainKey.ROUTINE.value,
        "generation": 2, "purgedRowCount": 3, "tableCounts": {"ai_personal_routines": 3},
        "dispositionScope": "AGENT_ACTIVE_POSTGRES_DOMAIN_ONLY",
        "dispositionMethod": "PHYSICAL_ROW_PURGE_OF_ENCRYPTED_RECORDS",
        "activeStoreEnvelopesDestroyed": True, "sourceSystemDataAffected": False,
        "backupDispositionState": "EXTERNAL_RETENTION_BOUNDARY",
    }
    fingerprint = store.fingerprints.value(
        tenant_id=identity.tenant_id,
        purpose="personal-data-disposition-receipt", payload=receipt_payload,
    )
    with connect(DATABASE_URL) as connection:
        connection.execute(
            """INSERT INTO ai_data_disposition_receipts (
                   disposition_id, deletion_job_id, tenant_id, user_id, domain_key,
                   generation, purged_row_count, table_counts,
                   active_store_envelopes_destroyed, source_system_data_affected,
                   backup_disposition_state, receipt_fingerprint)
               VALUES (%s,%s,%s,%s,'ROUTINE',2,3,%s,TRUE,FALSE,
                       'EXTERNAL_RETENTION_BOUNDARY',%s)""",
            (disposition_id, job_id, identity.tenant_id, identity.user_id,
             Jsonb({"ai_personal_routines": 3}), fingerprint),
        )
        connection.execute(
            """UPDATE ai_data_deletion_targets
                  SET state = CASE domain_key WHEN 'ROUTINE' THEN 'COMPLETED' ELSE 'FAILED' END,
                      affected_count = CASE domain_key WHEN 'ROUTINE' THEN 3 ELSE NULL END,
                      safe_error_code = CASE domain_key WHEN 'ROUTINE' THEN NULL ELSE 'PROVIDER_FAILURE' END,
                      disposition_id = CASE domain_key WHEN 'ROUTINE' THEN %s ELSE NULL END,
                      completed_at = CASE domain_key WHEN 'ROUTINE' THEN CURRENT_TIMESTAMP ELSE NULL END
                WHERE deletion_job_id = %s""",
            (disposition_id, job_id),
        )
        connection.execute(
            """UPDATE ai_data_deletion_jobs SET state = 'PARTIAL', generation = 2,
                      attempt_count = 2, completed_at = CURRENT_TIMESTAMP
                WHERE deletion_job_id = %s""",
            (job_id,),
        )
    return disposition_id


def _identity(
    tenant_id: int, *, user: str = "member-1", session: str = "session-1"
) -> PersonalDomainIdentity:
    return PersonalDomainIdentity(
        tenant_id=tenant_id, user_id=user, correlation_id=str(uuid4()),
        auth_session_id=session, roles=frozenset({"WORKSPACE_MEMBER"}),
        permissions=frozenset({"APP.ASK:VIEW", "APP.DWAION_PRIVACY:VIEW", "APP.DWAION_PRIVACY:MANAGE"}),
    )


def _policy(
    store: PostgresDomainRetentionStore,
    identity: PersonalDomainIdentity,
    domain: DomainKey,
) -> None:
    store.upsert_policy(
        identity, domain,
        UpsertRetentionPolicyRequest(
            commandId=uuid4(), expectedRevision=0,
            reasonCode="TENANT_RETENTION_BOOTSTRAP",
            changeReason=f"Set explicit retention for {domain.value.lower()} data.",
            retentionDays=365, deletionGraceDays=7, legalHold=False,
        ),
    )


def _delete_request(domain: DomainKey) -> RequestDeletionRequest:
    return RequestDeletionRequest(
        commandId=uuid4(), expectedRevision=0, reasonCode="USER_DATA_DELETE",
        changeReason=f"Delete my governed {domain.value.lower()} data.", domains=[domain],
    )
