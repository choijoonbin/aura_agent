from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from .governed_domain_contracts import DomainKey


class DeletionTargetRejected(RuntimeError):
    def __init__(self, safe_error_code: str) -> None:
        super().__init__(safe_error_code)
        self.safe_error_code = safe_error_code


class DispositionLease(Protocol):
    tenant_id: int
    user_id: str


def purge_domain(
    connection: Any, lease: DispositionLease, domain: DomainKey
) -> dict[str, int]:
    if domain == DomainKey.MEMORY:
        return _delete_statements(
            connection,
            lease,
            (
                ("ai_user_memory_events", "DELETE FROM ai_user_memory_events WHERE tenant_id = %s AND user_id = %s"),
                ("ai_user_memory_commands", "DELETE FROM ai_user_memory_commands WHERE tenant_id = %s AND user_id = %s"),
                ("ai_user_memories", "DELETE FROM ai_user_memories WHERE tenant_id = %s AND user_id = %s"),
                ("ai_user_memory_preferences", "DELETE FROM ai_user_memory_preferences WHERE tenant_id = %s AND user_id = %s"),
                ("ai_user_ai_source_preferences", "DELETE FROM ai_user_ai_source_preferences WHERE tenant_id = %s AND user_id = %s"),
            ),
        )
    if domain == DomainKey.ROUTINE:
        return _delete_statements(
            connection,
            lease,
            (
                ("ai_personal_routine_events", "DELETE FROM ai_personal_routine_events WHERE tenant_id = %s AND user_id = %s"),
                ("ai_personal_routine_consents", "DELETE FROM ai_personal_routine_consents WHERE tenant_id = %s AND user_id = %s"),
                ("ai_personal_routine_runs", "DELETE FROM ai_personal_routine_runs WHERE tenant_id = %s AND user_id = %s"),
                ("ai_personal_routine_commands", "DELETE FROM ai_personal_routine_commands WHERE tenant_id = %s AND user_id = %s"),
                ("ai_personal_routine_sources", "DELETE FROM ai_personal_routine_sources WHERE tenant_id = %s AND user_id = %s"),
                ("ai_personal_routines", "DELETE FROM ai_personal_routines WHERE tenant_id = %s AND user_id = %s"),
            ),
        )
    if domain == DomainKey.ARTIFACT_EXPORT:
        return _purge_artifact_exports(connection, lease)
    if domain == DomainKey.ARTIFACT:
        return _purge_artifacts(connection, lease)
    raise DeletionTargetRejected("DELETION_DOMAIN_UNSUPPORTED")


def _purge_artifact_exports(
    connection: Any, lease: DispositionLease
) -> dict[str, int]:
    job_rows = connection.execute(
        """SELECT export_job_id, command_id FROM ai_artifact_export_jobs
            WHERE tenant_id = %s AND user_id = %s""",
        (lease.tenant_id, lease.user_id),
    ).fetchall()
    job_ids = [row["export_job_id"] for row in job_rows]
    command_ids = [row["command_id"] for row in job_rows]
    if not job_ids:
        return {}
    counts: dict[str, int] = {}
    statements = (
        ("ai_artifact_export_outputs", "DELETE FROM ai_artifact_export_outputs WHERE export_job_id = ANY(%s)", (job_ids,)),
        ("ai_artifact_export_events", "DELETE FROM ai_artifact_export_events WHERE export_job_id = ANY(%s)", (job_ids,)),
        ("ai_transactional_outbox_events", "DELETE FROM ai_transactional_outbox_events WHERE outbox_id IN (SELECT outbox_id FROM ai_transactional_outbox WHERE tenant_id = %s AND aggregate_type = 'ARTIFACT_EXPORT' AND aggregate_id = ANY(%s))", (lease.tenant_id, [str(item) for item in job_ids])),
        ("ai_transactional_outbox", "DELETE FROM ai_transactional_outbox WHERE tenant_id = %s AND aggregate_type = 'ARTIFACT_EXPORT' AND aggregate_id = ANY(%s)", (lease.tenant_id, [str(item) for item in job_ids])),
        ("ai_artifact_events", "DELETE FROM ai_artifact_events WHERE tenant_id = %s AND user_id = %s AND command_id = ANY(%s)", (lease.tenant_id, lease.user_id, command_ids)),
        ("ai_artifact_commands", "DELETE FROM ai_artifact_commands WHERE tenant_id = %s AND user_id = %s AND command_id = ANY(%s)", (lease.tenant_id, lease.user_id, command_ids)),
        ("ai_artifact_export_jobs", "DELETE FROM ai_artifact_export_jobs WHERE export_job_id = ANY(%s)", (job_ids,)),
    )
    for name, sql, parameters in statements:
        counts[name] = connection.execute(sql, parameters).rowcount
    return counts


def _purge_artifacts(
    connection: Any, lease: DispositionLease
) -> dict[str, int]:
    remaining_exports = connection.execute(
        """SELECT COUNT(*) AS total FROM ai_artifact_export_jobs
            WHERE tenant_id = %s AND user_id = %s""",
        (lease.tenant_id, lease.user_id),
    ).fetchone()["total"]
    if remaining_exports:
        raise DeletionTargetRejected("ARTIFACT_EXPORT_DEPENDENCY")
    artifact_rows = connection.execute(
        """SELECT artifact_id FROM ai_artifacts
            WHERE tenant_id = %s AND user_id = %s""",
        (lease.tenant_id, lease.user_id),
    ).fetchall()
    artifact_ids: list[UUID] = [row["artifact_id"] for row in artifact_rows]
    if not artifact_ids:
        return {}
    return _delete_statements(
        connection,
        lease,
        (
            ("ai_artifact_events", "DELETE FROM ai_artifact_events WHERE tenant_id = %s AND user_id = %s"),
            ("ai_artifact_commands", "DELETE FROM ai_artifact_commands WHERE tenant_id = %s AND user_id = %s"),
            ("ai_artifact_preflight_runs", "DELETE FROM ai_artifact_preflight_runs WHERE tenant_id = %s AND user_id = %s"),
            ("ai_artifact_version_sources", "DELETE FROM ai_artifact_version_sources WHERE artifact_id = ANY(%s)", (artifact_ids,)),
            ("ai_artifact_versions", "DELETE FROM ai_artifact_versions WHERE tenant_id = %s AND user_id = %s"),
            ("ai_artifact_draft_sources", "DELETE FROM ai_artifact_draft_sources WHERE artifact_id = ANY(%s)", (artifact_ids,)),
            ("ai_artifact_drafts", "DELETE FROM ai_artifact_drafts WHERE tenant_id = %s AND user_id = %s"),
            ("ai_artifacts", "DELETE FROM ai_artifacts WHERE tenant_id = %s AND user_id = %s"),
        ),
    )


def _delete_statements(
    connection: Any,
    lease: DispositionLease,
    statements: tuple[tuple[Any, ...], ...],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for statement in statements:
        name, sql, *custom = statement
        parameters = custom[0] if custom else (lease.tenant_id, lease.user_id)
        counts[name] = connection.execute(sql, parameters).rowcount
    return counts
