from __future__ import annotations

from uuid import uuid4

from .artifact_collaboration_contracts import (
    TeamArtifactGovernanceGate,
    TeamArtifactReviewStage,
)


class ArtifactCollaborationGovernanceEvidence:
    def _initialize_review_stages(
        self, connection, workspace_id, artifact_id, identity, content_fingerprint
    ):
        reviewers = [
            row["subject_id"]
            for row in connection.execute(
                """SELECT subject_id FROM ai_artifact_team_members
                    WHERE workspace_id = %s AND allowed AND member_role = 'REVIEWER'
                    ORDER BY subject_id""",
                (workspace_id,),
            ).fetchall()
        ]
        author_stage_id = uuid4()
        author_reason = self.codec.encrypt_json(
            {"changeReason": "The author created the governed collaboration workspace."},
            tenant_id=identity.tenant_id,
            resource_type="artifact-review-stage",
            resource_id=str(author_stage_id),
            field="decision-reason",
        )
        stages = [
            (
                author_stage_id,
                1,
                "AUTHOR",
                identity.user_id,
                "APPROVED",
                content_fingerprint,
                identity.user_id,
                author_reason,
                True,
            ),
            (
                uuid4(),
                2,
                "PRIMARY_REVIEW",
                reviewers[0] if reviewers else None,
                "PENDING" if reviewers else "UNAVAILABLE",
                None,
                None,
                None,
                False,
            ),
            (
                uuid4(),
                3,
                "FINAL_APPROVAL",
                reviewers[1] if len(reviewers) > 1 else None,
                "PENDING" if len(reviewers) > 1 else "UNAVAILABLE",
                None,
                None,
                None,
                False,
            ),
        ]
        for (
            stage_id,
            order,
            key,
            assignee,
            state,
            evidence,
            decided_by,
            reason,
            decided,
        ) in stages:
            connection.execute(
                """INSERT INTO ai_artifact_review_stages (
                       stage_id, workspace_id, artifact_id, tenant_id, stage_order,
                       stage_key, assignee_subject_id, stage_state,
                       evidence_fingerprint, decided_by_user_id,
                       decision_reason_envelope, decided_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END)""",
                (
                    stage_id,
                    workspace_id,
                    artifact_id,
                    identity.tenant_id,
                    order,
                    key,
                    assignee,
                    state,
                    evidence,
                    decided_by,
                    reason,
                    decided,
                ),
            )
    def _review_stages(self, connection, workspace_id):
        return [
            TeamArtifactReviewStage(
                stage_id=row["stage_id"],
                stage_order=row["stage_order"],
                stage_key=row["stage_key"],
                assignee_subject_id=row["assignee_subject_id"],
                state=row["stage_state"],
                revision=row["revision"],
                evidence_fingerprint=row["evidence_fingerprint"],
                decided_by_subject_id=row["decided_by_user_id"],
                decided_at=row["decided_at"],
            )
            for row in connection.execute(
                """SELECT stage_id, stage_order, stage_key, assignee_subject_id,
                          stage_state, revision, evidence_fingerprint,
                          decided_by_user_id, decided_at
                     FROM ai_artifact_review_stages
                    WHERE workspace_id = %s ORDER BY stage_order""",
                (workspace_id,),
            ).fetchall()
        ]

    def _governance_gates(self, connection, workspace_row):
        artifact_id = workspace_row["artifact_id"]
        version = connection.execute(
            """SELECT version_number, content_fingerprint, created_at
                 FROM ai_artifact_versions
                WHERE artifact_id = %s ORDER BY version_number DESC LIMIT 1""",
            (artifact_id,),
        ).fetchone()
        dlp = connection.execute(
            """SELECT outcome, content_fingerprint, created_at, expires_at
                 FROM ai_artifact_preflight_runs
                WHERE artifact_id = %s ORDER BY created_at DESC LIMIT 1""",
            (artifact_id,),
        ).fetchone()
        citation = connection.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE verification_state = 'SERVER_VERIFIED') AS verified,
                      MAX(verified_at) AS verified_at
                 FROM ai_artifact_draft_sources WHERE artifact_id = %s""",
            (artifact_id,),
        ).fetchone()
        acl = connection.execute(
            """SELECT preflight_id, preflight_state, evidence_sha256, created_at,
                      expires_at
                 FROM ai_artifact_team_preflights
                WHERE artifact_id = %s ORDER BY created_at DESC LIMIT 1""",
            (artifact_id,),
        ).fetchone()
        dlp_state = "UNAVAILABLE"
        dlp_code = "DLP_EVIDENCE_UNAVAILABLE"
        if dlp is not None:
            dlp_state = {
                "PASS": "PASS",
                "REVIEW": "REVIEW",
                "BLOCKED": "BLOCKED",
            }[dlp["outcome"]]
            dlp_code = f"DLP_{dlp['outcome']}"
        total_sources = int(citation["total"] or 0)
        verified_sources = int(citation["verified"] or 0)
        if total_sources == 0:
            citation_state, citation_code = "REVIEW", "CITATION_SOURCES_EMPTY"
        elif verified_sources == total_sources:
            citation_state, citation_code = "PASS", "CITATION_SOURCES_VERIFIED"
        elif verified_sources > 0:
            citation_state, citation_code = "REVIEW", "CITATION_SOURCES_PARTIAL"
        else:
            citation_state, citation_code = "UNAVAILABLE", "CITATION_VERIFICATION_UNAVAILABLE"
        acl_state = "UNAVAILABLE"
        acl_code = "RECIPIENT_ACL_EVIDENCE_UNAVAILABLE"
        if acl is not None:
            acl_state = {
                "READY": "PASS",
                "PARTIAL": "REVIEW",
                "PERMISSION_DENIED": "BLOCKED",
            }[acl["preflight_state"]]
            acl_code = f"RECIPIENT_ACL_{acl['preflight_state']}"
        return [
            TeamArtifactGovernanceGate(
                key="DLP",
                state=dlp_state,
                detail_code=dlp_code,
                evidence_reference=None if dlp is None else "artifact-preflight",
                evidence_fingerprint=None if dlp is None else dlp["content_fingerprint"],
                evaluated_at=None if dlp is None else dlp["created_at"],
            ),
            TeamArtifactGovernanceGate(
                key="CITATION",
                state=citation_state,
                detail_code=citation_code,
                evidence_reference=f"verified:{verified_sources}/total:{total_sources}",
                evidence_fingerprint=None,
                evaluated_at=citation["verified_at"],
            ),
            TeamArtifactGovernanceGate(
                key="RECIPIENT_ACL",
                state=acl_state,
                detail_code=acl_code,
                evidence_reference=None if acl is None else str(acl["preflight_id"]),
                evidence_fingerprint=None if acl is None else acl["evidence_sha256"],
                evaluated_at=None if acl is None else acl["created_at"],
            ),
            TeamArtifactGovernanceGate(
                key="IMMUTABLE_VERSION",
                state="PASS" if version is not None else "REVIEW",
                detail_code=(
                    "IMMUTABLE_VERSION_CREATED"
                    if version is not None
                    else "IMMUTABLE_VERSION_REQUIRED"
                ),
                evidence_reference=(
                    None if version is None else f"version:{version['version_number']}"
                ),
                evidence_fingerprint=(
                    None if version is None else version["content_fingerprint"]
                ),
                evaluated_at=None if version is None else version["created_at"],
            ),
        ]
