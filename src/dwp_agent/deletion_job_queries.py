from __future__ import annotations

from typing import Any
from uuid import UUID

from .governed_domain_contracts import (
    DataDispositionReceipt,
    DeletionJob,
    DeletionJobState,
    DeletionTargetReceipt,
    DomainKey,
)
from .governed_domain_core import GovernedDomainNotFound, GovernedDomainUnavailable
from .governed_worker_runtime import governed_worker_available


def read_deletion_job(
    connection: Any,
    *,
    deletion_job_id: UUID,
    tenant_id: int,
    user_id: str,
    fingerprints: Any,
) -> DeletionJob:
    row = connection.execute(
        """SELECT deletion_job_id, state, requested_at, completed_at, attempt_count
             FROM ai_data_deletion_jobs
            WHERE deletion_job_id = %s AND tenant_id = %s AND user_id = %s""",
        (deletion_job_id, tenant_id, user_id),
    ).fetchone()
    if row is None:
        raise GovernedDomainNotFound("The deletion request is unavailable.")
    target_rows = connection.execute(
        """SELECT target.domain_key, target.state, target.affected_count,
                  target.safe_error_code, receipt.disposition_id,
                  receipt.generation, receipt.purged_row_count,
                  receipt.table_counts,
                  receipt.active_store_envelopes_destroyed,
                  receipt.source_system_data_affected,
                  receipt.backup_disposition_state,
                  receipt.receipt_fingerprint, receipt.completed_at,
                  receipt.deletion_job_id AS receipt_job_id,
                  receipt.tenant_id AS receipt_tenant_id,
                  receipt.user_id AS receipt_user_id,
                  receipt.domain_key AS receipt_domain_key
             FROM ai_data_deletion_targets AS target
             LEFT JOIN ai_data_disposition_receipts AS receipt
               ON receipt.disposition_id = target.disposition_id
            WHERE target.deletion_job_id = %s
            ORDER BY target.domain_key""",
        (deletion_job_id,),
    ).fetchall()
    targets = []
    for target in target_rows:
        disposition = None
        if target["disposition_id"] is not None:
            table_counts = dict(target["table_counts"] or {})
            if (
                target["receipt_job_id"] != deletion_job_id
                or target["receipt_tenant_id"] != tenant_id
                or target["receipt_user_id"] != user_id
                or target["receipt_domain_key"] != target["domain_key"]
            ):
                raise GovernedDomainUnavailable(
                    "Personal data disposition evidence failed binding validation."
                )
            receipt_payload = {
                "deletionJobId": str(deletion_job_id),
                "domain": target["receipt_domain_key"],
                "generation": target["generation"],
                "purgedRowCount": target["purged_row_count"],
                "tableCounts": table_counts,
                "dispositionScope": "AGENT_ACTIVE_POSTGRES_DOMAIN_ONLY",
                "dispositionMethod": "PHYSICAL_ROW_PURGE_OF_ENCRYPTED_RECORDS",
                "activeStoreEnvelopesDestroyed": target[
                    "active_store_envelopes_destroyed"
                ],
                "sourceSystemDataAffected": target["source_system_data_affected"],
                "backupDispositionState": target["backup_disposition_state"],
            }
            expected_fingerprint = fingerprints.value(
                tenant_id=tenant_id,
                purpose="personal-data-disposition-receipt",
                payload=receipt_payload,
            )
            if expected_fingerprint != target["receipt_fingerprint"]:
                raise GovernedDomainUnavailable(
                    "Personal data disposition evidence failed integrity validation."
                )
            disposition = DataDispositionReceipt(
                disposition_id=target["disposition_id"],
                domain=target["receipt_domain_key"],
                generation=target["generation"],
                purged_row_count=target["purged_row_count"],
                purged_table_counts=table_counts,
                active_store_envelopes_destroyed=target[
                    "active_store_envelopes_destroyed"
                ],
                source_system_data_affected=target["source_system_data_affected"],
                backup_disposition_state=target["backup_disposition_state"],
                receipt_fingerprint=target["receipt_fingerprint"],
                completed_at=target["completed_at"],
            )
        targets.append(
            DeletionTargetReceipt(
                domain=target["domain_key"],
                state=target["state"],
                affected_count=target["affected_count"],
                safe_error_code=target["safe_error_code"],
                disposition=disposition,
            )
        )
    state = DeletionJobState(row["state"])
    return DeletionJob(
        deletion_job_id=row["deletion_job_id"],
        state=state,
        domains=[target.domain for target in targets],
        requested_at=row["requested_at"],
        completed_at=row["completed_at"],
        deletion_performed=state == DeletionJobState.COMPLETED,
        deletion_execution_available=governed_worker_available("DATA_DELETION"),
        blocked_domains=[
            target.domain
            for target in targets
            if target.state.value == "BLOCKED_LEGAL_HOLD"
        ],
        attempt_count=row["attempt_count"],
        targets=targets,
    )


def deletion_result_envelope(
    codec: Any,
    job: DeletionJob,
    *,
    tenant_id: int,
) -> str:
    return codec.encrypt_json(
        job.model_dump(mode="json", by_alias=True),
        tenant_id=tenant_id,
        resource_type="data-deletion-job",
        resource_id=str(job.deletion_job_id),
        field="result",
    )
