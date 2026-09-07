from __future__ import annotations

import os
from functools import lru_cache

from .governed_domain_core import GovernedDomainUnavailable
from .personal_routine_postgres_store import PostgresPersonalRoutineStore


@lru_cache(maxsize=1)
def get_personal_routine_store() -> PostgresPersonalRoutineStore:
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise GovernedDomainUnavailable("Personal routine database is unavailable.")
    return PostgresPersonalRoutineStore(database_url)
