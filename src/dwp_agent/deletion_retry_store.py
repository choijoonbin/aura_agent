from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .deletion_job_queries import deletion_result_envelope, read_deletion_job
from .governed_domain_contracts import DeletionJob, RetryDeletionRequest
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
    advisory_lock,
    require_command_replay,
)
from .personal_domain_security import PersonalDomainIdentity
from .transactional_outbox import enqueue_internal_intent


class DeletionRetryStore:
    def __init__(self, retention_store: Any) -> None:
        self.database_url = retention_store.database_url
        self.codec = retention_store.codec
        self.fingerprints = retention_store.fingerprints

    def list(self, identity: PersonalDomainIdentity, *, limit: int = 100) -> list[DeletionJob]:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    """SELECT deletion_job_id FROM ai_data_deletion_jobs
                        WHERE tenant_id = %s AND user_id = %s
                        ORDER BY requested_at DESC LIMIT %s""",
                    (identity.tenant_id, identity.user_id, limit),
                ).fetchall()
                return [
                    read_deletion_job(
                        connection, deletion_job_id=row["deletion_job_id"],
                        tenant_id=identity.tenant_id, user_id=identity.user_id,
                        fingerprints=self.fingerprints,
                    )
                    for row in rows
                ]
        except (PsycopgError, ValueError, TypeError) as error:
            raise GovernedDomainUnavailable("Personal data deletion history is unavailable.") from error

    def retry(
        self, identity: PersonalDomainIdentity, deletion_job_id: UUID,
        request: RetryDeletionRequest,
    ) -> DeletionJob:
        proof = self.fingerprints.command(
            tenant_id=identity.tenant_id, session_id=identity.auth_session_id,
            purpose="personal-data-deletion-retry",
            payload={"deletionJobId": str(deletion_job_id),
                     **request.model_dump(mode="json", by_alias=True)},
        )
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(connection, "personal-data-deletion-retry",
                              identity.tenant_id, identity.user_id, deletion_job_id)
                replay = connection.execute(
                    """SELECT deletion_job_id, session_fingerprint, request_fingerprint
                         FROM ai_data_deletion_retry_commands
                        WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
                    (identity.tenant_id, identity.user_id, request.command_id),
                ).fetchone()
                if replay is not None:
                    require_command_replay(replay["session_fingerprint"],
                                           replay["request_fingerprint"], proof)
                    if replay["deletion_job_id"] != deletion_job_id:
                        raise GovernedDomainConflict("The retry command is bound to another deletion request.")
                    return self._read(connection, identity, deletion_job_id)
                job = connection.execute(
                    """SELECT state, generation, attempt_count, retention_until
                         FROM ai_data_deletion_jobs
                        WHERE deletion_job_id = %s AND tenant_id = %s AND user_id = %s
                        FOR UPDATE""",
                    (deletion_job_id, identity.tenant_id, identity.user_id),
                ).fetchone()
                if job is None:
                    raise GovernedDomainNotFound("The deletion request is unavailable.")
                if job["state"] not in {"PARTIAL", "FAILED"}:
                    raise GovernedDomainConflict("Only a partial or failed deletion may be retried.")
                if int(job["attempt_count"]) != request.expected_revision:
                    raise GovernedDomainConflict("The deletion attempt count changed. Reload and retry.")
                failed = connection.execute(
                    """SELECT domain_key FROM ai_data_deletion_targets
                        WHERE deletion_job_id = %s AND state = 'FAILED' FOR UPDATE""",
                    (deletion_job_id,),
                ).fetchall()
                failed_domains = [row["domain_key"] for row in failed]
                if not failed_domains:
                    raise GovernedDomainConflict("The deletion request has no failed target to retry.")
                held = connection.execute(
                    """SELECT domain_key FROM ai_domain_retention_policies
                        WHERE tenant_id = %s AND domain_key = ANY(%s) AND legal_hold""",
                    (identity.tenant_id, failed_domains),
                ).fetchall()
                held_domains = {row["domain_key"] for row in held}
                retry_domains = [domain for domain in failed_domains if domain not in held_domains]
                connection.execute(
                    """UPDATE ai_data_deletion_targets
                          SET state = CASE WHEN domain_key = ANY(%s)
                              THEN 'BLOCKED_LEGAL_HOLD' ELSE 'REQUESTED' END,
                              affected_count = NULL, safe_error_code = NULL,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE deletion_job_id = %s AND domain_key = ANY(%s)""",
                    (list(held_domains), deletion_job_id, failed_domains),
                )
                next_state = "REQUESTED" if retry_domains else self._terminal_state(connection, deletion_job_id)
                now = connection.execute("SELECT CURRENT_TIMESTAMP AS now").fetchone()["now"]
                updated = connection.execute(
                    """UPDATE ai_data_deletion_jobs
                          SET state = %s, lease_token = NULL, lease_expires_at = NULL,
                              started_at = NULL,
                              completed_at = CASE WHEN %s = 'REQUESTED' THEN NULL ELSE %s END,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE deletion_job_id = %s AND tenant_id = %s
                    RETURNING generation""",
                    (next_state, next_state, now, deletion_job_id, identity.tenant_id),
                ).fetchone()
                reason_envelope = self.codec.encrypt_json(
                    {"changeReason": request.change_reason}, tenant_id=identity.tenant_id,
                    resource_type="data-deletion-retry", resource_id=str(request.command_id),
                    field="change-reason",
                )
                connection.execute(
                    """INSERT INTO ai_data_deletion_retry_commands (
                           tenant_id, user_id, command_id, deletion_job_id,
                           expected_attempt_count, session_fingerprint, request_fingerprint,
                           reason_code, change_reason_envelope, resulting_state,
                           resulting_generation)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (identity.tenant_id, identity.user_id, request.command_id,
                     deletion_job_id, request.expected_revision, proof.session_fingerprint,
                     proof.request_fingerprint, request.reason_code, reason_envelope,
                     next_state, updated["generation"]),
                )
                connection.execute(
                    """INSERT INTO ai_data_deletion_events (
                           event_id, deletion_job_id, tenant_id, user_id, actor_user_id,
                           correlation_id, event_type, previous_state, current_state,
                           generation)
                       VALUES (%s, %s, %s, %s, %s, %s, 'RETRY_REQUESTED', %s, %s, %s)""",
                    (uuid4(), deletion_job_id, identity.tenant_id, identity.user_id,
                     identity.user_id, identity.correlation_id, job["state"], next_state,
                     updated["generation"]),
                )
                if retry_domains:
                    enqueue_internal_intent(
                        connection, codec=self.codec, fingerprints=self.fingerprints,
                        tenant_id=identity.tenant_id, user_id=identity.user_id,
                        topic="ai.personal-data.deletion-requested.v1",
                        aggregate_type="DATA_DELETION_JOB", aggregate_id=str(deletion_job_id),
                        payload={"deletionJobId": str(deletion_job_id),
                                 "domains": retry_domains,
                                 "retryCommandId": str(request.command_id),
                                 "expectedAttemptCount": request.expected_revision,
                                 "activeStoreDispositionRequested": True,
                                 "externalWritePerformed": False,
                                 "sourceSystemDataAffected": False,
                                 "backupDispositionState": "EXTERNAL_RETENTION_BOUNDARY"},
                        retention_until=job["retention_until"],
                    )
                result = self._read(connection, identity, deletion_job_id)
                connection.execute(
                    "UPDATE ai_data_deletion_jobs SET result_envelope = %s WHERE deletion_job_id = %s",
                    (deletion_result_envelope(self.codec, result, tenant_id=identity.tenant_id),
                     deletion_job_id),
                )
                return result
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise GovernedDomainUnavailable("Personal data deletion retry is unavailable.") from error

    def _read(self, connection: Any, identity: PersonalDomainIdentity,
              deletion_job_id: UUID) -> DeletionJob:
        return read_deletion_job(
            connection, deletion_job_id=deletion_job_id,
            tenant_id=identity.tenant_id, user_id=identity.user_id,
            fingerprints=self.fingerprints,
        )

    @staticmethod
    def _terminal_state(connection: Any, deletion_job_id: UUID) -> str:
        states = [row["state"] for row in connection.execute(
            "SELECT state FROM ai_data_deletion_targets WHERE deletion_job_id = %s",
            (deletion_job_id,),
        ).fetchall()]
        if all(state == "BLOCKED_LEGAL_HOLD" for state in states):
            return "BLOCKED_LEGAL_HOLD"
        if "FAILED" in states:
            return "FAILED"
        if all(state == "COMPLETED" for state in states):
            return "COMPLETED"
        return "PARTIAL"
