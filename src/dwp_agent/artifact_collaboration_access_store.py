from __future__ import annotations

from datetime import UTC, datetime

from .artifact_collaboration_contracts import (
    TeamArtifactMember,
    TeamArtifactPreflight,
    TeamArtifactWorkspace,
)
from .artifact_contracts import ArtifactSourceReference
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    advisory_lock,
    require_command_replay,
)


class ArtifactCollaborationAccess:
    def _proof(self, identity, artifact_id, command_type, request):
        return self.fingerprints.command(
            tenant_id=identity.tenant_id,
            session_id=identity.auth_session_id,
            purpose=f"artifact-collaboration:{command_type.lower()}",
            payload={
                "artifactId": str(artifact_id),
                "request": request.model_dump(mode="json", by_alias=True),
            },
        )

    def _replay(self, connection, identity, command_id, command_type, proof):
        advisory_lock(
            connection,
            "artifact-collaboration-command",
            identity.tenant_id,
            identity.user_id,
            command_id,
        )
        row = connection.execute(
            """SELECT command_type, session_fingerprint, request_fingerprint,
                      result_envelope
                 FROM ai_artifact_collaboration_commands
                WHERE tenant_id = %s AND user_id = %s AND command_id = %s""",
            (identity.tenant_id, identity.user_id, command_id),
        ).fetchone()
        if row is None:
            return None
        if row["command_type"] != command_type:
            raise GovernedDomainConflict("The command ID is already in use.")
        require_command_replay(
            row["session_fingerprint"], row["request_fingerprint"], proof
        )
        return self.codec.decrypt_json(
            row["result_envelope"],
            tenant_id=identity.tenant_id,
            resource_type="artifact-collaboration-command",
            resource_id=str(command_id),
            field="result",
        )

    def _record_command(
        self,
        connection,
        identity,
        artifact_id,
        command_type,
        command_id,
        proof,
        result,
    ):
        envelope = self.codec.encrypt_json(
            result.model_dump(mode="json", by_alias=True),
            tenant_id=identity.tenant_id,
            resource_type="artifact-collaboration-command",
            resource_id=str(command_id),
            field="result",
        )
        connection.execute(
            """INSERT INTO ai_artifact_collaboration_commands (
                   tenant_id, user_id, command_id, artifact_id, command_type,
                   session_fingerprint, request_fingerprint, result_envelope)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                identity.tenant_id,
                identity.user_id,
                command_id,
                artifact_id,
                command_type,
                proof.session_fingerprint,
                proof.request_fingerprint,
                envelope,
            ),
        )

    def _owner_artifact(self, connection, identity, artifact_id):
        row = connection.execute(
            """SELECT a.artifact_id, a.tenant_id, a.user_id, a.artifact_state,
                      a.revision, d.content_envelope
                 FROM ai_artifacts a
                 JOIN ai_artifact_drafts d ON d.artifact_id = a.artifact_id
                WHERE a.artifact_id = %s AND a.tenant_id = %s AND a.user_id = %s
                FOR UPDATE""",
            (artifact_id, identity.tenant_id, identity.user_id),
        ).fetchone()
        if row is None:
            raise GovernedDomainNotFound("The governed artifact is unavailable.")
        return row

    def _selected_sources(self, connection, identity, artifact_id, request):
        rows = connection.execute(
            """SELECT source_type, reference_envelope, source_link_id
                 FROM ai_artifact_draft_sources WHERE artifact_id = %s""",
            (artifact_id,),
        ).fetchall()
        sources = [
            ArtifactSourceReference(
                source_type=row["source_type"],
                reference=self.codec.decrypt_json(
                    row["reference_envelope"],
                    tenant_id=identity.tenant_id,
                    resource_type="artifact-draft-source",
                    resource_id=str(row["source_link_id"]),
                    field="reference",
                )["reference"],
            )
            for row in rows
        ]
        if not request.sources:
            return sources
        server = {(source.source_type, source.reference): source for source in sources}
        selected = []
        for requested in request.sources:
            match = server.get((requested.source_type, requested.reference))
            if match is None:
                raise GovernedDomainConflict(
                    "The requested artifact source is not server verified."
                )
            selected.append(match)
        return selected

    def _usable_preflight(
        self,
        connection,
        identity,
        artifact_id,
        preflight_id,
        team_id,
        artifact_revision,
        *,
        allow_team_member=False,
    ):
        row = connection.execute(
            """SELECT * FROM ai_artifact_team_preflights
                WHERE preflight_id = %s AND artifact_id = %s AND tenant_id = %s
                  AND team_id = %s AND artifact_revision = %s
                  AND expires_at > CURRENT_TIMESTAMP""",
            (
                preflight_id,
                artifact_id,
                identity.tenant_id,
                team_id,
                artifact_revision,
            ),
        ).fetchone()
        if row is None or row["preflight_state"] not in {"READY", "PARTIAL"}:
            raise GovernedDomainConflict(
                "A current successful team ACL preflight is required."
            )
        if not allow_team_member and row["owner_user_id"] != identity.user_id:
            raise GovernedDomainNotFound("The team ACL preflight is unavailable.")
        return self._preflight(row)

    def _preflight(self, row):
        members = self.codec.decrypt_json(
            row["members_envelope"],
            tenant_id=int(row["tenant_id"]),
            resource_type="artifact-team-preflight",
            resource_id=str(row["preflight_id"]),
            field="members",
        )["members"]
        state = (
            "EXPIRED"
            if row["expires_at"] <= datetime.now(UTC)
            else row["preflight_state"]
        )
        return TeamArtifactPreflight(
            preflight_id=row["preflight_id"],
            artifact_id=row["artifact_id"],
            team_id=row["team_id"],
            artifact_revision=row["artifact_revision"],
            state=state,
            decision_revision=row["decision_revision"],
            members=[TeamArtifactMember.model_validate(member) for member in members],
            allowed_source_count=row["allowed_source_count"],
            excluded_source_count=row["excluded_source_count"],
            evidence_sha256=row["evidence_sha256"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
        )

    def _insert_members(self, connection, workspace_id, identity, preflight):
        owner_present = False
        for member in preflight.members:
            if not member.allowed:
                continue
            role = member.role.value
            if member.subject_id == identity.user_id:
                role = "OWNER"
                owner_present = True
            connection.execute(
                """INSERT INTO ai_artifact_team_members (
                       workspace_id, tenant_id, subject_id, member_role,
                       allowed, denied_source_count, reason_code, decision_revision)
                   VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s)""",
                (
                    workspace_id,
                    identity.tenant_id,
                    member.subject_id,
                    role,
                    member.denied_source_count,
                    member.reason_code,
                    preflight.decision_revision,
                ),
            )
        if not owner_present:
            connection.execute(
                """INSERT INTO ai_artifact_team_members (
                       workspace_id, tenant_id, subject_id, member_role,
                       allowed, denied_source_count, decision_revision)
                   VALUES (%s, %s, %s, 'OWNER', TRUE, 0, %s)""",
                (
                    workspace_id,
                    identity.tenant_id,
                    identity.user_id,
                    preflight.decision_revision,
                ),
            )

    def _locked_workspace(
        self,
        connection,
        identity,
        artifact_id,
        *,
        owner_only,
        require_edit=False,
    ):
        row = connection.execute(
            WORKSPACE_SELECT
            + " WHERE w.artifact_id = %s AND w.tenant_id = %s FOR UPDATE",
            (artifact_id, identity.tenant_id),
        ).fetchone()
        if row is None:
            raise GovernedDomainNotFound("The team artifact workspace is unavailable.")
        if owner_only and row["owner_user_id"] != identity.user_id:
            raise GovernedDomainNotFound("The team artifact workspace is unavailable.")
        if row["owner_user_id"] != identity.user_id:
            member = connection.execute(
                """SELECT member_role, allowed FROM ai_artifact_team_members
                    WHERE workspace_id = %s AND tenant_id = %s AND subject_id = %s""",
                (row["workspace_id"], identity.tenant_id, identity.user_id),
            ).fetchone()
            if member is None or not member["allowed"]:
                raise GovernedDomainNotFound(
                    "The team artifact workspace is unavailable."
                )
            if require_edit and member["member_role"] not in {"OWNER", "EDITOR"}:
                raise GovernedDomainConflict(
                    "The team artifact role cannot edit this workspace."
                )
        return row

    def _workspace_by_id(self, connection, identity, workspace_id, *, require_edit):
        row = connection.execute(
            WORKSPACE_SELECT + " WHERE w.workspace_id = %s AND w.tenant_id = %s",
            (workspace_id, identity.tenant_id),
        ).fetchone()
        if row is None:
            raise GovernedDomainNotFound("The team artifact workspace is unavailable.")
        self._locked_workspace(
            connection,
            identity,
            row["artifact_id"],
            owner_only=False,
            require_edit=require_edit,
        )
        return self._workspace_from_row(connection, identity, row)

    def _workspace_from_row(self, connection, identity, row):
        members = [
            TeamArtifactMember(
                subject_id=member["subject_id"],
                role=member["member_role"],
                allowed=member["allowed"],
                denied_source_count=member["denied_source_count"],
                reason_code=member["reason_code"],
            )
            for member in connection.execute(
                """SELECT subject_id, member_role, allowed,
                          denied_source_count, reason_code
                     FROM ai_artifact_team_members
                    WHERE workspace_id = %s ORDER BY member_role, subject_id""",
                (row["workspace_id"],),
            ).fetchall()
        ]
        conflict_row = connection.execute(
            """SELECT * FROM ai_artifact_team_conflicts
                WHERE workspace_id = %s AND conflict_state = 'OPEN'""",
            (row["workspace_id"],),
        ).fetchone()
        now = connection.execute("SELECT CURRENT_TIMESTAMP AS now").fetchone()["now"]
        shares = [
            self._share(share, now)
            for share in connection.execute(
                """SELECT * FROM ai_artifact_team_shares
                    WHERE workspace_id = %s ORDER BY created_at DESC LIMIT 100""",
                (row["workspace_id"],),
            ).fetchall()
        ]
        return TeamArtifactWorkspace(
            workspace_id=row["workspace_id"],
            artifact_id=row["artifact_id"],
            team_id=row["team_id"],
            state=row["workspace_state"],
            revision=row["revision"],
            content=self._workspace_content(row),
            content_sha256=row["content_sha256"],
            members=members,
            open_conflict=(
                self._conflict(conflict_row) if conflict_row is not None else None
            ),
            shares=shares,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

WORKSPACE_SELECT = """SELECT w.workspace_id, w.artifact_id, w.team_id,
       w.tenant_id, w.owner_user_id, w.workspace_state, w.revision,
       w.source_artifact_revision, w.content_envelope, w.content_sha256,
       w.created_at, w.updated_at, w.revoked_at
  FROM ai_artifact_team_workspaces w"""
