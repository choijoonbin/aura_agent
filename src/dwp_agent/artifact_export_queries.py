from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import connect
from psycopg.rows import dict_row

from .artifact_contracts import ArtifactExportReceipt
from .governed_domain_core import GovernedDomainConflict, GovernedDomainNotFound
from .governed_worker_runtime import governed_worker_available
from .personal_domain_security import PersonalDomainIdentity


@dataclass(frozen=True)
class ArtifactExportFile:
    content: bytes
    media_type: str
    file_name: str
    content_fingerprint: str


class ArtifactExportQueries:
    database_url: str
    codec: Any
    fingerprints: Any

    def export_job(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        export_job_id: UUID,
    ) -> ArtifactExportReceipt:
        with connect(self.database_url, row_factory=dict_row) as connection:
            return self._export_receipt(
                connection, identity, artifact_id, export_job_id
            )

    def export_file(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        export_job_id: UUID,
    ) -> ArtifactExportFile:
        with connect(self.database_url, row_factory=dict_row) as connection:
            receipt = self._export_receipt(
                connection, identity, artifact_id, export_job_id
            )
            if not receipt.file_available:
                raise GovernedDomainConflict("The artifact export file is not available.")
            row = connection.execute(
                """SELECT content_envelope FROM ai_artifact_export_outputs
                    WHERE export_job_id = %s AND tenant_id = %s AND user_id = %s""",
                (export_job_id, identity.tenant_id, identity.user_id),
            ).fetchone()
            if row is None:
                raise GovernedDomainConflict("The artifact export output is incomplete.")
            payload = self.codec.decrypt_json(
                row["content_envelope"],
                tenant_id=identity.tenant_id,
                resource_type="artifact-export-output",
                resource_id=str(export_job_id),
                field="content",
            )
            try:
                content = base64.b64decode(payload["base64"], validate=True)
            except (KeyError, TypeError, ValueError, binascii.Error) as error:
                raise GovernedDomainConflict(
                    "The artifact export output failed integrity validation."
                ) from error
            fingerprint = self.fingerprints.value(
                tenant_id=identity.tenant_id,
                purpose="artifact-export-content",
                payload={
                    "exportJobId": str(export_job_id),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "byteSize": len(content),
                },
            )
            if (
                len(content) != receipt.byte_size
                or fingerprint != receipt.content_fingerprint
            ):
                raise GovernedDomainConflict(
                    "The artifact export output failed integrity validation."
                )
            return ArtifactExportFile(
                content=content,
                media_type=receipt.media_type or "application/octet-stream",
                file_name=receipt.file_name or f"artifact-{export_job_id}",
                content_fingerprint=fingerprint,
            )

    @staticmethod
    def _export_receipt(
        connection: Any,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        export_job_id: UUID,
    ) -> ArtifactExportReceipt:
        row = connection.execute(
            """SELECT job.export_job_id, job.artifact_id,
                      job.artifact_revision, job.version_number,
                      job.export_format, job.export_state, job.requested_at,
                      job.completed_at, job.safe_error_code,
                      output.media_type, output.file_name, output.byte_size,
                      output.content_fingerprint
                 FROM ai_artifact_export_jobs AS job
                 LEFT JOIN ai_artifact_export_outputs AS output
                   ON output.export_job_id = job.export_job_id
                WHERE job.export_job_id = %s AND job.artifact_id = %s
                  AND job.tenant_id = %s AND job.user_id = %s""",
            (export_job_id, artifact_id, identity.tenant_id, identity.user_id),
        ).fetchone()
        if row is None:
            raise GovernedDomainNotFound("The artifact export is unavailable.")
        succeeded = row["export_state"] == "SUCCEEDED"
        return ArtifactExportReceipt(
            export_job_id=row["export_job_id"],
            artifact_id=row["artifact_id"],
            artifact_revision=row["artifact_revision"],
            version_number=row["version_number"],
            export_format=row["export_format"],
            state=row["export_state"],
            execution_available=governed_worker_available("ARTIFACT_EXPORT"),
            file_available=succeeded,
            media_type=row["media_type"] if succeeded else None,
            file_name=row["file_name"] if succeeded else None,
            byte_size=row["byte_size"] if succeeded else None,
            content_fingerprint=(
                row["content_fingerprint"] if succeeded else None
            ),
            requested_at=row["requested_at"],
            completed_at=row["completed_at"],
            safe_error_code=row["safe_error_code"],
        )
