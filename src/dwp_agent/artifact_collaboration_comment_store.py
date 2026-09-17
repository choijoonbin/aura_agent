from __future__ import annotations

from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .artifact_collaboration_contracts import (
    CreateTeamArtifactCommentRequest,
    ReplyTeamArtifactCommentRequest,
    ResolveTeamArtifactCommentRequest,
    TeamArtifactComment,
)
from .artifact_collaboration_comment_content import (
    ArtifactCollaborationCommentContent,
)
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
)
from .personal_domain_security import PersonalDomainIdentity


class ArtifactCollaborationCommentCommands(ArtifactCollaborationCommentContent):
    def list_comments(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
    ) -> list[TeamArtifactComment]:
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                workspace = self._locked_workspace(
                    connection,
                    identity,
                    artifact_id,
                    owner_only=False,
                )
                rows = connection.execute(
                    """SELECT * FROM ai_artifact_team_comments
                        WHERE workspace_id = %s AND artifact_id = %s
                          AND tenant_id = %s
                        ORDER BY created_at ASC
                        LIMIT 200""",
                    (
                        workspace["workspace_id"],
                        artifact_id,
                        identity.tenant_id,
                    ),
                ).fetchall()
                return [self._comment(connection, row) for row in rows]
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration comments are unavailable."
            ) from error

    def create_comment(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        request: CreateTeamArtifactCommentRequest,
    ) -> TeamArtifactComment:
        proof = self._proof(identity, artifact_id, "CREATE_COMMENT", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                replay = self._replay(
                    connection,
                    identity,
                    request.command_id,
                    "CREATE_COMMENT",
                    proof,
                )
                if replay is not None:
                    return TeamArtifactComment.model_validate(replay)
                workspace = self._locked_workspace(
                    connection,
                    identity,
                    artifact_id,
                    owner_only=False,
                )
                self._require_comment_permission(connection, identity, workspace)
                if workspace["workspace_state"] != "ACTIVE":
                    raise GovernedDomainConflict(
                        "The team artifact workspace is read-only."
                    )
                if int(workspace["revision"]) != request.expected_revision:
                    raise GovernedDomainConflict(
                        "The team workspace revision has changed."
                    )
                comment_id = uuid4()
                body_envelope = self.codec.encrypt_json(
                    {"body": request.body},
                    tenant_id=identity.tenant_id,
                    resource_type="artifact-team-comment",
                    resource_id=str(comment_id),
                    field="body",
                )
                anchor_envelope = (
                    self.codec.encrypt_json(
                        {"anchor": request.anchor},
                        tenant_id=identity.tenant_id,
                        resource_type="artifact-team-comment",
                        resource_id=str(comment_id),
                        field="anchor",
                    )
                    if request.anchor is not None
                    else None
                )
                connection.execute(
                    """INSERT INTO ai_artifact_team_comments (
                           comment_id, workspace_id, artifact_id, tenant_id,
                           author_user_id, command_id, body_envelope,
                           anchor_envelope)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        comment_id,
                        workspace["workspace_id"],
                        artifact_id,
                        identity.tenant_id,
                        identity.user_id,
                        request.command_id,
                        body_envelope,
                        anchor_envelope,
                    ),
                )
                comment = self._comment_by_id(
                    connection,
                    identity,
                    workspace,
                    artifact_id,
                    comment_id,
                    for_update=False,
                )
                self._record_command(
                    connection,
                    identity,
                    artifact_id,
                    "CREATE_COMMENT",
                    request.command_id,
                    proof,
                    comment,
                )
                self._event(
                    connection,
                    workspace_id=workspace["workspace_id"],
                    artifact_id=artifact_id,
                    identity=identity,
                    command_id=request.command_id,
                    event_type="COMMENT_CREATED",
                    previous_state=None,
                    current_state="OPEN",
                    revision=comment.revision,
                    request_fingerprint=proof.request_fingerprint,
                )
                return comment
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration comments are unavailable."
            ) from error

    def reply_comment(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        comment_id: UUID,
        request: ReplyTeamArtifactCommentRequest,
    ) -> TeamArtifactComment:
        proof = self._proof(identity, artifact_id, "REPLY_COMMENT", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                replay = self._replay(
                    connection,
                    identity,
                    request.command_id,
                    "REPLY_COMMENT",
                    proof,
                )
                if replay is not None:
                    return TeamArtifactComment.model_validate(replay)
                workspace = self._locked_workspace(
                    connection,
                    identity,
                    artifact_id,
                    owner_only=False,
                )
                self._require_comment_permission(connection, identity, workspace)
                if workspace["workspace_state"] != "ACTIVE":
                    raise GovernedDomainConflict(
                        "The team artifact workspace is read-only."
                    )
                comment_row = self._comment_row(
                    connection,
                    identity,
                    workspace,
                    artifact_id,
                    comment_id,
                    for_update=True,
                )
                if comment_row["comment_state"] != "OPEN":
                    raise GovernedDomainConflict(
                        "A resolved artifact comment cannot receive replies."
                    )
                if int(comment_row["revision"]) != request.expected_revision:
                    raise GovernedDomainConflict(
                        "The artifact comment revision has changed."
                    )
                reply_id = uuid4()
                body_envelope = self.codec.encrypt_json(
                    {"body": request.body},
                    tenant_id=identity.tenant_id,
                    resource_type="artifact-team-comment-reply",
                    resource_id=str(reply_id),
                    field="body",
                )
                connection.execute(
                    """INSERT INTO ai_artifact_team_comment_replies (
                           reply_id, comment_id, workspace_id, artifact_id,
                           tenant_id, author_user_id, command_id, body_envelope)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        reply_id,
                        comment_id,
                        workspace["workspace_id"],
                        artifact_id,
                        identity.tenant_id,
                        identity.user_id,
                        request.command_id,
                        body_envelope,
                    ),
                )
                revision = int(comment_row["revision"]) + 1
                connection.execute(
                    """UPDATE ai_artifact_team_comments
                          SET revision = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE comment_id = %s""",
                    (revision, comment_id),
                )
                comment = self._comment_by_id(
                    connection,
                    identity,
                    workspace,
                    artifact_id,
                    comment_id,
                    for_update=False,
                )
                self._record_command(
                    connection,
                    identity,
                    artifact_id,
                    "REPLY_COMMENT",
                    request.command_id,
                    proof,
                    comment,
                )
                self._event(
                    connection,
                    workspace_id=workspace["workspace_id"],
                    artifact_id=artifact_id,
                    identity=identity,
                    command_id=request.command_id,
                    event_type="COMMENT_REPLIED",
                    previous_state="OPEN",
                    current_state="OPEN",
                    revision=revision,
                    request_fingerprint=proof.request_fingerprint,
                )
                return comment
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration comments are unavailable."
            ) from error

    def resolve_comment(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        comment_id: UUID,
        request: ResolveTeamArtifactCommentRequest,
    ) -> TeamArtifactComment:
        proof = self._proof(identity, artifact_id, "RESOLVE_COMMENT", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                replay = self._replay(
                    connection,
                    identity,
                    request.command_id,
                    "RESOLVE_COMMENT",
                    proof,
                )
                if replay is not None:
                    return TeamArtifactComment.model_validate(replay)
                workspace = self._locked_workspace(
                    connection,
                    identity,
                    artifact_id,
                    owner_only=False,
                )
                if workspace["workspace_state"] != "ACTIVE":
                    raise GovernedDomainConflict(
                        "The team artifact workspace is read-only."
                    )
                comment_row = self._comment_row(
                    connection,
                    identity,
                    workspace,
                    artifact_id,
                    comment_id,
                    for_update=True,
                )
                self._require_comment_resolution_permission(
                    connection,
                    identity,
                    workspace,
                    comment_row,
                )
                if comment_row["comment_state"] != "OPEN":
                    raise GovernedDomainConflict(
                        "The artifact comment is already resolved."
                    )
                if int(comment_row["revision"]) != request.expected_revision:
                    raise GovernedDomainConflict(
                        "The artifact comment revision has changed."
                    )
                revision = int(comment_row["revision"]) + 1
                connection.execute(
                    """UPDATE ai_artifact_team_comments
                          SET comment_state = 'RESOLVED', revision = %s,
                              updated_at = CURRENT_TIMESTAMP,
                              resolved_at = CURRENT_TIMESTAMP
                        WHERE comment_id = %s""",
                    (revision, comment_id),
                )
                comment = self._comment_by_id(
                    connection,
                    identity,
                    workspace,
                    artifact_id,
                    comment_id,
                    for_update=False,
                )
                self._record_command(
                    connection,
                    identity,
                    artifact_id,
                    "RESOLVE_COMMENT",
                    request.command_id,
                    proof,
                    comment,
                )
                self._event(
                    connection,
                    workspace_id=workspace["workspace_id"],
                    artifact_id=artifact_id,
                    identity=identity,
                    command_id=request.command_id,
                    event_type="COMMENT_RESOLVED",
                    previous_state="OPEN",
                    current_state="RESOLVED",
                    revision=revision,
                    request_fingerprint=proof.request_fingerprint,
                )
                return comment
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration comments are unavailable."
            ) from error
