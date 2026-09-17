from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .canonical_json import canonical_json_bytes
from .deletion_job_queries import read_deletion_job
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
)
from .personal_data_evidence_contracts import (
    PersonalDataEvidenceAction,
    PersonalDataEvidenceCommandState,
)
from .personal_domain_security import PersonalDomainIdentity


@dataclass(frozen=True)
class PersonalDataEvidenceDownload:
    content: bytes
    filename: str
    media_type: str
    fingerprint: str
    receipt_id: UUID


class PersonalDataEvidenceDownloadStoreMixin:
    database_url: str
    codec: Any
    fingerprints: Any

    def legal_hold_snapshot(
        self,
        identity: PersonalDomainIdentity,
        deletion_job_id: UUID,
    ) -> PersonalDataEvidenceDownload:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                job = read_deletion_job(
                    connection,
                    deletion_job_id=deletion_job_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    fingerprints=self.fingerprints,
                )
                payload = {
                    "schema": "dwp.personal-data.legal-hold-evidence.v1",
                    "tenantId": identity.tenant_id,
                    "userId": identity.user_id,
                    "deletionJobId": str(job.deletion_job_id),
                    "jobState": job.state.value,
                    "blockedDomains": [domain.value for domain in job.blocked_domains],
                    "legalHolds": [
                        hold.model_dump(mode="json", by_alias=True)
                        for hold in job.legal_holds
                    ],
                }
                return self._record_json_download(
                    connection,
                    identity,
                    deletion_job_id=deletion_job_id,
                    download_type="LEGAL_HOLD_SNAPSHOT",
                    filename=f"legal-hold-{deletion_job_id}.json",
                    payload=payload,
                )
        except GovernedDomainNotFound:
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise GovernedDomainUnavailable(
                "Legal-hold evidence is unavailable."
            ) from error

    def receipt_index(
        self,
        identity: PersonalDomainIdentity,
    ) -> PersonalDataEvidenceDownload:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    """SELECT deletion_job_id FROM ai_data_deletion_jobs
                        WHERE tenant_id = %s AND user_id = %s
                        ORDER BY requested_at DESC LIMIT 100""",
                    (identity.tenant_id, identity.user_id),
                ).fetchall()
                jobs = [
                    read_deletion_job(
                        connection,
                        deletion_job_id=row["deletion_job_id"],
                        tenant_id=identity.tenant_id,
                        user_id=identity.user_id,
                        fingerprints=self.fingerprints,
                    )
                    for row in rows
                ]
                payload = {
                    "schema": "dwp.personal-data.deletion-receipt-index.v1",
                    "tenantId": identity.tenant_id,
                    "userId": identity.user_id,
                    "receipts": [
                        job.model_dump(mode="json", by_alias=True) for job in jobs
                    ],
                }
                return self._record_json_download(
                    connection,
                    identity,
                    deletion_job_id=None,
                    download_type="RECEIPT_INDEX",
                    filename="personal-data-deletion-receipts.json",
                    payload=payload,
                )
        except (PsycopgError, ValueError, TypeError) as error:
            raise GovernedDomainUnavailable(
                "The personal-data receipt index is unavailable."
            ) from error

    def certificate(
        self,
        identity: PersonalDomainIdentity,
        deletion_job_id: UUID,
        command_id: UUID,
    ) -> PersonalDataEvidenceDownload:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = self._command_row(
                    connection,
                    identity,
                    deletion_job_id=deletion_job_id,
                    command_id=command_id,
                )
                if row is None:
                    raise GovernedDomainNotFound(
                        "The signed deletion certificate is unavailable."
                    )
                if (
                    row["action"] != PersonalDataEvidenceAction.SIGNED_CERTIFICATE.value
                    or row["state"]
                    != PersonalDataEvidenceCommandState.COMPLETED.value
                    or not row["result_envelope"]
                ):
                    raise GovernedDomainConflict(
                        "The signed deletion certificate is not ready."
                    )
                private = self.codec.decrypt_json(
                    row["result_envelope"],
                    tenant_id=identity.tenant_id,
                    resource_type="personal-data-evidence-command",
                    resource_id=str(command_id),
                    field="result",
                )
                encoded = private.get("_certificatePdfBase64")
                expected = private.get("documentSha256")
                if not isinstance(encoded, str) or not isinstance(expected, str):
                    raise GovernedDomainUnavailable(
                        "The signed deletion certificate evidence is invalid."
                    )
                content = base64.b64decode(encoded, validate=True)
                if hashlib.sha256(content).hexdigest() != expected:
                    raise GovernedDomainUnavailable(
                        "The signed deletion certificate failed integrity validation."
                    )
                return self._record_download(
                    connection,
                    identity,
                    deletion_job_id=deletion_job_id,
                    command_id=command_id,
                    download_type="SIGNED_CERTIFICATE",
                    filename=f"deletion-certificate-{deletion_job_id}.pdf",
                    media_type="application/pdf",
                    content=content,
                )
        except (
            GovernedDomainNotFound,
            GovernedDomainConflict,
            GovernedDomainUnavailable,
        ):
            raise
        except (PsycopgError, ValueError, TypeError) as error:
            raise GovernedDomainUnavailable(
                "The signed deletion certificate is unavailable."
            ) from error

    def _record_json_download(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        *,
        deletion_job_id: UUID | None,
        download_type: str,
        filename: str,
        payload: Mapping[str, object],
    ) -> PersonalDataEvidenceDownload:
        content = canonical_json_bytes(payload) + b"\n"
        return self._record_download(
            connection,
            identity,
            deletion_job_id=deletion_job_id,
            command_id=None,
            download_type=download_type,
            filename=filename,
            media_type="application/json",
            content=content,
        )

    def _record_download(
        self,
        connection: Any,
        identity: PersonalDomainIdentity,
        *,
        deletion_job_id: UUID | None,
        command_id: UUID | None,
        download_type: str,
        filename: str,
        media_type: str,
        content: bytes,
    ) -> PersonalDataEvidenceDownload:
        receipt_id = uuid4()
        fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="personal-data-evidence-download",
            payload={
                "receiptId": str(receipt_id),
                "downloadType": download_type,
                "deletionJobId": str(deletion_job_id) if deletion_job_id else None,
                "commandId": str(command_id) if command_id else None,
                "contentSha256": hashlib.sha256(content).hexdigest(),
            },
        )
        connection.execute(
            """INSERT INTO ai_personal_data_evidence_download_events (
                   receipt_id, tenant_id, user_id, actor_user_id, correlation_id,
                   deletion_job_id, command_id, download_type, filename,
                   byte_count, evidence_fingerprint)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                receipt_id,
                identity.tenant_id,
                identity.user_id,
                identity.user_id,
                identity.correlation_id,
                deletion_job_id,
                command_id,
                download_type,
                filename,
                len(content),
                fingerprint,
            ),
        )
        return PersonalDataEvidenceDownload(
            content=content,
            filename=filename,
            media_type=media_type,
            fingerprint=fingerprint,
            receipt_id=receipt_id,
        )
