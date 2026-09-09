from __future__ import annotations

import os
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .governed_domain_contracts import (
    DeletionJob,
    DeletionJobState,
    DomainKey,
    RequestDeletionRequest,
    RetentionPolicy,
    UpsertRetentionPolicyRequest,
)
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
    GovernedFingerprints,
    GovernedPayloadCodec,
    advisory_lock,
    require_command_replay,
    retention_deadline,
)
from .personal_domain_security import PersonalDomainIdentity
from .deletion_job_queries import read_deletion_job
from .transactional_outbox import enqueue_internal_intent


class PostgresDomainRetentionStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise GovernedDomainUnavailable(
                "Personal data governance encryption is unavailable."
            ) from error

    def policies(self, identity: PersonalDomainIdentity) -> list[RetentionPolicy]:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    """SELECT domain_key, retention_days, deletion_grace_days,
                              legal_hold, revision, updated_at
                         FROM ai_domain_retention_policies
                        WHERE tenant_id = %s ORDER BY domain_key""",
                    (identity.tenant_id,),
                ).fetchall()
                return [_policy(row) for row in rows]
        except PsycopgError as error:
            raise GovernedDomainUnavailable(
                "Personal data retention is unavailable."
            ) from error

    def upsert_policy(
        self,
        identity: PersonalDomainIdentity,
        domain: DomainKey,
        request: UpsertRetentionPolicyRequest,
    ) -> RetentionPolicy:
        payload = {"domain": domain.value, **request.model_dump(mode="json", by_alias=True)}
        proof = self.fingerprints.command(
            tenant_id=identity.tenant_id,
            session_id=identity.auth_session_id,
            purpose="retention-policy-command",
            payload=payload,
        )
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(
                    connection,
                    "retention-policy",
                    identity.tenant_id,
                    identity.user_id,
                    request.command_id,
                )
                replay = connection.execute(
                    """SELECT domain_key, session_fingerprint, request_fingerprint,
                              current_value
                         FROM ai_domain_retention_events
                        WHERE tenant_id = %s AND actor_user_id = %s AND command_id = %s""",
                    (identity.tenant_id, identity.user_id, request.command_id),
                ).fetchone()
                if replay is not None:
                    require_command_replay(
                        replay["session_fingerprint"], replay["request_fingerprint"], proof
                    )
                    if replay["domain_key"] != domain.value:
                        raise GovernedDomainConflict(
                            "The command ID is already bound to another retention domain."
                        )
                    return RetentionPolicy.model_validate(replay["current_value"])
                advisory_lock(connection, "retention-policy-target", identity.tenant_id, domain.value)
                current = connection.execute(
                    """SELECT domain_key, retention_days, deletion_grace_days,
                              legal_hold, revision, updated_at
                         FROM ai_domain_retention_policies
                        WHERE tenant_id = %s AND domain_key = %s FOR UPDATE""",
                    (identity.tenant_id, domain.value),
                ).fetchone()
                current_revision = int(current["revision"]) if current else 0
                if current_revision != request.expected_revision:
                    raise GovernedDomainConflict("The retention policy revision has changed.")
                revision = current_revision + 1
                row = connection.execute(
                    """INSERT INTO ai_domain_retention_policies (
                           tenant_id, domain_key, retention_days, deletion_grace_days,
                           legal_hold, revision, updated_by_user_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (tenant_id, domain_key) DO UPDATE SET
                           retention_days = EXCLUDED.retention_days,
                           deletion_grace_days = EXCLUDED.deletion_grace_days,
                           legal_hold = EXCLUDED.legal_hold,
                           revision = EXCLUDED.revision,
                           updated_by_user_id = EXCLUDED.updated_by_user_id,
                           updated_at = CURRENT_TIMESTAMP
                       RETURNING domain_key, retention_days, deletion_grace_days,
                                 legal_hold, revision, updated_at""",
                    (
                        identity.tenant_id,
                        domain.value,
                        request.retention_days,
                        request.deletion_grace_days,
                        request.legal_hold,
                        revision,
                        identity.user_id,
                    ),
                ).fetchone()
                policy = _policy(row)
                event_id = uuid4()
                reason_envelope = self.codec.encrypt_json(
                    {"changeReason": request.change_reason},
                    tenant_id=identity.tenant_id,
                    resource_type="domain-retention-event",
                    resource_id=str(event_id),
                    field="change-reason",
                )
                connection.execute(
                    """INSERT INTO ai_domain_retention_events (
                           event_id, tenant_id, domain_key, actor_user_id, correlation_id,
                           command_id, session_fingerprint, request_fingerprint, event_type,
                           reason_code, change_reason_envelope, previous_value,
                           current_value, revision)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s)""",
                    (
                        event_id,
                        identity.tenant_id,
                        domain.value,
                        identity.user_id,
                        identity.correlation_id,
                        request.command_id,
                        proof.session_fingerprint,
                        proof.request_fingerprint,
                        "BOOTSTRAPPED" if current is None else "UPDATED",
                        request.reason_code,
                        reason_envelope,
                        Jsonb(_policy(current).model_dump(mode="json", by_alias=True))
                        if current
                        else None,
                        Jsonb(policy.model_dump(mode="json", by_alias=True)),
                        revision,
                    ),
                )
                return policy
        except (GovernedDomainConflict, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise GovernedDomainUnavailable(
                "Personal data retention is unavailable."
            ) from error

    def request_deletion(
        self,
        identity: PersonalDomainIdentity,
        request: RequestDeletionRequest,
    ) -> DeletionJob:
        if request.expected_revision != 0:
            raise GovernedDomainConflict("A deletion request must start at revision zero.")
        payload = request.model_dump(mode="json", by_alias=True)
        proof = self.fingerprints.command(
            tenant_id=identity.tenant_id,
            session_id=identity.auth_session_id,
            purpose="personal-data-deletion-command",
            payload=payload,
        )
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(
                    connection,
                    "personal-data-deletion",
                    identity.tenant_id,
                    identity.user_id,
                    request.command_id,
                )
                replay = connection.execute(
                    """SELECT deletion_job_id, session_fingerprint, request_fingerprint,
                              result_envelope
                         FROM ai_data_deletion_jobs
                        WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
                    (identity.tenant_id, identity.user_id, request.command_id),
                ).fetchone()
                if replay is not None:
                    require_command_replay(
                        replay["session_fingerprint"], replay["request_fingerprint"], proof
                    )
                    return read_deletion_job(
                        connection,
                        deletion_job_id=replay["deletion_job_id"],
                        tenant_id=identity.tenant_id,
                        user_id=identity.user_id,
                        fingerprints=self.fingerprints,
                    )
                policy_rows = connection.execute(
                    """SELECT domain_key, retention_days, legal_hold
                         FROM ai_domain_retention_policies
                        WHERE tenant_id = %s AND domain_key = ANY(%s)""",
                    (identity.tenant_id, [domain.value for domain in request.domains]),
                ).fetchall()
                policies = {row["domain_key"]: row for row in policy_rows}
                missing = [domain.value for domain in request.domains if domain.value not in policies]
                if missing:
                    raise GovernedDomainConflict(
                        "Explicit retention policy is required before deletion can be requested."
                    )
                now = connection.execute(
                    "SELECT CURRENT_TIMESTAMP AS now"
                ).fetchone()["now"]
                blocked = [domain for domain in request.domains if policies[domain.value]["legal_hold"]]
                state = (
                    DeletionJobState.BLOCKED_LEGAL_HOLD
                    if len(blocked) == len(request.domains)
                    else DeletionJobState.REQUESTED
                )
                job_id = uuid4()
                result = DeletionJob(
                    deletion_job_id=job_id,
                    state=state,
                    domains=request.domains,
                    requested_at=now,
                    completed_at=(
                        now if len(blocked) == len(request.domains) else None
                    ),
                    deletion_performed=False,
                    blocked_domains=blocked,
                )
                reason_envelope = self.codec.encrypt_json(
                    {"changeReason": request.change_reason},
                    tenant_id=identity.tenant_id,
                    resource_type="data-deletion-job",
                    resource_id=str(job_id),
                    field="change-reason",
                )
                result_envelope = self.codec.encrypt_json(
                    result.model_dump(mode="json", by_alias=True),
                    tenant_id=identity.tenant_id,
                    resource_type="data-deletion-job",
                    resource_id=str(job_id),
                    field="result",
                )
                deadline = retention_deadline(now, 365)
                connection.execute(
                    """INSERT INTO ai_data_deletion_jobs (
                           deletion_job_id, tenant_id, user_id, command_id,
                           session_fingerprint, request_fingerprint, state, reason_code,
                           change_reason_envelope, result_envelope, requested_at,
                           completed_at, retention_until)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        job_id,
                        identity.tenant_id,
                        identity.user_id,
                        request.command_id,
                        proof.session_fingerprint,
                        proof.request_fingerprint,
                        state.value,
                        request.reason_code,
                        reason_envelope,
                        result_envelope,
                        now,
                        result.completed_at,
                        deadline,
                    ),
                )
                for domain in request.domains:
                    target_state = "BLOCKED_LEGAL_HOLD" if domain in blocked else "REQUESTED"
                    connection.execute(
                        """INSERT INTO ai_data_deletion_targets (
                               deletion_job_id, domain_key, state)
                           VALUES (%s, %s, %s)""",
                        (job_id, domain.value, target_state),
                    )
                connection.execute(
                    """INSERT INTO ai_data_deletion_events (
                           event_id, deletion_job_id, tenant_id, user_id, actor_user_id,
                           correlation_id, event_type, current_state, generation)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)""",
                    (
                        uuid4(),
                        job_id,
                        identity.tenant_id,
                        identity.user_id,
                        identity.user_id,
                        identity.correlation_id,
                        "BLOCKED" if blocked else "REQUESTED",
                        state.value,
                    ),
                )
                if len(blocked) < len(request.domains):
                    enqueue_internal_intent(
                        connection,
                        codec=self.codec,
                        fingerprints=self.fingerprints,
                        tenant_id=identity.tenant_id,
                        user_id=identity.user_id,
                        topic="ai.personal-data.deletion-requested.v1",
                        aggregate_type="DATA_DELETION_JOB",
                        aggregate_id=str(job_id),
                        payload={
                            "deletionJobId": str(job_id),
                            "domains": [domain.value for domain in request.domains],
                            "activeStoreDispositionRequested": True,
                            "externalWritePerformed": False,
                            "sourceSystemDataAffected": False,
                            "backupDispositionState": "EXTERNAL_RETENTION_BOUNDARY",
                        },
                        retention_until=deadline,
                    )
                return read_deletion_job(
                    connection,
                    deletion_job_id=job_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    fingerprints=self.fingerprints,
                )
        except (GovernedDomainConflict, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise GovernedDomainUnavailable(
                "Personal data deletion requests are unavailable."
            ) from error

    def deletion_job(
        self, identity: PersonalDomainIdentity, deletion_job_id: UUID
    ) -> DeletionJob:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                return read_deletion_job(
                    connection,
                    deletion_job_id=deletion_job_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    fingerprints=self.fingerprints,
                )
        except GovernedDomainNotFound:
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise GovernedDomainUnavailable(
                "Personal data deletion requests are unavailable."
            ) from error


def _policy(row: Any) -> RetentionPolicy:
    return RetentionPolicy(
        domain=row["domain_key"],
        retention_days=row["retention_days"],
        deletion_grace_days=row["deletion_grace_days"],
        legal_hold=row["legal_hold"],
        revision=row["revision"],
        updated_at=row["updated_at"],
    )


@lru_cache(maxsize=1)
def get_domain_retention_store() -> PostgresDomainRetentionStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise GovernedDomainUnavailable("Personal data governance database is unavailable.")
    return PostgresDomainRetentionStore(database_url)
