from __future__ import annotations

import os
from functools import lru_cache

from .artifact_postgres_store import PostgresArtifactStore
from .governed_domain_core import GovernedDomainUnavailable


@lru_cache(maxsize=1)
def get_artifact_store() -> PostgresArtifactStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise GovernedDomainUnavailable("Governed artifact database is unavailable.")
    try:
        return PostgresArtifactStore(database_url)
    except Exception as error:
        raise GovernedDomainUnavailable("Governed artifact storage is unavailable.") from error
