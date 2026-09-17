from __future__ import annotations

import os
from functools import lru_cache

from .artifact_collaboration_access_store import ArtifactCollaborationAccess
from .artifact_collaboration_content_store import ArtifactCollaborationContent
from .artifact_collaboration_edit_store import ArtifactCollaborationEditCommands
from .artifact_collaboration_preflight_store import ArtifactCollaborationPreflightCommands
from .artifact_collaboration_provider import ArtifactCollaborationProvider
from .artifact_collaboration_share_store import ArtifactCollaborationShareCommands
from .governed_domain_core import (
    GovernedDomainUnavailable,
    GovernedFingerprints,
    GovernedPayloadCodec,
)


class PostgresArtifactCollaborationStore(
    ArtifactCollaborationPreflightCommands,
    ArtifactCollaborationEditCommands,
    ArtifactCollaborationShareCommands,
    ArtifactCollaborationAccess,
    ArtifactCollaborationContent,
):
    def __init__(
        self,
        database_url: str,
        *,
        provider: ArtifactCollaborationProvider | None = None,
    ) -> None:
        self.database_url = database_url
        self.provider = provider or ArtifactCollaborationProvider()
        try:
            self.codec = GovernedPayloadCodec()
            self.fingerprints = GovernedFingerprints.load()
        except Exception as error:
            raise GovernedDomainUnavailable(
                "Artifact collaboration encryption is unavailable."
            ) from error

    def capabilities(self):
        return self.provider.capabilities()


@lru_cache(maxsize=1)
def get_artifact_collaboration_store() -> PostgresArtifactCollaborationStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise GovernedDomainUnavailable(
            "Artifact collaboration database is unavailable."
        )
    return PostgresArtifactCollaborationStore(database_url)
