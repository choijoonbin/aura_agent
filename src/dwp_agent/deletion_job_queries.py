from __future__ import annotations

from typing import Any
from uuid import UUID

from .governed_domain_contracts import (
    DataDispositionReceipt,
    DeletionJob,
    DeletionStage,
    DeletionJobState,
    DeletionTargetReceipt,
    DomainKey,
    LegalHoldEvidence,
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
        """SELECT deletion_job_id, state, requested_at, started_at, completed_at,
                  updated_at, attempt_count
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
                  receipt.receipt_fingerprint,
                  receipt.completed_at AS disposition_completed_at,
                  target.completed_at AS target_completed_at,
                  target.updated_at AS target_updated_at,
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
                completed_at=target["disposition_completed_at"],
            )
        hold = _legal_hold_evidence(
            connection,
            tenant_id=tenant_id,
            domain=DomainKey(target["domain_key"]),
            blocked=target["state"] == "BLOCKED_LEGAL_HOLD",
        )
        targets.append(
            DeletionTargetReceipt(
                domain=target["domain_key"],
                state=target["state"],
                affected_count=target["affected_count"],
                safe_error_code=target["safe_error_code"],
                disposition=disposition,
                legal_hold_evidence=hold,
            )
        )
    state = DeletionJobState(row["state"])
    legal_holds = [
        target.legal_hold_evidence
        for target in targets
        if target.legal_hold_evidence is not None
    ]
    stages = _deletion_stages(row, target_rows, targets, state)
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
        stages=stages,
        legal_holds=legal_holds,
    )


def _legal_hold_evidence(
    connection: Any,
    *,
    tenant_id: int,
    domain: DomainKey,
    blocked: bool,
) -> LegalHoldEvidence | None:
    if not blocked:
        return None
    row = connection.execute(
        """SELECT hold_id, hold_state, authority_reference, dpo_subject_id,
                  reason_code, effective_at, expires_at
             FROM ai_personal_data_legal_holds
            WHERE tenant_id = %s AND domain_key = %s AND hold_state = 'ACTIVE'
            ORDER BY effective_at DESC LIMIT 1""",
        (tenant_id, domain.value),
    ).fetchone()
    if row is None:
        return LegalHoldEvidence(
            available=False,
            domain=domain,
            reason_code="LEGAL_HOLD_DETAILS_NOT_RECORDED",
        )
    return LegalHoldEvidence(
        available=True,
        domain=domain,
        hold_id=row["hold_id"],
        state=row["hold_state"],
        authority_reference=row["authority_reference"],
        dpo_subject_id=row["dpo_subject_id"],
        reason_code=row["reason_code"],
        effective_at=row["effective_at"],
        expires_at=row["expires_at"],
    )


def _deletion_stages(row, target_rows, targets, state):
    states = [target.state.value for target in targets]
    dispositions = [target.disposition for target in targets if target.disposition]
    if states and all(value == "COMPLETED" for value in states):
        active_state, active_code = "COMPLETED", "ACTIVE_STORE_DISPOSITION_COMPLETED"
    elif states and all(value == "BLOCKED_LEGAL_HOLD" for value in states):
        active_state, active_code = "BLOCKED", "ACTIVE_STORE_BLOCKED_LEGAL_HOLD"
    elif any(value == "RUNNING" for value in states):
        active_state, active_code = "RUNNING", "ACTIVE_STORE_DISPOSITION_RUNNING"
    elif any(value == "FAILED" for value in states) and any(
        value == "COMPLETED" for value in states
    ):
        active_state, active_code = "PARTIAL", "ACTIVE_STORE_DISPOSITION_PARTIAL"
    elif any(value == "FAILED" for value in states):
        active_state, active_code = "FAILED", "ACTIVE_STORE_DISPOSITION_FAILED"
    else:
        active_state, active_code = "PENDING", "ACTIVE_STORE_DISPOSITION_PENDING"
    receipt_state = {
        DeletionJobState.COMPLETED: "COMPLETED",
        DeletionJobState.PARTIAL: "PARTIAL",
        DeletionJobState.BLOCKED_LEGAL_HOLD: "BLOCKED",
        DeletionJobState.FAILED: "FAILED",
        DeletionJobState.RUNNING: "RUNNING",
        DeletionJobState.REQUESTED: "PENDING",
    }[state]
    receipt_code = {
        "COMPLETED": "SERVER_DISPOSITION_RECEIPTS_FINALIZED",
        "PARTIAL": "SERVER_DISPOSITION_RECEIPTS_PARTIAL",
        "BLOCKED": "SERVER_DISPOSITION_RECEIPT_BLOCKED_LEGAL_HOLD",
        "FAILED": "SERVER_DISPOSITION_RECEIPT_FAILED",
        "RUNNING": "SERVER_DISPOSITION_RECEIPT_PENDING",
        "PENDING": "SERVER_DISPOSITION_RECEIPT_PENDING",
    }[receipt_state]
    observed = max(
        (
            target["target_completed_at"] or target["target_updated_at"]
            for target in target_rows
        ),
        default=None,
    )
    sole_fingerprint = (
        dispositions[0].receipt_fingerprint if len(dispositions) == 1 else None
    )
    return [
        DeletionStage(
            key="REQUEST_ACCEPTED",
            state="COMPLETED",
            detail_code="DELETION_REQUEST_ACCEPTED",
            observed_at=row["requested_at"],
            evidence_reference=str(row["deletion_job_id"]),
        ),
        DeletionStage(
            key="TARGETS_SCHEDULED",
            state="COMPLETED",
            detail_code="DELETION_TARGETS_RECORDED",
            observed_at=row["requested_at"],
            evidence_reference=f"targets:{len(targets)}",
        ),
        DeletionStage(
            key="ACTIVE_STORE_DISPOSITION",
            state=active_state,
            detail_code=active_code,
            observed_at=observed,
            evidence_reference=f"dispositions:{len(dispositions)}/targets:{len(targets)}",
            evidence_fingerprint=sole_fingerprint,
        ),
        DeletionStage(
            key="BACKUP_BOUNDARY",
            state="UNAVAILABLE",
            detail_code="BACKUP_DESTRUCTION_LOG_NOT_CONFIGURED",
            evidence_reference="EXTERNAL_RETENTION_BOUNDARY",
        ),
        DeletionStage(
            key="RECEIPT_FINALIZATION",
            state=receipt_state,
            detail_code=receipt_code,
            observed_at=row["completed_at"],
            evidence_reference=f"server-disposition-receipts:{len(dispositions)}",
            evidence_fingerprint=sole_fingerprint,
        ),
    ]


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
