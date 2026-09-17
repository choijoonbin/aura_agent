from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any

from psycopg import connect
from psycopg.rows import dict_row

from .artifact_contracts import ArtifactDraftContent
from .envelope import EnvelopeEncryptionError
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainUnavailable,
    GovernedPayloadCodec,
)
from .home_widget_contracts import (
    DwaionArtifactHomeItem,
    DwaionArtifactHomeProjection,
)
from .personal_domain_security import PersonalDomainIdentity


_PROJECTION_ERRORS = (
    EnvelopeEncryptionError,
    GovernedDomainUnavailable,
    KeyError,
    TypeError,
    ValueError,
)


class ArtifactHomeProjectionQueries:
    database_url: str
    codec: GovernedPayloadCodec

    def home_projection(
        self, identity: PersonalDomainIdentity, *, limit: int
    ) -> DwaionArtifactHomeProjection:
        if not 1 <= limit <= 50:
            raise GovernedDomainConflict("Artifact Home projection bounds are invalid.")
        with connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                """SELECT a.artifact_id, a.tenant_id, a.artifact_type,
                          a.artifact_state, a.revision, a.updated_at,
                          d.home_title_envelope, COUNT(*) OVER() AS visible_count
                     FROM ai_artifacts a
                     JOIN ai_artifact_drafts d ON d.artifact_id = a.artifact_id
                    WHERE a.tenant_id = %s AND a.user_id = %s
                      AND a.artifact_state IN ('DRAFT', 'REVIEW_REQUIRED')
                    ORDER BY a.updated_at DESC, a.artifact_id
                    LIMIT %s""",
                (identity.tenant_id, identity.user_id, limit),
            ).fetchall()
            if not rows:
                return DwaionArtifactHomeProjection(visible_count=0, slots=[])
            slots: list[DwaionArtifactHomeItem | None] = []
            for row in rows:
                try:
                    title = self.codec.decrypt_json(
                        row["home_title_envelope"],
                        tenant_id=row["tenant_id"],
                        resource_type="artifact-home-title",
                        resource_id=str(row["artifact_id"]),
                        field="title",
                    )["title"]
                    slots.append(DwaionArtifactHomeItem(
                        artifact_id=row["artifact_id"], title=title,
                        artifact_type=row["artifact_type"], state=row["artifact_state"],
                        revision=row["revision"], updated_at=row["updated_at"],
                    ))
                except _PROJECTION_ERRORS:
                    slots.append(None)
            return DwaionArtifactHomeProjection(
                visible_count=int(rows[0]["visible_count"]), slots=slots
            )

    def _home_title_envelope(
        self, identity: PersonalDomainIdentity, artifact_id: object, title: str
    ) -> str:
        return self.codec.encrypt_json(
            {"title": title}, tenant_id=identity.tenant_id,
            resource_type="artifact-home-title", resource_id=str(artifact_id),
            field="title",
        )


@dataclass(frozen=True)
class ArtifactHomeProjectionCoverage:
    pending: int
    failed: int
    succeeded: int

    @property
    def complete(self) -> bool:
        return self.pending == 0 and self.failed == 0


class PostgresArtifactHomeProjectionBackfill:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.codec = GovernedPayloadCodec()

    def process_batch(self, *, limit: int = 100) -> int:
        if not 1 <= limit <= 500:
            raise ValueError("Artifact Home projection batch limit is invalid.")
        with connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                """SELECT a.artifact_id, a.tenant_id, a.user_id, d.content_envelope
                     FROM ai_artifacts a
                     JOIN ai_artifact_drafts d ON d.artifact_id = a.artifact_id
                     LEFT JOIN agent_artifact_home_projection_backfill_receipts r
                       ON r.artifact_id = a.artifact_id
                    WHERE d.home_title_envelope IS NULL
                      AND (r.artifact_id IS NULL OR (
                           r.state = 'FAILED' AND r.attempt_count < 5
                           AND r.attempted_at <= CURRENT_TIMESTAMP - INTERVAL '5 minutes'))
                    ORDER BY a.tenant_id, a.user_id, a.updated_at DESC, a.artifact_id
                    LIMIT %s FOR UPDATE OF d SKIP LOCKED""",
                (limit,),
            ).fetchall()
            for row in rows:
                self._project(connection, row)
            return len(rows)

    def coverage(self) -> ArtifactHomeProjectionCoverage:
        with connect(self.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                """SELECT
                       COUNT(*) FILTER (WHERE d.home_title_envelope IS NULL) AS pending,
                       COUNT(*) FILTER (WHERE d.home_title_envelope IS NULL
                            AND r.state = 'FAILED') AS failed,
                       COUNT(*) FILTER (WHERE d.home_title_envelope IS NOT NULL) AS succeeded
                     FROM ai_artifacts a
                     JOIN ai_artifact_drafts d ON d.artifact_id = a.artifact_id
                     LEFT JOIN agent_artifact_home_projection_backfill_receipts r
                       ON r.artifact_id = a.artifact_id"""
            ).fetchone()
        return ArtifactHomeProjectionCoverage(
            pending=int(row["pending"]), failed=int(row["failed"]),
            succeeded=int(row["succeeded"]),
        )

    def _project(self, connection: Any, row: dict[str, object]) -> None:
        state, safe_error_code = "SUCCEEDED", None
        try:
            content = ArtifactDraftContent.model_validate(self.codec.decrypt_json(
                row["content_envelope"], tenant_id=row["tenant_id"],
                resource_type="artifact-draft", resource_id=str(row["artifact_id"]),
                field="content",
            ))
            title_envelope = self.codec.encrypt_json(
                {"title": content.title}, tenant_id=row["tenant_id"],
                resource_type="artifact-home-title", resource_id=str(row["artifact_id"]),
                field="title",
            )
            connection.execute(
                """UPDATE ai_artifact_drafts SET home_title_envelope = %s
                    WHERE artifact_id = %s AND tenant_id = %s AND user_id = %s
                      AND home_title_envelope IS NULL""",
                (title_envelope, row["artifact_id"], row["tenant_id"], row["user_id"]),
            )
        except _PROJECTION_ERRORS:
            state, safe_error_code = "FAILED", "ARTIFACT_HOME_TITLE_PROJECTION_FAILED"
        connection.execute(
            """INSERT INTO agent_artifact_home_projection_backfill_receipts (
                   artifact_id, tenant_id, user_id, state, safe_error_code,
                   attempt_count, attempted_at)
               VALUES (%s, %s, %s, %s, %s, 1, CURRENT_TIMESTAMP)
               ON CONFLICT (artifact_id) DO UPDATE
                   SET state = EXCLUDED.state, safe_error_code = EXCLUDED.safe_error_code,
                       attempt_count = LEAST(5,
                           agent_artifact_home_projection_backfill_receipts.attempt_count + 1),
                       attempted_at = CURRENT_TIMESTAMP""",
            (row["artifact_id"], row["tenant_id"], row["user_id"], state, safe_error_code),
        )


def validate_artifact_home_projection_activation() -> None:
    if os.getenv(
        "DWP_DWAION_HOME_TITLE_PROJECTION_READY", "false"
    ).strip().lower() != "true":
        return
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("Artifact Home projection activation requires Postgres.")
    coverage = PostgresArtifactHomeProjectionBackfill(database_url).coverage()
    if not coverage.complete:
        raise RuntimeError(
            "Artifact Home projection activation requires complete backfill coverage."
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a bounded DWAI-ON artifact Home title projection backfill."
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-batches", type=int, default=10)
    parser.add_argument("--require-complete", action="store_true")
    arguments = parser.parse_args()
    if not 1 <= arguments.max_batches <= 100:
        parser.error("--max-batches must be between 1 and 100")
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        parser.error("DWP_AGENT_DATABASE_URL is required")
    backfill = PostgresArtifactHomeProjectionBackfill(database_url)
    processed = 0
    for _ in range(arguments.max_batches):
        batch = backfill.process_batch(limit=arguments.batch_size)
        processed += batch
        if batch < arguments.batch_size:
            break
    coverage = backfill.coverage()
    print(json.dumps({"processed": processed, "pending": coverage.pending,
                      "failed": coverage.failed, "succeeded": coverage.succeeded,
                      "complete": coverage.complete}, sort_keys=True))
    return 2 if arguments.require_complete and not coverage.complete else 0


if __name__ == "__main__":
    raise SystemExit(main())
