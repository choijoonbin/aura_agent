from __future__ import annotations

import os
from functools import lru_cache

from .governed_domain_core import GovernedDomainUnavailable
from .personal_memory_postgres_store import PostgresPersonalMemoryStore


@lru_cache(maxsize=1)
def get_personal_memory_store() -> PostgresPersonalMemoryStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise GovernedDomainUnavailable("Personal memory database is unavailable.")
    return PostgresPersonalMemoryStore(database_url)
