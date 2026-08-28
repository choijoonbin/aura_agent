from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError
from psycopg import connect

from .proposal_analysis_contracts import ProposalAnalysisPreference
from .proposal_analysis_fingerprints import ProposalAnalysisFingerprints


class ProposalAnalysisControlError(RuntimeError):
    pass


class ProposalAnalysisControlUnavailable(ProposalAnalysisControlError):
    pass


class ProposalAnalysisDisabled(ProposalAnalysisControlError):
    pass


class ProposalAnalysisPreferenceConflict(ProposalAnalysisControlError):
    pass


class ProposalAnalysisControl(Protocol):
    def preference(
        self, *, tenant_id: str, user_id: str
    ) -> ProposalAnalysisPreference: ...

    def update_preference(
        self,
        *,
        tenant_id: str,
        user_id: str,
        actor_user_id: str,
        correlation_id: str,
        command_id: UUID,
        expected_revision: int,
        enabled: bool,
    ) -> ProposalAnalysisPreference: ...


@dataclass
class _MemoryPreference:
    value: ProposalAnalysisPreference


class InMemoryProposalAnalysisControl:
    def __init__(
        self, fingerprints: ProposalAnalysisFingerprints | None = None
    ) -> None:
        self._lock = threading.Lock()
        self._fingerprints = fingerprints or ProposalAnalysisFingerprints.ephemeral()
        self._preferences: dict[tuple[str, str], _MemoryPreference] = {}
        self._preference_commands: dict[tuple[str, str, UUID], str] = {}

    def preference(
        self, *, tenant_id: str, user_id: str
    ) -> ProposalAnalysisPreference:
        with self._lock:
            record = self._preferences.get((tenant_id, user_id))
            return record.value if record else _default_preference()

    def update_preference(
        self,
        *,
        tenant_id: str,
        user_id: str,
        actor_user_id: str,
        correlation_id: str,
        command_id: UUID,
        expected_revision: int,
        enabled: bool,
    ) -> ProposalAnalysisPreference:
        del actor_user_id, correlation_id
        fingerprint = self._fingerprints.preference(
            tenant_id, enabled=enabled, expected_revision=expected_revision
        )
        key = (tenant_id, user_id, command_id)
        with self._lock:
            previous = self._preference_commands.get(key)
            current = self._preferences.get((tenant_id, user_id))
            value = current.value if current else _default_preference()
            if previous is not None:
                if previous != fingerprint:
                    raise ProposalAnalysisPreferenceConflict(
                        "The preference command payload changed."
                    )
                return value
            if value.revision != expected_revision:
                raise ProposalAnalysisPreferenceConflict(
                    "The preference changed. Reload and retry."
                )
            updated = ProposalAnalysisPreference(
                proactive_analysis_enabled=enabled,
                revision=value.revision + 1,
                updated_at=datetime.now(timezone.utc),
            )
            self._preferences[(tenant_id, user_id)] = _MemoryPreference(updated)
            self._preference_commands[key] = fingerprint
            return updated


class PostgresProposalAnalysisControl:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.fingerprints = ProposalAnalysisFingerprints.load()

    def preference(
        self, *, tenant_id: str, user_id: str
    ) -> ProposalAnalysisPreference:
        try:
            with connect(self.database_url) as connection:
                row = connection.execute(
                    """SELECT proactive_analysis_enabled, revision, updated_at
                         FROM ai_agent_proposal_preferences
                        WHERE tenant_id = %s AND user_id = %s""",
                    (_tenant(tenant_id), user_id),
                ).fetchone()
            return (
                ProposalAnalysisPreference(
                    proactive_analysis_enabled=row[0],
                    revision=row[1],
                    updated_at=row[2],
                )
                if row
                else _default_preference()
            )
        except (PsycopgError, ValueError) as error:
            raise ProposalAnalysisControlUnavailable(
                "Proposal analysis controls are unavailable."
            ) from error

    def update_preference(
        self,
        *,
        tenant_id: str,
        user_id: str,
        actor_user_id: str,
        correlation_id: str,
        command_id: UUID,
        expected_revision: int,
        enabled: bool,
    ) -> ProposalAnalysisPreference:
        tenant = _tenant(tenant_id)
        fingerprint = self.fingerprints.preference(
            tenant, enabled=enabled, expected_revision=expected_revision
        )
        try:
            with connect(self.database_url) as connection:
                _lock_user(connection, tenant, user_id)
                replay = connection.execute(
                    """SELECT request_fingerprint
                         FROM ai_agent_proposal_preference_events
                        WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
                    (tenant, user_id, command_id),
                ).fetchone()
                current = _preference_row(connection, tenant, user_id)
                if replay is not None:
                    if not self.fingerprints.matches_preference(
                        replay[0],
                        tenant,
                        enabled=enabled,
                        expected_revision=expected_revision,
                    ):
                        raise ProposalAnalysisPreferenceConflict(
                            "The preference command payload changed."
                        )
                    return current
                if current.revision != expected_revision:
                    raise ProposalAnalysisPreferenceConflict(
                        "The preference changed. Reload and retry."
                    )
                next_revision = current.revision + 1
                row = connection.execute(
                    """INSERT INTO ai_agent_proposal_preferences (
                           tenant_id, user_id, proactive_analysis_enabled, revision,
                           updated_by_user_id)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (tenant_id, user_id) DO UPDATE
                           SET proactive_analysis_enabled = EXCLUDED.proactive_analysis_enabled,
                               revision = EXCLUDED.revision,
                               updated_by_user_id = EXCLUDED.updated_by_user_id,
                               updated_at = CURRENT_TIMESTAMP
                       RETURNING proactive_analysis_enabled, revision, updated_at""",
                    (tenant, user_id, enabled, next_revision, actor_user_id),
                ).fetchone()
                connection.execute(
                    """INSERT INTO ai_agent_proposal_preference_events (
                           event_id, tenant_id, user_id, actor_user_id, correlation_id,
                           command_id, previous_enabled, current_enabled, revision,
                           request_fingerprint)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        uuid4(), tenant, user_id, actor_user_id, correlation_id,
                        command_id, current.proactive_analysis_enabled, enabled,
                        next_revision, fingerprint,
                    ),
                )
                return ProposalAnalysisPreference(
                    proactive_analysis_enabled=row[0],
                    revision=row[1],
                    updated_at=row[2],
                )
        except ProposalAnalysisPreferenceConflict:
            raise
        except (PsycopgError, ValueError) as error:
            raise ProposalAnalysisControlUnavailable(
                "Proposal analysis controls are unavailable."
            ) from error


def _preference_row(connection, tenant: int, user_id: str) -> ProposalAnalysisPreference:
    row = connection.execute(
        """SELECT proactive_analysis_enabled, revision, updated_at
             FROM ai_agent_proposal_preferences
            WHERE tenant_id = %s AND user_id = %s""",
        (tenant, user_id),
    ).fetchone()
    if row is None:
        return _default_preference()
    return ProposalAnalysisPreference(
        proactive_analysis_enabled=row[0], revision=row[1], updated_at=row[2]
    )


def _default_preference() -> ProposalAnalysisPreference:
    return ProposalAnalysisPreference(
        proactive_analysis_enabled=True, revision=0, updated_at=None
    )


def _tenant(value: str) -> int:
    tenant = int(value)
    if tenant <= 0:
        raise ValueError("Tenant must be positive.")
    return tenant


def _lock_user(connection, tenant: int, user_id: str) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"dwp-agent:proposal-analysis:{tenant}:{user_id}",),
    )


_CONTROL: ProposalAnalysisControl | None = None
_CONTROL_LOCK = threading.Lock()


def get_proposal_analysis_control() -> ProposalAnalysisControl:
    global _CONTROL
    with _CONTROL_LOCK:
        if _CONTROL is not None:
            return _CONTROL
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            raise ProposalAnalysisControlUnavailable(
                "Proposal analysis controls require the configured Agent database."
            )
        _CONTROL = PostgresProposalAnalysisControl(database_url)
        return _CONTROL


def set_proposal_analysis_control_for_tests(
    control: ProposalAnalysisControl | None,
) -> None:
    global _CONTROL
    with _CONTROL_LOCK:
        _CONTROL = control
