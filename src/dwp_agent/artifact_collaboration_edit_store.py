from __future__ import annotations

from uuid import UUID

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .artifact_collaboration_contracts import (
    ResolveTeamArtifactConflictRequest,
    SubmitTeamArtifactEditRequest,
    TeamArtifactConflictResolution,
    TeamArtifactEditResult,
    TeamArtifactWorkspace,
)
from .artifact_collaboration_content_store import content_sha256
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
)
from .personal_domain_security import PersonalDomainIdentity


class ArtifactCollaborationEditCommands:
    def edit(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        request: SubmitTeamArtifactEditRequest,
    ) -> TeamArtifactEditResult:
        proof = self._proof(identity, artifact_id, "EDIT", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                replay = self._replay(
                    connection, identity, request.command_id, "EDIT", proof
                )
                if replay is not None:
                    return TeamArtifactEditResult.model_validate(replay)
                row = self._locked_workspace(
                    connection, identity, artifact_id, owner_only=False, require_edit=True
                )
                if row["workspace_state"] != "ACTIVE":
                    raise GovernedDomainConflict(
                        "The team artifact workspace is read-only."
                    )
                existing_conflict = connection.execute(
                    """SELECT conflict_id FROM ai_artifact_team_conflicts
                        WHERE workspace_id = %s AND conflict_state = 'OPEN'""",
                    (row["workspace_id"],),
                ).fetchone()
                if existing_conflict is not None:
                    raise GovernedDomainConflict(
                        "Resolve the open artifact conflict before editing."
                    )
                if request.base_revision != int(row["revision"]):
                    conflict = self._create_conflict(
                        connection, identity, row, request
                    )
                    workspace = self._workspace_from_row(connection, identity, row)
                    result = TeamArtifactEditResult(
                        state="CONFLICT", workspace=workspace, conflict=conflict
                    )
                    event_type = "CONFLICT_DETECTED"
                    current_state = "CONFLICT"
                    revision = int(row["revision"])
                else:
                    revision = int(row["revision"]) + 1
                    envelope = self._encrypt_content(
                        identity.tenant_id, row["workspace_id"], request.content
                    )
                    fingerprint = content_sha256(request.content)
                    connection.execute(
                        """UPDATE ai_artifact_team_workspaces
                              SET revision = %s, content_envelope = %s,
                                  content_sha256 = %s, updated_at = CURRENT_TIMESTAMP
                            WHERE workspace_id = %s""",
                        (revision, envelope, fingerprint, row["workspace_id"]),
                    )
                    self._version(
                        connection,
                        row["workspace_id"],
                        identity.tenant_id,
                        identity.user_id,
                        revision,
                        envelope,
                        fingerprint,
                    )
                    workspace = self._workspace_by_id(
                        connection, identity, row["workspace_id"], require_edit=False
                    )
                    result = TeamArtifactEditResult(
                        state="APPLIED", workspace=workspace
                    )
                    event_type = "EDIT_APPLIED"
                    current_state = "ACTIVE"
                self._record_command(
                    connection,
                    identity,
                    artifact_id,
                    "EDIT",
                    request.command_id,
                    proof,
                    result,
                )
                self._event(
                    connection,
                    workspace_id=row["workspace_id"],
                    artifact_id=artifact_id,
                    identity=identity,
                    command_id=request.command_id,
                    event_type=event_type,
                    previous_state="ACTIVE",
                    current_state=current_state,
                    revision=revision,
                    request_fingerprint=proof.request_fingerprint,
                )
                return result
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration is unavailable."
            ) from error

    def resolve_conflict(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        conflict_id: UUID,
        request: ResolveTeamArtifactConflictRequest,
    ) -> TeamArtifactWorkspace:
        proof = self._proof(identity, artifact_id, "RESOLVE_CONFLICT", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                replay = self._replay(
                    connection,
                    identity,
                    request.command_id,
                    "RESOLVE_CONFLICT",
                    proof,
                )
                if replay is not None:
                    return TeamArtifactWorkspace.model_validate(replay)
                workspace = self._locked_workspace(
                    connection, identity, artifact_id, owner_only=False, require_edit=True
                )
                if int(workspace["revision"]) != request.expected_revision:
                    raise GovernedDomainConflict("The team workspace revision has changed.")
                conflict = connection.execute(
                    """SELECT * FROM ai_artifact_team_conflicts
                        WHERE conflict_id = %s AND workspace_id = %s
                          AND tenant_id = %s FOR UPDATE""",
                    (conflict_id, workspace["workspace_id"], identity.tenant_id),
                ).fetchone()
                if conflict is None or conflict["conflict_state"] != "OPEN":
                    raise GovernedDomainNotFound(
                        "The open artifact conflict is unavailable."
                    )
                resolution = request.resolution
                if resolution == TeamArtifactConflictResolution.USE_LOCAL:
                    content = self._decrypt_conflict_content(conflict, "local")
                    state = "RESOLVED_LOCAL"
                elif resolution == TeamArtifactConflictResolution.USE_SERVER:
                    content = self._decrypt_conflict_content(conflict, "server")
                    state = "RESOLVED_SERVER"
                elif resolution == TeamArtifactConflictResolution.MERGE:
                    content = request.merged_content
                    state = "RESOLVED_MERGED"
                elif resolution == TeamArtifactConflictResolution.STASH:
                    content = self._workspace_content(workspace)
                    state = "STASHED"
                else:
                    version = connection.execute(
                        """SELECT content_envelope, content_sha256
                             FROM ai_artifact_team_versions
                            WHERE workspace_id = %s AND revision = %s""",
                        (workspace["workspace_id"], conflict["base_revision"]),
                    ).fetchone()
                    if version is None:
                        raise GovernedDomainConflict(
                            "The rollback revision is unavailable."
                        )
                    content = self.codec.decrypt_json(
                        version["content_envelope"],
                        tenant_id=identity.tenant_id,
                        resource_type="artifact-team-workspace",
                        resource_id=str(workspace["workspace_id"]),
                        field="content",
                    )
                    content = ArtifactDraftContent.model_validate(content)
                    state = "ROLLED_BACK"
                revision = int(workspace["revision"])
                if resolution != TeamArtifactConflictResolution.STASH:
                    revision += 1
                    envelope = self._encrypt_content(
                        identity.tenant_id, workspace["workspace_id"], content
                    )
                    fingerprint = content_sha256(content)
                    connection.execute(
                        """UPDATE ai_artifact_team_workspaces
                              SET revision = %s, content_envelope = %s,
                                  content_sha256 = %s, updated_at = CURRENT_TIMESTAMP
                            WHERE workspace_id = %s""",
                        (
                            revision,
                            envelope,
                            fingerprint,
                            workspace["workspace_id"],
                        ),
                    )
                    self._version(
                        connection,
                        workspace["workspace_id"],
                        identity.tenant_id,
                        identity.user_id,
                        revision,
                        envelope,
                        fingerprint,
                    )
                connection.execute(
                    """UPDATE ai_artifact_team_conflicts
                          SET conflict_state = %s, resolved_at = CURRENT_TIMESTAMP
                        WHERE conflict_id = %s""",
                    (state, conflict_id),
                )
                resolved = self._workspace_by_id(
                    connection, identity, workspace["workspace_id"], require_edit=False
                )
                self._record_command(
                    connection,
                    identity,
                    artifact_id,
                    "RESOLVE_CONFLICT",
                    request.command_id,
                    proof,
                    resolved,
                )
                self._event(
                    connection,
                    workspace_id=workspace["workspace_id"],
                    artifact_id=artifact_id,
                    identity=identity,
                    command_id=request.command_id,
                    event_type="CONFLICT_RESOLVED",
                    previous_state="CONFLICT",
                    current_state=state,
                    revision=revision,
                    request_fingerprint=proof.request_fingerprint,
                )
                return resolved
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration is unavailable."
            ) from error
