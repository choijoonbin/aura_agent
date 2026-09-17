from __future__ import annotations

import hashlib
from uuid import uuid4

from .artifact_collaboration_contracts import (
    TeamArtifactConflict,
    TeamArtifactShare,
)
from .artifact_contracts import ArtifactDraftContent
from .canonical_json import canonical_json_bytes


class ArtifactCollaborationContent:
    def _create_conflict(self, connection, identity, workspace, request):
        conflict_id = uuid4()
        local_envelope = self.codec.encrypt_json(
            request.content.model_dump(mode="json", by_alias=True),
            tenant_id=identity.tenant_id,
            resource_type="artifact-team-conflict",
            resource_id=str(conflict_id),
            field="local-content",
        )
        server_content = self._workspace_content(workspace)
        server_envelope = self.codec.encrypt_json(
            server_content.model_dump(mode="json", by_alias=True),
            tenant_id=identity.tenant_id,
            resource_type="artifact-team-conflict",
            resource_id=str(conflict_id),
            field="server-content",
        )
        connection.execute(
            """INSERT INTO ai_artifact_team_conflicts (
                   conflict_id, workspace_id, tenant_id, actor_user_id,
                   command_id, base_revision, server_revision,
                   local_content_envelope, local_sha256,
                   server_content_envelope, server_sha256)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                conflict_id,
                workspace["workspace_id"],
                identity.tenant_id,
                identity.user_id,
                request.command_id,
                request.base_revision,
                workspace["revision"],
                local_envelope,
                content_sha256(request.content),
                server_envelope,
                workspace["content_sha256"],
            ),
        )
        return self._conflict(
            connection.execute(
                "SELECT * FROM ai_artifact_team_conflicts WHERE conflict_id = %s",
                (conflict_id,),
            ).fetchone()
        )

    def _conflict(self, row):
        return TeamArtifactConflict(
            conflict_id=row["conflict_id"],
            workspace_id=row["workspace_id"],
            base_revision=row["base_revision"],
            server_revision=row["server_revision"],
            state=row["conflict_state"],
            local_content=self._decrypt_conflict_content(row, "local"),
            server_content=self._decrypt_conflict_content(row, "server"),
            local_sha256=row["local_sha256"],
            server_sha256=row["server_sha256"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
        )

    def _decrypt_conflict_content(self, row, side):
        payload = self.codec.decrypt_json(
            row[f"{side}_content_envelope"],
            tenant_id=int(row["tenant_id"]),
            resource_type="artifact-team-conflict",
            resource_id=str(row["conflict_id"]),
            field=f"{side}-content",
        )
        return ArtifactDraftContent.model_validate(payload)

    def _artifact_content(self, row):
        return ArtifactDraftContent.model_validate(
            self.codec.decrypt_json(
                row["content_envelope"],
                tenant_id=int(row["tenant_id"]),
                resource_type="artifact-draft",
                resource_id=str(row["artifact_id"]),
                field="content",
            )
        )

    def _workspace_content(self, row):
        return ArtifactDraftContent.model_validate(
            self.codec.decrypt_json(
                row["content_envelope"],
                tenant_id=int(row["tenant_id"]),
                resource_type="artifact-team-workspace",
                resource_id=str(row["workspace_id"]),
                field="content",
            )
        )

    def _encrypt_content(self, tenant_id, workspace_id, content):
        return self.codec.encrypt_json(
            content.model_dump(mode="json", by_alias=True),
            tenant_id=tenant_id,
            resource_type="artifact-team-workspace",
            resource_id=str(workspace_id),
            field="content",
        )

    @staticmethod
    def _version(
        connection,
        workspace_id,
        tenant_id,
        actor_user_id,
        revision,
        envelope,
        fingerprint,
    ):
        connection.execute(
            """INSERT INTO ai_artifact_team_versions (
                   workspace_id, revision, tenant_id, actor_user_id,
                   content_envelope, content_sha256)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                workspace_id,
                revision,
                tenant_id,
                actor_user_id,
                envelope,
                fingerprint,
            ),
        )

    @staticmethod
    def _share(row, now):
        state = (
            "EXPIRED"
            if row["share_state"] == "ACTIVE" and row["expires_at"] <= now
            else row["share_state"]
        )
        return TeamArtifactShare(
            share_id=row["share_id"],
            workspace_id=row["workspace_id"],
            state=state,
            permission=row["permission"],
            member_count=row["member_count"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            receipt_id=row["receipt_id"],
            receipt_sha256=row["receipt_sha256"],
            revocation_receipt_id=row["revocation_receipt_id"],
            revocation_receipt_sha256=row["revocation_receipt_sha256"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _event(
        connection,
        *,
        workspace_id,
        artifact_id,
        identity,
        command_id,
        event_type,
        previous_state,
        current_state,
        revision,
        request_fingerprint,
    ):
        connection.execute(
            """INSERT INTO ai_artifact_collaboration_events (
                   event_id, workspace_id, artifact_id, tenant_id,
                   actor_user_id, command_id, event_type, previous_state,
                   current_state, revision, request_fingerprint)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(),
                workspace_id,
                artifact_id,
                identity.tenant_id,
                identity.user_id,
                command_id,
                event_type,
                previous_state,
                current_state,
                revision,
                request_fingerprint,
            ),
        )

def content_sha256(content: ArtifactDraftContent) -> str:
    return hashlib.sha256(
        canonical_json_bytes(content.model_dump(mode="json", by_alias=True))
    ).hexdigest()
