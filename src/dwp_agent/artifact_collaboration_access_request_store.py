from __future__ import annotations

from uuid import UUID

from psycopg import Error as PsycopgError, connect
from psycopg.rows import dict_row

from .artifact_collaboration_contracts import (
    CreateTeamArtifactAccessRequest,
    TeamArtifactAccessRequest,
    TeamArtifactPreflightState,
)
from .artifact_collaboration_provider import ArtifactCollaborationProviderUnavailable
from .governed_domain_core import (
    GovernedDomainConflict,
    GovernedDomainNotFound,
    GovernedDomainUnavailable,
)
from .personal_domain_security import PersonalDomainIdentity


class ArtifactCollaborationAccessRequestCommands:
    def request_access(
        self,
        identity: PersonalDomainIdentity,
        artifact_id: UUID,
        request: CreateTeamArtifactAccessRequest,
    ) -> TeamArtifactAccessRequest:
        proof = self._proof(identity, artifact_id, "REQUEST_ACCESS", request)
        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                replay = self._replay(
                    connection,
                    identity,
                    request.command_id,
                    "REQUEST_ACCESS",
                    proof,
                )
                if replay is not None:
                    return TeamArtifactAccessRequest.model_validate(replay)

                artifact = self._owner_artifact(connection, identity, artifact_id)
                if int(artifact["revision"]) != request.expected_revision:
                    raise GovernedDomainConflict("The artifact revision has changed.")

                row = connection.execute(
                    """SELECT * FROM ai_artifact_team_preflights
                        WHERE preflight_id = %s AND artifact_id = %s
                          AND tenant_id = %s AND owner_user_id = %s
                          AND artifact_revision = %s
                          AND expires_at > CURRENT_TIMESTAMP
                        FOR UPDATE""",
                    (
                        request.preflight_id,
                        artifact_id,
                        identity.tenant_id,
                        identity.user_id,
                        request.expected_revision,
                    ),
                ).fetchone()
                if row is None:
                    raise GovernedDomainNotFound(
                        "The denied team ACL preflight is unavailable."
                    )
                preflight = self._preflight(row)
                if preflight.state != TeamArtifactPreflightState.PERMISSION_DENIED:
                    raise GovernedDomainConflict(
                        "Access can only be requested for a denied ACL preflight."
                    )
                denied_subject_ids = sorted(
                    member.subject_id for member in preflight.members if not member.allowed
                )
                denied_subject_count = len(denied_subject_ids)
                denied_source_count = preflight.excluded_source_count
                if denied_subject_count + denied_source_count < 1:
                    raise GovernedDomainConflict(
                        "The ACL preflight has no denied access to request."
                    )

                try:
                    provider_result = self.provider.request_access(
                        artifact_id=artifact_id,
                        team_id=preflight.team_id,
                        preflight_id=preflight.preflight_id,
                        tenant_id=identity.tenant_id,
                        owner_user_id=identity.user_id,
                        artifact_revision=request.expected_revision,
                        command_id=request.command_id,
                        denied_subject_ids=denied_subject_ids,
                        denied_source_count=denied_source_count,
                        reason_code=request.reason_code,
                        change_reason=request.change_reason,
                        correlation_id=identity.correlation_id,
                    )
                except ArtifactCollaborationProviderUnavailable as error:
                    raise GovernedDomainUnavailable(str(error)) from error

                now = connection.execute(
                    "SELECT CURRENT_TIMESTAMP AS now"
                ).fetchone()["now"]
                result = TeamArtifactAccessRequest(
                    access_request_id=provider_result.accessRequestId,
                    artifact_id=artifact_id,
                    team_id=preflight.team_id,
                    preflight_id=preflight.preflight_id,
                    state=provider_result.state,
                    denied_subject_count=denied_subject_count,
                    denied_source_count=denied_source_count,
                    submission_evidence_sha256=(
                        provider_result.submissionEvidenceSha256
                    ),
                    created_at=now,
                )
                connection.execute(
                    """INSERT INTO ai_artifact_team_access_requests (
                           access_request_id, artifact_id, team_id, preflight_id,
                           tenant_id, requester_user_id, command_id, request_state,
                           denied_subject_count, denied_source_count,
                           submission_evidence_sha256, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        result.access_request_id,
                        artifact_id,
                        result.team_id,
                        result.preflight_id,
                        identity.tenant_id,
                        identity.user_id,
                        request.command_id,
                        result.state.value,
                        result.denied_subject_count,
                        result.denied_source_count,
                        result.submission_evidence_sha256,
                        result.created_at,
                    ),
                )
                self._record_command(
                    connection,
                    identity,
                    artifact_id,
                    "REQUEST_ACCESS",
                    request.command_id,
                    proof,
                    result,
                )
                self._event(
                    connection,
                    workspace_id=None,
                    artifact_id=artifact_id,
                    identity=identity,
                    command_id=request.command_id,
                    event_type="ACCESS_REQUESTED",
                    previous_state=preflight.state.value,
                    current_state=result.state.value,
                    revision=request.expected_revision,
                    request_fingerprint=proof.request_fingerprint,
                )
                return result
        except (
            GovernedDomainConflict,
            GovernedDomainNotFound,
            GovernedDomainUnavailable,
        ):
            raise
        except (PsycopgError, ValueError, TypeError, KeyError) as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration access requests are unavailable."
            ) from error
