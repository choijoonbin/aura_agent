from __future__ import annotations

from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .artifact_collaboration_contracts import (
    CreateTeamArtifactWorkspaceRequest,
    RunTeamArtifactPreflightRequest,
    TeamArtifactMember,
    TeamArtifactPreflight,
    TeamArtifactPreflightState,
    TeamArtifactWorkspace,
    UpdateTeamArtifactMembersRequest,
)
from .artifact_collaboration_provider import ArtifactCollaborationProviderUnavailable
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
)
from .personal_domain_security import PersonalDomainIdentity
from .artifact_collaboration_content_store import content_sha256


class ArtifactCollaborationPreflightCommands:
    def preflight(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        request: RunTeamArtifactPreflightRequest,
    ) -> TeamArtifactPreflight:
        proof = self._proof(identity, artifact_id, "PREFLIGHT", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                replay = self._replay(
                    connection, identity, request.command_id, "PREFLIGHT", proof
                )
                if replay is not None:
                    return TeamArtifactPreflight.model_validate(replay)
                artifact = self._owner_artifact(connection, identity, artifact_id)
                if int(artifact["revision"]) != request.artifact_revision:
                    raise GovernedDomainConflict("The artifact revision has changed.")
                sources = self._selected_sources(connection, identity, artifact_id, request)
                try:
                    result = self.provider.preflight(
                        artifact_id=artifact_id,
                        team_id=request.team_id,
                        tenant_id=identity.tenant_id,
                        owner_user_id=identity.user_id,
                        artifact_revision=request.artifact_revision,
                        members=request.members,
                        sources=sources,
                        exclude_inaccessible_sources=request.exclude_inaccessible_sources,
                        correlation_id=identity.correlation_id,
                    )
                except ArtifactCollaborationProviderUnavailable as error:
                    raise GovernedDomainUnavailable(str(error)) from error
                denied_members = [member for member in result.members if not member.allowed]
                if denied_members:
                    state = TeamArtifactPreflightState.PERMISSION_DENIED
                elif result.excludedSources:
                    state = (
                        TeamArtifactPreflightState.PARTIAL
                        if request.exclude_inaccessible_sources
                        else TeamArtifactPreflightState.PERMISSION_DENIED
                    )
                else:
                    state = TeamArtifactPreflightState.READY
                now = connection.execute(
                    "SELECT CURRENT_TIMESTAMP AS now"
                ).fetchone()["now"]
                if result.expiresAt <= now:
                    raise GovernedDomainUnavailable(
                        "The artifact ACL provider returned expired evidence."
                    )
                preflight_id = uuid4()
                member_payload = [
                    member.model_dump(mode="json", by_alias=True)
                    for member in result.members
                ]
                source_payload = {
                    "allowed": [
                        source.model_dump(mode="json", by_alias=True)
                        for source in result.allowedSources
                    ],
                    "excluded": [
                        source.model_dump(mode="json", by_alias=True)
                        for source in result.excludedSources
                    ],
                }
                connection.execute(
                    """INSERT INTO ai_artifact_team_preflights (
                           preflight_id, artifact_id, team_id, tenant_id,
                           owner_user_id, command_id, artifact_revision,
                           preflight_state, decision_revision, members_envelope,
                           sources_envelope, allowed_source_count,
                           excluded_source_count, evidence_sha256,
                           request_fingerprint, expires_at, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        preflight_id,
                        artifact_id,
                        request.team_id,
                        identity.tenant_id,
                        identity.user_id,
                        request.command_id,
                        request.artifact_revision,
                        state.value,
                        result.decisionRevision,
                        self.codec.encrypt_json(
                            {"members": member_payload},
                            tenant_id=identity.tenant_id,
                            resource_type="artifact-team-preflight",
                            resource_id=str(preflight_id),
                            field="members",
                        ),
                        self.codec.encrypt_json(
                            source_payload,
                            tenant_id=identity.tenant_id,
                            resource_type="artifact-team-preflight",
                            resource_id=str(preflight_id),
                            field="sources",
                        ),
                        len(result.allowedSources),
                        len(result.excludedSources),
                        result.evidenceSha256,
                        proof.request_fingerprint,
                        result.expiresAt,
                        now,
                    ),
                )
                preflight = TeamArtifactPreflight(
                    preflight_id=preflight_id,
                    artifact_id=artifact_id,
                    team_id=request.team_id,
                    artifact_revision=request.artifact_revision,
                    state=state,
                    decision_revision=result.decisionRevision,
                    members=result.members,
                    allowed_source_count=len(result.allowedSources),
                    excluded_source_count=len(result.excludedSources),
                    evidence_sha256=result.evidenceSha256,
                    expires_at=result.expiresAt,
                    created_at=now,
                )
                self._record_command(
                    connection,
                    identity,
                    artifact_id,
                    "PREFLIGHT",
                    request.command_id,
                    proof,
                    preflight,
                )
                self._event(
                    connection,
                    workspace_id=None,
                    artifact_id=artifact_id,
                    identity=identity,
                    command_id=request.command_id,
                    event_type="PREFLIGHT_COMPLETED",
                    previous_state=None,
                    current_state=state.value,
                    revision=request.artifact_revision,
                    request_fingerprint=proof.request_fingerprint,
                )
                return preflight
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration is unavailable."
            ) from error

    def create_workspace(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        request: CreateTeamArtifactWorkspaceRequest,
    ) -> TeamArtifactWorkspace:
        proof = self._proof(identity, artifact_id, "CREATE_WORKSPACE", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                replay = self._replay(
                    connection,
                    identity,
                    request.command_id,
                    "CREATE_WORKSPACE",
                    proof,
                )
                if replay is not None:
                    return TeamArtifactWorkspace.model_validate(replay)
                artifact = self._owner_artifact(connection, identity, artifact_id)
                if int(artifact["revision"]) != request.expected_revision:
                    raise GovernedDomainConflict("The artifact revision has changed.")
                if artifact["artifact_state"] == "ARCHIVED":
                    raise GovernedDomainConflict(
                        "An archived artifact cannot become a team workspace."
                    )
                existing = connection.execute(
                    "SELECT workspace_id FROM ai_artifact_team_workspaces WHERE artifact_id = %s",
                    (artifact_id,),
                ).fetchone()
                if existing is not None:
                    raise GovernedDomainConflict(
                        "The artifact already has a team workspace."
                    )
                preflight = self._usable_preflight(
                    connection,
                    identity,
                    artifact_id,
                    request.preflight_id,
                    request.team_id,
                    request.expected_revision,
                )
                content = self._artifact_content(artifact)
                workspace_id = uuid4()
                content_fingerprint = content_sha256(content)
                content_envelope = self._encrypt_content(
                    identity.tenant_id, workspace_id, content
                )
                connection.execute(
                    """INSERT INTO ai_artifact_team_workspaces (
                           workspace_id, artifact_id, team_id, tenant_id,
                           owner_user_id, source_artifact_revision,
                           content_envelope, content_sha256)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        workspace_id,
                        artifact_id,
                        request.team_id,
                        identity.tenant_id,
                        identity.user_id,
                        artifact["revision"],
                        content_envelope,
                        content_fingerprint,
                    ),
                )
                self._insert_members(
                    connection,
                    workspace_id,
                    identity,
                    preflight,
                )
                self._version(
                    connection,
                    workspace_id,
                    identity.tenant_id,
                    identity.user_id,
                    1,
                    content_envelope,
                    content_fingerprint,
                )
                workspace = self._workspace_by_id(
                    connection, identity, workspace_id, require_edit=False
                )
                self._record_command(
                    connection,
                    identity,
                    artifact_id,
                    "CREATE_WORKSPACE",
                    request.command_id,
                    proof,
                    workspace,
                )
                self._event(
                    connection,
                    workspace_id=workspace_id,
                    artifact_id=artifact_id,
                    identity=identity,
                    command_id=request.command_id,
                    event_type="WORKSPACE_CREATED",
                    previous_state=None,
                    current_state="ACTIVE",
                    revision=1,
                    request_fingerprint=proof.request_fingerprint,
                )
                return workspace
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration is unavailable."
            ) from error

    def get_workspace(
        self, identity: PersonalDomainIdentity, artifact_id: UUID
    ) -> TeamArtifactWorkspace:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    """SELECT workspace_id FROM ai_artifact_team_workspaces
                        WHERE artifact_id = %s AND tenant_id = %s""",
                    (artifact_id, identity.tenant_id),
                ).fetchone()
                if row is None:
                    raise GovernedDomainNotFound(
                        "The team artifact workspace is unavailable."
                    )
                return self._workspace_by_id(
                    connection, identity, row["workspace_id"], require_edit=False
                )
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration is unavailable."
            ) from error

    def update_members(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        request: UpdateTeamArtifactMembersRequest,
    ) -> TeamArtifactWorkspace:
        proof = self._proof(identity, artifact_id, "UPDATE_MEMBERS", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                replay = self._replay(
                    connection, identity, request.command_id, "UPDATE_MEMBERS", proof
                )
                if replay is not None:
                    return TeamArtifactWorkspace.model_validate(replay)
                workspace_row = self._locked_workspace(
                    connection, identity, artifact_id, owner_only=True
                )
                if int(workspace_row["revision"]) != request.expected_revision:
                    raise GovernedDomainConflict("The team workspace revision has changed.")
                preflight = self._usable_preflight(
                    connection,
                    identity,
                    artifact_id,
                    request.preflight_id,
                    workspace_row["team_id"],
                    workspace_row["source_artifact_revision"],
                )
                connection.execute(
                    "DELETE FROM ai_artifact_team_members WHERE workspace_id = %s",
                    (workspace_row["workspace_id"],),
                )
                self._insert_members(
                    connection, workspace_row["workspace_id"], identity, preflight
                )
                revision = int(workspace_row["revision"]) + 1
                connection.execute(
                    """UPDATE ai_artifact_team_workspaces
                          SET revision = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE workspace_id = %s""",
                    (revision, workspace_row["workspace_id"]),
                )
                workspace = self._workspace_by_id(
                    connection,
                    identity,
                    workspace_row["workspace_id"],
                    require_edit=False,
                )
                self._record_command(
                    connection,
                    identity,
                    artifact_id,
                    "UPDATE_MEMBERS",
                    request.command_id,
                    proof,
                    workspace,
                )
                self._event(
                    connection,
                    workspace_id=workspace_row["workspace_id"],
                    artifact_id=artifact_id,
                    identity=identity,
                    command_id=request.command_id,
                    event_type="MEMBERS_UPDATED",
                    previous_state="ACTIVE",
                    current_state="ACTIVE",
                    revision=revision,
                    request_fingerprint=proof.request_fingerprint,
                )
                return workspace
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration is unavailable."
            ) from error
