from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .dwaion_workflow_contracts import ResearchResult
from .dwaion_workflow_errors import (
    DwaionWorkflowConflict,
    DwaionWorkflowNotFound,
    DwaionWorkflowUnavailable,
)
from .governed_domain_core import GovernedFingerprints, GovernedPayloadCodec
from .personal_domain_security import PersonalDomainIdentity
from .research_download_contracts import (
    ResearchAuditDownloadEvent,
    ResearchRawDownload,
    ResearchReceiptDownload,
)


class ResearchDownloadStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise DwaionWorkflowUnavailable("Research download security is unavailable.") from error

    def raw(self, identity: PersonalDomainIdentity, run_id: UUID) -> ResearchRawDownload:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = self._completed(connection, identity, run_id)
                result = self._result(row)
                payload = {
                    "schemaVersion": 1,
                    "runId": str(row["run_id"]),
                    "planId": str(row["plan_id"]),
                    "planRevision": row["plan_revision"],
                    "result": result.model_dump(mode="json", by_alias=True),
                    "completedAt": row["completed_at"].isoformat(),
                }
                fingerprint = _integrity(payload)
                self._download_event(connection, identity, row, "RAW", fingerprint)
                return ResearchRawDownload.model_validate(
                    {**payload, "integrityFingerprint": fingerprint}
                )
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research raw download is unavailable.") from error

    def receipt(
        self, identity: PersonalDomainIdentity, run_id: UUID
    ) -> ResearchReceiptDownload:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = self._completed(connection, identity, run_id)
                result = self._result(row)
                payload = {
                    "schemaVersion": 1,
                    "receiptId": str(row["receipt_id"]),
                    "runId": str(row["run_id"]),
                    "planId": str(row["plan_id"]),
                    "planRevision": row["plan_revision"],
                    "resultSha256": result.result_sha256,
                    "citationCount": len(result.citations),
                    "completedAt": row["completed_at"].isoformat(),
                }
                fingerprint = _integrity(payload)
                self._download_event(connection, identity, row, "RECEIPT", fingerprint)
                return ResearchReceiptDownload.model_validate(
                    {**payload, "integrityFingerprint": fingerprint}
                )
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research receipt download is unavailable.") from error

    def audit(
        self, identity: PersonalDomainIdentity, run_id: UUID
    ) -> list[ResearchAuditDownloadEvent]:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = self._completed(connection, identity, run_id)
                self._download_event(connection, identity, row, "AUDIT", None)
                events = connection.execute(
                    """SELECT event_id, event_type, actor_user_id, correlation_id,
                              command_id, previous_state, current_state, revision, occurred_at
                         FROM ai_research_run_events
                        WHERE run_id = %s AND tenant_id = %s AND user_id = %s
                     ORDER BY occurred_at, event_id""",
                    (run_id, identity.tenant_id, identity.user_id),
                ).fetchall()
                return [self._audit_event(event) for event in events]
        except (DwaionWorkflowConflict, DwaionWorkflowNotFound):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise DwaionWorkflowUnavailable("Research audit download is unavailable.") from error

    def _completed(
        self, connection: Any, identity: PersonalDomainIdentity, run_id: UUID
    ) -> Any:
        row = connection.execute(
            """SELECT * FROM ai_research_runs
                WHERE run_id = %s AND tenant_id = %s AND user_id = %s
                FOR UPDATE""",
            (run_id, identity.tenant_id, identity.user_id),
        ).fetchone()
        if row is None:
            raise DwaionWorkflowNotFound("The research run is unavailable.")
        if (
            row["run_state"] != "COMPLETED"
            or row["receipt_id"] is None
            or row["result_envelope"] is None
            or row["completed_at"] is None
        ):
            raise DwaionWorkflowConflict("Only a completed research run can be downloaded.")
        return row

    def _result(self, row: Any) -> ResearchResult:
        payload = self.codec.decrypt_json(
            row["result_envelope"],
            tenant_id=row["tenant_id"],
            resource_type="research-run",
            resource_id=str(row["run_id"]),
            field="result",
        )
        return ResearchResult.model_validate(payload)

    def _download_event(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        row: Any,
        download_type: str,
        artifact_fingerprint: str | None,
    ) -> None:
        command_id = uuid4()
        detail = {
            "downloadType": download_type,
            "artifactFingerprint": artifact_fingerprint,
        }
        request_fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="research-run:download",
            payload={"runId": str(row["run_id"]), **detail},
        )
        detail_envelope = self.codec.encrypt_json(
            detail,
            tenant_id=identity.tenant_id,
            resource_type="research-run",
            resource_id=str(row["run_id"]),
            field=f"event-{command_id}",
        )
        connection.execute(
            """INSERT INTO ai_research_run_events (
                   event_id, run_id, tenant_id, user_id, actor_user_id,
                   correlation_id, command_id, event_type, previous_state,
                   current_state, revision, request_fingerprint, detail_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(), row["run_id"], identity.tenant_id, identity.user_id,
                identity.user_id, identity.correlation_id, command_id,
                f"DOWNLOAD_{download_type}", row["run_state"], row["run_state"],
                row["revision"], request_fingerprint, detail_envelope,
            ),
        )

    @staticmethod
    def _audit_event(row: Any) -> ResearchAuditDownloadEvent:
        payload = {
            "eventId": str(row["event_id"]),
            "eventType": row["event_type"],
            "actorUserId": row["actor_user_id"],
            "correlationId": row["correlation_id"],
            "commandId": str(row["command_id"]),
            "previousState": row["previous_state"],
            "currentState": row["current_state"],
            "revision": row["revision"],
            "occurredAt": row["occurred_at"].isoformat(),
        }
        return ResearchAuditDownloadEvent.model_validate(
            {**payload, "integrityFingerprint": _integrity(payload)}
        )


def _integrity(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def get_research_download_store() -> ResearchDownloadStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise DwaionWorkflowUnavailable("Research download storage is unavailable.")
    return ResearchDownloadStore(database_url)
