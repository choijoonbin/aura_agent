from __future__ import annotations

import os

from .database_migrations import apply_migrations
from .run_store_errors import RunStoreUnavailable


_DATABASE_STATUS = "DISABLED"


def initialize_database() -> None:
    global _DATABASE_STATUS
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    required = os.getenv("DWP_AGENT_DATABASE_REQUIRED", "false").lower() == "true"
    if not database_url:
        _DATABASE_STATUS = "MISSING" if required else "DISABLED"
        if required:
            raise RunStoreUnavailable("Agent database is required but not configured.")
        return
    try:
        apply_migrations(database_url)
        _DATABASE_STATUS = "READY"
    except Exception:
        _DATABASE_STATUS = "FAILED"
        raise


def database_status() -> str:
    return _DATABASE_STATUS


def reset_database_status() -> None:
    global _DATABASE_STATUS
    _DATABASE_STATUS = "DISABLED"
