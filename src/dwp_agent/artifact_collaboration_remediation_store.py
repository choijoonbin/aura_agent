from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .artifact_collaboration_contracts import (
    ExecuteTeamArtifactRemediationRequest,
    TeamArtifactRemediationKind,
    TeamArtifactRemediationReceipt,
)
from .artifact_dlp import (
    assess_artifact,
    remediate_artifact_content,
    remediate_artifact_text,
)
from .artifact_review_notification_contracts import (
    ArtifactReviewNotificationResult,
)
from .canonical_json import canonical_json_bytes
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
)
from .personal_domain_security import PersonalDomainIdentity
from .artifact_collaboration_provider import ArtifactCollaborationProviderUnavailable


class ArtifactCollaborationRemediationCommands:
    def remediate(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        request: ExecuteTeamArtifactRemediationRequest,
    ) -> TeamArtifactRemediationReceipt:
        command_type = {
            TeamArtifactRemediationKind.AUTOMATIC_MASKING: "REMEDIATE_MASK",
            TeamArtifactRemediationKind.SYNTHETIC_REPLACEMENT: "REMEDIATE_SYNTHETIC",
            TeamArtifactRemediationKind.REVIEW_NOTIFICATION: "REVIEW_NOTIFICATION",
        }[request.action]
        proof = self._proof(identity, artifact_id, command_type, request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                replay = self._replay(
                    connection, identity, request.command_id, command_type, proof
                )
                if replay is not None:
                    return TeamArtifactRemediationReceipt.model_validate(replay)
                artifact = self._owner_artifact(connection, identity, artifact_id)
                if int(artifact["revision"]) != request.expected_revision:
                    raise GovernedDomainConflict("The governed artifact revision has changed.")
                if request.action == TeamArtifactRemediationKind.REVIEW_NOTIFICATION:
                    receipt, workspace_id = self._notify_review(
                        connection, identity, artifact, request
                    )
                    event_type = "REVIEW_NOTIFICATION_SENT"
                    event_revision = receipt.workspace_revision or receipt.artifact_revision
                else:
                    receipt, workspace_id = self._apply_content_remediation(
                        connection, identity, artifact, request
                    )
                    event_type = "REMEDIATION_APPLIED"
                    event_revision = receipt.artifact_revision
                self._record_command(
                    connection,
                    identity,
                    artifact_id,
                    command_type,
                    request.command_id,
                    proof,
                    receipt,
                )
                self._event(
                    connection,
                    workspace_id=workspace_id,
                    artifact_id=artifact_id,
                    identity=identity,
                    command_id=request.command_id,
                    event_type=event_type,
                    previous_state="DRAFT",
                    current_state="DRAFT",
                    revision=event_revision,
                    request_fingerprint=proof.request_fingerprint,
                )
                return receipt
        except (
            GovernedDomainConflict,
            GovernedDomainNotFound,
            GovernedDomainUnavailable,
        ):
            raise
        except ArtifactCollaborationProviderUnavailable as error:
            raise GovernedDomainUnavailable(
                "The governed review notification provider did not accept the request."
            ) from error
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "The governed artifact remediation could not be completed."
            ) from error

    def _apply_content_remediation(
        self,
        connection,
        identity: PersonalDomainIdentity,
        artifact,
        request: ExecuteTeamArtifactRemediationRequest,
    ) -> tuple[TeamArtifactRemediationReceipt, UUID | None]:
        content = self._artifact_content(artifact)
        synthetic = request.action == TeamArtifactRemediationKind.SYNTHETIC_REPLACEMENT
        source_state = connection.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (
                          WHERE verification_state = 'SERVER_VERIFIED'
                      ) AS verified
                 FROM ai_artifact_draft_sources WHERE artifact_id = %s""",
            (artifact["artifact_id"],),
        ).fetchone()
        source_total = int(source_state["total"])
        source_verified = int(source_state["verified"])
        all_sources_verified = source_total == source_verified
        assessment = assess_artifact(
            content,
            all_sources_verified=all_sources_verified,
        )
        if not all_sources_verified:
            raise GovernedDomainConflict(
                "Verify every current artifact source before applying DLP remediation."
            )
        content_findings = [
            finding for finding in assessment.findings if finding.field in {"title", "body"}
        ]
        if not content_findings:
            raise GovernedDomainConflict(
                "No current artifact DLP findings require remediation."
            )
        source_content_fingerprint = str(artifact["content_fingerprint"])
        finding_manifest = {
            "policyKey": "DWP_DETERMINISTIC_DLP_V1",
            "policyVersion": 1,
            "artifactId": str(artifact["artifact_id"]),
            "artifactRevision": int(artifact["revision"]),
            "sourceContentFingerprint": source_content_fingerprint,
            "sourceVerification": {
                "total": source_total,
                "verified": source_verified,
                "allVerified": all_sources_verified,
            },
            "findings": [
                finding.model_dump(mode="json", by_alias=True)
                for finding in content_findings
            ],
        }
        finding_manifest_sha256 = hashlib.sha256(
            canonical_json_bytes(finding_manifest)
        ).hexdigest()
        next_content, affected_count, remediated_codes = remediate_artifact_content(
            content,
            synthetic=synthetic,
        )
        residual = assess_artifact(
            next_content,
            all_sources_verified=all_sources_verified,
        )
        residual_findings = [
            finding for finding in residual.findings if finding.field in {"title", "body"}
        ]
        if affected_count < 1 or residual_findings:
            raise GovernedDomainConflict(
                "Artifact DLP remediation left current findings unresolved."
            )
        artifact_revision = int(artifact["revision"])
        payload = next_content.model_dump(mode="json", by_alias=True)
        result_content_sha256 = hashlib.sha256(
            canonical_json_bytes(payload)
        ).hexdigest()
        envelope = self.codec.encrypt_json(
            payload,
            tenant_id=identity.tenant_id,
            resource_type="artifact-draft",
            resource_id=str(artifact["artifact_id"]),
            field="content",
        )
        fingerprint = self.fingerprints.value(
            tenant_id=identity.tenant_id,
            purpose="artifact-content",
            payload=payload,
        )
        draft = connection.execute(
            "SELECT draft_revision FROM ai_artifact_drafts WHERE artifact_id = %s FOR UPDATE",
            (artifact["artifact_id"],),
        ).fetchone()
        if draft is None:
            raise GovernedDomainNotFound("The governed artifact draft is unavailable.")
        draft_revision = int(draft["draft_revision"]) + 1
        artifact_revision += 1
        connection.execute(
            """UPDATE ai_artifact_drafts
                  SET draft_revision = %s, content_envelope = %s,
                      content_fingerprint = %s, updated_by_user_id = %s,
                      updated_at = CURRENT_TIMESTAMP
                WHERE artifact_id = %s AND tenant_id = %s AND user_id = %s""",
            (
                draft_revision,
                envelope,
                fingerprint,
                identity.user_id,
                artifact["artifact_id"],
                identity.tenant_id,
                identity.user_id,
            ),
        )
        connection.execute(
            """UPDATE ai_artifacts
                  SET artifact_state = 'DRAFT', revision = %s,
                      current_draft_revision = %s, updated_at = CURRENT_TIMESTAMP
                WHERE artifact_id = %s AND tenant_id = %s AND user_id = %s""",
            (
                artifact_revision,
                draft_revision,
                artifact["artifact_id"],
                identity.tenant_id,
                identity.user_id,
            ),
        )
        workspace = connection.execute(
            """SELECT workspace_id, revision FROM ai_artifact_team_workspaces
                WHERE artifact_id = %s AND tenant_id = %s""",
            (artifact["artifact_id"], identity.tenant_id),
        ).fetchone()
        now = connection.execute("SELECT CURRENT_TIMESTAMP AS now").fetchone()["now"]
        result = {
            "commandId": str(request.command_id),
            "artifactId": str(artifact["artifact_id"]),
            "action": request.action.value,
            "sourceContentFingerprint": source_content_fingerprint,
            "findingManifestSha256": finding_manifest_sha256,
            "resultContentSha256": result_content_sha256,
            "artifactRevision": artifact_revision,
            "affectedCount": affected_count,
            "remediatedCodes": remediated_codes,
            "residualFindingCount": len(residual_findings),
        }
        return (
            TeamArtifactRemediationReceipt(
                receipt_id=uuid4(),
                command_id=request.command_id,
                artifact_id=artifact["artifact_id"],
                action=request.action,
                state="SUCCEEDED",
                artifact_revision=artifact_revision,
                workspace_revision=(int(workspace["revision"]) if workspace else None),
                affected_count=affected_count,
                provider_receipt_id=None,
                source_content_fingerprint=source_content_fingerprint,
                finding_manifest_sha256=finding_manifest_sha256,
                result_content_sha256=result_content_sha256,
                remediated_codes=remediated_codes,
                residual_finding_count=len(residual_findings),
                result_sha256=hashlib.sha256(canonical_json_bytes(result)).hexdigest(),
                completed_at=now,
            ),
            workspace["workspace_id"] if workspace else None,
        )

    def _notify_review(
        self,
        connection,
        identity: PersonalDomainIdentity,
        artifact,
        request: ExecuteTeamArtifactRemediationRequest,
    ) -> tuple[TeamArtifactRemediationReceipt, UUID]:
        workspace = self._locked_workspace(
            connection,
            identity,
            artifact["artifact_id"],
            owner_only=True,
            require_edit=True,
        )
        stage = connection.execute(
            """SELECT stage_id, assignee_subject_id, stage_state
                 FROM ai_artifact_review_stages
                WHERE stage_id = %s AND workspace_id = %s AND tenant_id = %s
                FOR UPDATE""",
            (request.stage_id, workspace["workspace_id"], identity.tenant_id),
        ).fetchone()
        if stage is None or stage["stage_state"] != "PENDING":
            raise GovernedDomainConflict(
                "A current pending artifact review stage is required."
            )
        if not stage["assignee_subject_id"]:
            raise GovernedDomainConflict("The pending review stage has no assignee.")
        result = ArtifactReviewNotificationResult.model_validate(
            self.provider.notify_review(
                command_id=request.command_id,
                artifact_id=artifact["artifact_id"],
                workspace_id=workspace["workspace_id"],
                stage_id=stage["stage_id"],
                tenant_id=identity.tenant_id,
                actor_user_id=identity.user_id,
                assignee_subject_id=stage["assignee_subject_id"],
                workspace_revision=int(workspace["revision"]),
                reason_code=request.reason_code,
                change_reason=request.change_reason,
                correlation_id=identity.correlation_id,
            )
        )
        if (
            result.commandId != request.command_id
            or result.artifactId != artifact["artifact_id"]
            or result.workspaceId != workspace["workspace_id"]
            or result.stageId != stage["stage_id"]
            or result.assigneeSubjectId != stage["assignee_subject_id"]
            or result.workspaceRevision != int(workspace["revision"])
            or result.reasonCode != request.reason_code
            or result.changeReason != request.change_reason
            or result.deliveryState != "DELIVERED"
        ):
            raise ArtifactCollaborationProviderUnavailable(
                "ARTIFACT_REVIEW_NOTIFICATION_BINDING_INVALID"
            )
        return (
            TeamArtifactRemediationReceipt(
                receipt_id=uuid4(),
                command_id=request.command_id,
                artifact_id=artifact["artifact_id"],
                action=request.action,
                state="SUCCEEDED",
                artifact_revision=int(artifact["revision"]),
                workspace_revision=int(workspace["revision"]),
                affected_count=1,
                provider_receipt_id=result.providerReceiptId,
                result_sha256=result.resultSha256,
                completed_at=result.deliveredAt,
            ),
            workspace["workspace_id"],
        )


def _remediate_text(value: str, *, synthetic: bool) -> tuple[str, int]:
    remediated, count, _codes = remediate_artifact_text(
        value,
        synthetic=synthetic,
    )
    return remediated, count
