from __future__ import annotations

from .artifact_collaboration_contracts import (
    TeamArtifactComment,
    TeamArtifactCommentReply,
)
from .governed_domain_core import GovernedDomainConflict, GovernedDomainNotFound


class ArtifactCollaborationCommentContent:
    @staticmethod
    def _member_role(connection, identity, workspace) -> str:
        if workspace["owner_user_id"] == identity.user_id:
            return "OWNER"
        row = connection.execute(
            """SELECT member_role, allowed FROM ai_artifact_team_members
                WHERE workspace_id = %s AND tenant_id = %s AND subject_id = %s""",
            (workspace["workspace_id"], identity.tenant_id, identity.user_id),
        ).fetchone()
        if row is None or not row["allowed"]:
            raise GovernedDomainNotFound(
                "The team artifact workspace is unavailable."
            )
        return str(row["member_role"])

    def _require_comment_permission(self, connection, identity, workspace) -> None:
        if self._member_role(connection, identity, workspace) not in {
            "OWNER",
            "EDITOR",
            "REVIEWER",
        }:
            raise GovernedDomainConflict(
                "The team artifact role cannot comment on this workspace."
            )

    def _require_comment_resolution_permission(
        self,
        connection,
        identity,
        workspace,
        comment_row,
    ) -> None:
        role = self._member_role(connection, identity, workspace)
        if role not in {"OWNER", "EDITOR"} and (
            comment_row["author_user_id"] != identity.user_id
        ):
            raise GovernedDomainConflict(
                "The team artifact role cannot resolve this comment."
            )

    @staticmethod
    def _comment_row(
        connection,
        identity,
        workspace,
        artifact_id,
        comment_id,
        *,
        for_update,
    ):
        suffix = " FOR UPDATE" if for_update else ""
        row = connection.execute(
            """SELECT * FROM ai_artifact_team_comments
                WHERE comment_id = %s AND workspace_id = %s
                  AND artifact_id = %s AND tenant_id = %s"""
            + suffix,
            (
                comment_id,
                workspace["workspace_id"],
                artifact_id,
                identity.tenant_id,
            ),
        ).fetchone()
        if row is None:
            raise GovernedDomainNotFound("The artifact comment is unavailable.")
        return row

    def _comment_by_id(
        self,
        connection,
        identity,
        workspace,
        artifact_id,
        comment_id,
        *,
        for_update,
    ) -> TeamArtifactComment:
        return self._comment(
            connection,
            self._comment_row(
                connection,
                identity,
                workspace,
                artifact_id,
                comment_id,
                for_update=for_update,
            ),
        )

    def _comment(self, connection, row) -> TeamArtifactComment:
        body = self.codec.decrypt_json(
            row["body_envelope"],
            tenant_id=int(row["tenant_id"]),
            resource_type="artifact-team-comment",
            resource_id=str(row["comment_id"]),
            field="body",
        )["body"]
        anchor = None
        if row["anchor_envelope"] is not None:
            anchor = self.codec.decrypt_json(
                row["anchor_envelope"],
                tenant_id=int(row["tenant_id"]),
                resource_type="artifact-team-comment",
                resource_id=str(row["comment_id"]),
                field="anchor",
            )["anchor"]
        replies = [
            self._reply(reply)
            for reply in connection.execute(
                """SELECT * FROM ai_artifact_team_comment_replies
                    WHERE comment_id = %s AND workspace_id = %s
                      AND artifact_id = %s AND tenant_id = %s
                    ORDER BY created_at ASC
                    LIMIT 500""",
                (
                    row["comment_id"],
                    row["workspace_id"],
                    row["artifact_id"],
                    row["tenant_id"],
                ),
            ).fetchall()
        ]
        return TeamArtifactComment(
            comment_id=row["comment_id"],
            workspace_id=row["workspace_id"],
            artifact_id=row["artifact_id"],
            author_subject_id=row["author_user_id"],
            author_display_name=None,
            body=body,
            anchor=anchor,
            state=row["comment_state"],
            revision=row["revision"],
            replies=replies,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolved_at=row["resolved_at"],
        )

    def _reply(self, row) -> TeamArtifactCommentReply:
        body = self.codec.decrypt_json(
            row["body_envelope"],
            tenant_id=int(row["tenant_id"]),
            resource_type="artifact-team-comment-reply",
            resource_id=str(row["reply_id"]),
            field="body",
        )["body"]
        return TeamArtifactCommentReply(
            reply_id=row["reply_id"],
            comment_id=row["comment_id"],
            author_subject_id=row["author_user_id"],
            author_display_name=None,
            body=body,
            created_at=row["created_at"],
        )
