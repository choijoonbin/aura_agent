from __future__ import annotations

import hashlib
from datetime import timedelta
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .artifact_collaboration_contracts import (
    CreateTeamArtifactShareRequest,
    RevokeTeamArtifactShareRequest,
    TeamArtifactShare,
)
from .canonical_json import canonical_json_bytes
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
)
from .personal_domain_security import PersonalDomainIdentity


class ArtifactCollaborationShareCommands:
    def create_share(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        request: CreateTeamArtifactShareRequest,
    ) -> TeamArtifactShare:
        proof = self._proof(identity, artifact_id, "CREATE_SHARE", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                replay = self._replay(
                    connection, identity, request.command_id, "CREATE_SHARE", proof
                )
                if replay is not None:
                    return TeamArtifactShare.model_validate(replay)
                workspace = self._locked_workspace(
                    connection, identity, artifact_id, owner_only=False, require_edit=True
                )
                if workspace["workspace_state"] != "ACTIVE":
                    raise GovernedDomainConflict(
                        "Only an active team workspace can create shares."
                    )
                if int(workspace["revision"]) != request.expected_revision:
                    raise GovernedDomainConflict("The team workspace revision has changed.")
                now = connection.execute(
                    "SELECT CURRENT_TIMESTAMP AS now"
                ).fetchone()["now"]
                if request.expires_at <= now or request.expires_at > now + timedelta(
                    days=366
                ):
                    raise GovernedDomainConflict(
                        "The share expiry must be within the next year."
                    )
                preflight = self._usable_preflight(
                    connection,
                    identity,
                    artifact_id,
                    request.preflight_id,
                    workspace["team_id"],
                    workspace["source_artifact_revision"],
                    allow_team_member=True,
                )
                allowed_members = [
                    member for member in preflight.members if member.allowed
                ]
                if not allowed_members:
                    raise GovernedDomainConflict(
                        "The share has no authorized recipients."
                    )
                share_id = uuid4()
                receipt_id = uuid4()
                receipt_payload = {
                    "receiptId": str(receipt_id),
                    "shareId": str(share_id),
                    "workspaceId": str(workspace["workspace_id"]),
                    "permission": request.permission.value,
                    "memberCount": len(allowed_members),
                    "expiresAt": request.expires_at.isoformat(),
                    "decisionRevision": preflight.decision_revision,
                    "evidenceSha256": preflight.evidence_sha256,
                }
                receipt_sha256 = hashlib.sha256(
                    canonical_json_bytes(receipt_payload)
                ).hexdigest()
                connection.execute(
                    """INSERT INTO ai_artifact_team_shares (
                           share_id, workspace_id, tenant_id, owner_user_id,
                           command_id, preflight_id, permission, member_count,
                           expires_at, receipt_id, receipt_sha256, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        share_id,
                        workspace["workspace_id"],
                        identity.tenant_id,
                        workspace["owner_user_id"],
                        request.command_id,
                        request.preflight_id,
                        request.permission.value,
                        len(allowed_members),
                        request.expires_at,
                        receipt_id,
                        receipt_sha256,
                        now,
                    ),
                )
                share = self._share(
                    connection.execute(
                        "SELECT * FROM ai_artifact_team_shares WHERE share_id = %s",
                        (share_id,),
                    ).fetchone(),
                    now,
                )
                self._record_command(
                    connection,
                    identity,
                    artifact_id,
                    "CREATE_SHARE",
                    request.command_id,
                    proof,
                    share,
                )
                self._event(
                    connection,
                    workspace_id=workspace["workspace_id"],
                    artifact_id=artifact_id,
                    identity=identity,
                    command_id=request.command_id,
                    event_type="SHARE_CREATED",
                    previous_state=None,
                    current_state="ACTIVE",
                    revision=workspace["revision"],
                    request_fingerprint=proof.request_fingerprint,
                )
                return share
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration is unavailable."
            ) from error

    def revoke_share(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        share_id: UUID,
        request: RevokeTeamArtifactShareRequest,
    ) -> TeamArtifactShare:
        proof = self._proof(identity, artifact_id, "REVOKE_SHARE", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                replay = self._replay(
                    connection, identity, request.command_id, "REVOKE_SHARE", proof
                )
                if replay is not None:
                    return TeamArtifactShare.model_validate(replay)
                workspace = self._locked_workspace(
                    connection, identity, artifact_id, owner_only=True
                )
                if int(workspace["revision"]) != request.expected_revision:
                    raise GovernedDomainConflict("The team workspace revision has changed.")
                row = connection.execute(
                    """SELECT * FROM ai_artifact_team_shares
                        WHERE share_id = %s AND workspace_id = %s FOR UPDATE""",
                    (share_id, workspace["workspace_id"]),
                ).fetchone()
                if row is None:
                    raise GovernedDomainNotFound("The artifact share is unavailable.")
                if row["share_state"] != "ACTIVE":
                    raise GovernedDomainConflict("The artifact share is already revoked.")
                now = connection.execute(
                    "SELECT CURRENT_TIMESTAMP AS now"
                ).fetchone()["now"]
                revocation_receipt_id = uuid4()
                revocation_receipt_sha256 = hashlib.sha256(
                    canonical_json_bytes(
                        {
                            "receiptId": str(revocation_receipt_id),
                            "shareId": str(share_id),
                            "workspaceId": str(workspace["workspace_id"]),
                            "revokedAt": now.isoformat(),
                            "reasonCode": request.reason_code,
                        }
                    )
                ).hexdigest()
                connection.execute(
                    """UPDATE ai_artifact_team_shares
                          SET share_state = 'REVOKED', revoked_at = %s,
                              revocation_receipt_id = %s,
                              revocation_receipt_sha256 = %s
                        WHERE share_id = %s""",
                    (
                        now,
                        revocation_receipt_id,
                        revocation_receipt_sha256,
                        share_id,
                    ),
                )
                revoked = self._share(
                    connection.execute(
                        "SELECT * FROM ai_artifact_team_shares WHERE share_id = %s",
                        (share_id,),
                    ).fetchone(),
                    now,
                )
                self._record_command(
                    connection,
                    identity,
                    artifact_id,
                    "REVOKE_SHARE",
                    request.command_id,
                    proof,
                    revoked,
                )
                self._event(
                    connection,
                    workspace_id=workspace["workspace_id"],
                    artifact_id=artifact_id,
                    identity=identity,
                    command_id=request.command_id,
                    event_type="SHARE_REVOKED",
                    previous_state="ACTIVE",
                    current_state="REVOKED",
                    revision=workspace["revision"],
                    request_fingerprint=proof.request_fingerprint,
                )
                return revoked
        except (GovernedDomainConflict, GovernedDomainNotFound, GovernedDomainUnavailable):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration is unavailable."
            ) from error
