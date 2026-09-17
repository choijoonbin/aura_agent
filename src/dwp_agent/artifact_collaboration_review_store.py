from __future__ import annotations

from uuid import UUID

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .artifact_collaboration_contracts import (
    DecideTeamArtifactReviewStageRequest,
    TeamArtifactReviewDecision,
    TeamArtifactWorkspace,
)
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
)
from .personal_domain_security import PersonalDomainIdentity


class ArtifactCollaborationReviewCommands:
    def decide_review_stage(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        stage_id: UUID,
        request: DecideTeamArtifactReviewStageRequest,
    ) -> TeamArtifactWorkspace:
        proof = self._proof(identity, artifact_id, "REVIEW_DECISION", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                replay = self._replay(
                    connection,
                    identity,
                    request.command_id,
                    "REVIEW_DECISION",
                    proof,
                )
                if replay is not None:
                    return TeamArtifactWorkspace.model_validate(replay)
                workspace = self._locked_workspace(
                    connection,
                    identity,
                    artifact_id,
                    owner_only=False,
                    require_edit=False,
                )
                stage = connection.execute(
                    """SELECT stage_id, stage_order, stage_key, assignee_subject_id,
                              stage_state, revision
                         FROM ai_artifact_review_stages
                        WHERE stage_id = %s AND workspace_id = %s
                          AND artifact_id = %s AND tenant_id = %s
                        FOR UPDATE""",
                    (
                        stage_id,
                        workspace["workspace_id"],
                        artifact_id,
                        identity.tenant_id,
                    ),
                ).fetchone()
                if stage is None:
                    raise GovernedDomainNotFound("The artifact review stage is unavailable.")
                if stage["stage_state"] != "PENDING":
                    raise GovernedDomainConflict("Only a pending review stage can be decided.")
                if stage["assignee_subject_id"] != identity.user_id:
                    raise GovernedDomainNotFound("The artifact review stage is unavailable.")
                if int(stage["revision"]) != request.expected_revision:
                    raise GovernedDomainConflict("The artifact review stage revision has changed.")
                prior = connection.execute(
                    """SELECT COUNT(*) AS incomplete
                         FROM ai_artifact_review_stages
                        WHERE workspace_id = %s AND stage_order < %s
                          AND stage_state <> 'APPROVED'""",
                    (workspace["workspace_id"], stage["stage_order"]),
                ).fetchone()
                if int(prior["incomplete"]) > 0:
                    raise GovernedDomainConflict(
                        "Earlier artifact review stages must be approved first."
                    )
                state = (
                    "APPROVED"
                    if request.decision == TeamArtifactReviewDecision.APPROVE
                    else "REJECTED"
                )
                revision = int(stage["revision"]) + 1
                evidence = self.fingerprints.value(
                    tenant_id=identity.tenant_id,
                    purpose="artifact-review-decision",
                    payload={
                        "workspaceId": str(workspace["workspace_id"]),
                        "artifactId": str(artifact_id),
                        "stageId": str(stage_id),
                        "stageKey": stage["stage_key"],
                        "decision": request.decision.value,
                        "revision": revision,
                        "contentSha256": workspace["content_sha256"],
                        "requestFingerprint": proof.request_fingerprint,
                    },
                )
                reason = self.codec.encrypt_json(
                    {"changeReason": request.change_reason},
                    tenant_id=identity.tenant_id,
                    resource_type="artifact-review-stage",
                    resource_id=str(stage_id),
                    field="decision-reason",
                )
                connection.execute(
                    """UPDATE ai_artifact_review_stages
                          SET stage_state = %s, revision = %s,
                              evidence_fingerprint = %s,
                              decided_by_user_id = %s,
                              decision_reason_envelope = %s,
                              decided_at = CURRENT_TIMESTAMP,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE stage_id = %s""",
                    (
                        state,
                        revision,
                        evidence,
                        identity.user_id,
                        reason,
                        stage_id,
                    ),
                )
                result = self._workspace_by_id(
                    connection,
                    identity,
                    workspace["workspace_id"],
                    require_edit=False,
                )
                self._record_command(
                    connection,
                    identity,
                    artifact_id,
                    "REVIEW_DECISION",
                    request.command_id,
                    proof,
                    result,
                )
                self._event(
                    connection,
                    workspace_id=workspace["workspace_id"],
                    artifact_id=artifact_id,
                    identity=identity,
                    command_id=request.command_id,
                    event_type=(
                        "REVIEW_APPROVED" if state == "APPROVED" else "REVIEW_REJECTED"
                    ),
                    previous_state="PENDING",
                    current_state=state,
                    revision=revision,
                    request_fingerprint=proof.request_fingerprint,
                )
                return result
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact staged review is unavailable."
            ) from error
