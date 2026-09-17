from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
)
from .governed_domain_core import (
    GovernedFingerprints,
    GovernedPayloadCodec,
    advisory_lock,
    canonical_json_bytes,
)
from .personal_domain_security import PersonalDomainIdentity
from .proposal_handoff_draft_contracts import (
    ProposalHandoffDraft,
    SaveProposalHandoffDraftRequest,
)


class ProposalHandoffDraftStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise DwaionWorkflowUnavailable(
                "Proposal handoff draft encryption is unavailable."
            ) from error

    def current(
        self, identity: PersonalDomainIdentity, handoff_id: UUID
    ) -> ProposalHandoffDraft | None:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                handoff = connection.execute(
                    """SELECT handoff_id FROM ai_proposal_handoffs
                        WHERE handoff_id = %s AND tenant_id = %s AND user_id = %s""",
                    (handoff_id, identity.tenant_id, identity.user_id),
                ).fetchone()
                if handoff is None:
                    raise DwaionWorkflowNotFound("The proposal handoff is unavailable.")
                row = connection.execute(
                    _SELECT
                    + " WHERE d.handoff_id = %s AND d.tenant_id = %s AND d.user_id = %s "
                    "ORDER BY d.draft_revision DESC LIMIT 1",
                    (handoff_id, identity.tenant_id, identity.user_id),
                ).fetchone()
                return self._record(row) if row is not None else None
        except DwaionWorkflowNotFound:
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable(
                "Proposal handoff draft storage is unavailable."
            ) from error

    def save(
        self,
        identity: PersonalDomainIdentity,
        handoff_id: UUID,
        request: SaveProposalHandoffDraftRequest,
    ) -> ProposalHandoffDraft:
        request_payload = request.model_dump(mode="json", by_alias=True)
        request_fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="proposal-handoff-draft-save",
            payload={"handoffId": str(handoff_id), "request": request_payload},
        )
        content_sha256 = hashlib.sha256(
            canonical_json_bytes(request.reviewed_inputs)
        ).hexdigest()
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                advisory_lock(
                    connection,
                    "proposal-handoff-draft",
                    identity.tenant_id,
                    identity.user_id,
                    handoff_id,
                )
                handoff = connection.execute(
                    """SELECT handoff_id, proposal_id, handoff_state, revision
                         FROM ai_proposal_handoffs
                        WHERE handoff_id = %s AND tenant_id = %s AND user_id = %s
                        FOR UPDATE""",
                    (handoff_id, identity.tenant_id, identity.user_id),
                ).fetchone()
                if handoff is None:
                    raise DwaionWorkflowNotFound("The proposal handoff is unavailable.")

                replay = connection.execute(
                    _SELECT
                    + " WHERE d.tenant_id = %s AND d.user_id = %s AND d.command_id = %s",
                    (identity.tenant_id, identity.user_id, request.command_id),
                ).fetchone()
                if replay is not None:
                    if not self.fingerprints.matches(
                        replay["request_fingerprint"], request_fingerprint
                    ):
                        raise DwaionWorkflowConflict(
                            "The proposal draft command ID is already bound to another request."
                        )
                    return self._record(replay)

                if int(handoff["revision"]) != request.expected_version:
                    raise DwaionWorkflowConflict(
                        "The proposal handoff version has changed."
                    )
                if handoff["handoff_state"] not in (
                    "REVIEW_REQUIRED",
                    "AWAITING_APPROVAL",
                ):
                    raise DwaionWorkflowConflict(
                        "The proposal handoff can no longer accept draft changes."
                    )

                next_revision = int(
                    connection.execute(
                        """SELECT COALESCE(MAX(draft_revision), 0) + 1 AS next_revision
                             FROM ai_proposal_handoff_draft_versions
                            WHERE handoff_id = %s AND tenant_id = %s AND user_id = %s""",
                        (handoff_id, identity.tenant_id, identity.user_id),
                    ).fetchone()["next_revision"]
                )
                draft_id = uuid4()
                envelope = self.codec.encrypt_json(
                    request.reviewed_inputs,
                    tenant_id=identity.tenant_id,
                    resource_type="proposal-handoff-draft",
                    resource_id=str(draft_id),
                    field="reviewed-inputs",
                )
                row = connection.execute(
                    """INSERT INTO ai_proposal_handoff_draft_versions (
                           draft_id, handoff_id, proposal_id, tenant_id, user_id,
                           actor_user_id, correlation_id, command_id, handoff_version,
                           draft_revision, reviewed_inputs_envelope, content_sha256,
                           request_fingerprint)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *""",
                    (
                        draft_id,
                        handoff_id,
                        handoff["proposal_id"],
                        identity.tenant_id,
                        identity.user_id,
                        identity.user_id,
                        identity.correlation_id,
                        request.command_id,
                        request.expected_version,
                        next_revision,
                        envelope,
                        content_sha256,
                        request_fingerprint,
                    ),
                ).fetchone()
                return self._record(row)
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable(
                "Proposal handoff draft storage is unavailable."
            ) from error

    def _record(self, row: Any) -> ProposalHandoffDraft:
        inputs = self.codec.decrypt_json(
            row["reviewed_inputs_envelope"],
            tenant_id=int(row["tenant_id"]),
            resource_type="proposal-handoff-draft",
            resource_id=str(row["draft_id"]),
            field="reviewed-inputs",
        )
        return ProposalHandoffDraft(
            draft_id=row["draft_id"],
            handoff_id=row["handoff_id"],
            proposal_id=row["proposal_id"],
            handoff_version=row["handoff_version"],
            revision=row["draft_revision"],
            reviewed_inputs=inputs,
            content_sha256=row["content_sha256"],
            saved_at=row["saved_at"],
        )


@lru_cache(maxsize=1)
def get_proposal_handoff_draft_store() -> ProposalHandoffDraftStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise DwaionWorkflowUnavailable(
            "Proposal handoff draft storage is unavailable."
        )
    return ProposalHandoffDraftStore(database_url)


_SELECT = "SELECT d.* FROM ai_proposal_handoff_draft_versions d"
