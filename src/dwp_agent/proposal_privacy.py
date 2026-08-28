from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError
from psycopg import connect

from .envelope import EnvelopeEncryptionError
from .proposal_analysis_commands import ProposalAnalysisCommandStore
from .proposal_analysis_contracts import ClearProposalInboxReceipt
from .proposal_contracts import ProposalContent
from .run_store import load_payload_encryption
from .run_store_errors import RunStoreUnavailable


class ProposalPrivacyUnavailable(RuntimeError):
    pass


class ProposalPrivacyService(Protocol):
    def clear(
        self,
        *,
        tenant_id: str,
        user_id: str,
        correlation_id: str,
        command_id: UUID,
    ) -> ClearProposalInboxReceipt: ...


class InMemoryProposalPrivacyService:
    def __init__(
        self,
        proposal_store,
        analysis_commands: ProposalAnalysisCommandStore | None = None,
    ) -> None:
        self._proposal_store = proposal_store
        self._analysis_commands = analysis_commands
        self._lock = threading.Lock()
        self._commands: dict[
            tuple[str, str, UUID], ClearProposalInboxReceipt
        ] = {}

    def clear(
        self,
        *,
        tenant_id: str,
        user_id: str,
        correlation_id: str,
        command_id: UUID,
    ) -> ClearProposalInboxReceipt:
        del correlation_id
        key = (tenant_id, user_id, command_id)
        with self._lock:
            existing = self._commands.get(key)
            if existing is not None:
                return existing
            if self._analysis_commands is not None:
                self._analysis_commands.clear_for_user(
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
            hidden_count = self._proposal_store.clear_for_user(
                tenant_id=tenant_id, user_id=user_id
            )
            receipt = ClearProposalInboxReceipt(
                hidden_count=hidden_count,
                cleared_at=datetime.now(timezone.utc),
            )
            self._commands[key] = receipt
            return receipt


class PostgresProposalPrivacyService:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            self.encryption = load_payload_encryption()
        except RunStoreUnavailable as error:
            raise ProposalPrivacyUnavailable(
                "Proposal privacy controls are unavailable."
            ) from error

    def clear(
        self,
        *,
        tenant_id: str,
        user_id: str,
        correlation_id: str,
        command_id: UUID,
    ) -> ClearProposalInboxReceipt:
        tenant = _tenant(tenant_id)
        try:
            with connect(self.database_url) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"dwp-agent:proposal-analysis:{tenant}:{user_id}",),
                )
                replay = connection.execute(
                    """SELECT current_value, created_at
                         FROM ai_governance_events
                        WHERE tenant_id = %s AND category = 'RETENTION'
                          AND event_type = 'proposal-inbox.cleared'
                          AND target_type = 'USER_PROPOSAL_INBOX'
                          AND target_key = %s AND actor_user_id = %s""",
                    (tenant, str(command_id), user_id),
                ).fetchone()
                if replay is not None:
                    return ClearProposalInboxReceipt(
                        hidden_count=int(replay[0]["hiddenCount"]),
                        cleared_at=replay[1],
                    )
                connection.execute(
                    """UPDATE ai_agent_proposal_analysis_commands
                          SET status = 'FAILED', generation = generation + 1,
                              lease_token = NULL, completed_at = NULL,
                              result_envelope = NULL,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE tenant_id = %s AND user_id = %s""",
                    (tenant, user_id),
                )
                rows = connection.execute(
                    """SELECT proposal_id
                         FROM ai_agent_proposals
                        WHERE tenant_id = %s AND target_user_id = %s
                          AND hidden_at IS NULL
                        ORDER BY proposal_id
                        FOR UPDATE""",
                    (tenant, user_id),
                ).fetchall()
                cleared_at = connection.execute(
                    "SELECT CURRENT_TIMESTAMP"
                ).fetchone()[0]
                tombstone = ProposalContent(
                    title="Removed by the user",
                    summary="Business content was redacted under the user privacy command.",
                    rationale="Only non-content audit metadata remains for the tenant retention policy.",
                )
                for (proposal_id,) in rows:
                    payload = self.encryption.encrypt_bytes(
                        tombstone.model_dump_json(by_alias=True).encode("utf-8"),
                        _proposal_context(tenant, proposal_id),
                    )
                    connection.execute(
                        """UPDATE ai_agent_proposals
                              SET payload_envelope = %s, hidden_at = %s,
                                  updated_at = %s
                            WHERE proposal_id = %s AND tenant_id = %s
                              AND target_user_id = %s AND hidden_at IS NULL""",
                        (payload, cleared_at, cleared_at, proposal_id, tenant, user_id),
                    )
                connection.execute(
                    """UPDATE ai_agent_proposal_events event
                          SET note_envelope = NULL
                        WHERE event.tenant_id = %s AND event.target_user_id = %s
                          AND event.note_envelope IS NOT NULL
                          AND EXISTS (
                              SELECT 1 FROM ai_agent_proposals proposal
                               WHERE proposal.proposal_id = event.proposal_id
                                 AND proposal.hidden_at = %s)""",
                    (tenant, user_id, cleared_at),
                )
                connection.execute(
                    """INSERT INTO ai_governance_events (
                           event_id, tenant_id, category, event_type, target_type,
                           target_key, actor_user_id, correlation_id, change_reason,
                           previous_value, current_value)
                       VALUES (%s, %s, 'RETENTION', 'proposal-inbox.cleared',
                               'USER_PROPOSAL_INBOX', %s, %s, %s,
                               'User cleared proactive proposal business content.',
                               %s::jsonb, %s::jsonb)""",
                    (
                        uuid4(), tenant, str(command_id), user_id, correlation_id,
                        json.dumps({"visibleCount": len(rows)}),
                        json.dumps(
                            {
                                "hiddenCount": len(rows),
                                "contentState": "REDACTED",
                            }
                        ),
                    ),
                )
                return ClearProposalInboxReceipt(
                    hidden_count=len(rows), cleared_at=cleared_at
                )
        except (PsycopgError, EnvelopeEncryptionError, ValueError) as error:
            raise ProposalPrivacyUnavailable(
                "Proposal privacy controls are unavailable."
            ) from error


def _tenant(value: str) -> int:
    tenant = int(value)
    if tenant <= 0:
        raise ValueError("Tenant must be positive.")
    return tenant


def _proposal_context(tenant_id: int, proposal_id: UUID):
    from .envelope import KeyContext

    return KeyContext.payload(
        tenant_id=tenant_id,
        resource_type="agent-proposal",
        resource_id=str(proposal_id),
        field="content",
    )


_PRIVACY_SERVICE: ProposalPrivacyService | None = None
_PRIVACY_LOCK = threading.Lock()


def get_proposal_privacy_service() -> ProposalPrivacyService:
    global _PRIVACY_SERVICE
    with _PRIVACY_LOCK:
        if _PRIVACY_SERVICE is not None:
            return _PRIVACY_SERVICE
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            raise ProposalPrivacyUnavailable(
                "Proposal privacy controls require the configured Agent database."
            )
        _PRIVACY_SERVICE = PostgresProposalPrivacyService(database_url)
        return _PRIVACY_SERVICE


def set_proposal_privacy_service_for_tests(
    service: ProposalPrivacyService | None,
) -> None:
    global _PRIVACY_SERVICE
    with _PRIVACY_LOCK:
        _PRIVACY_SERVICE = service
