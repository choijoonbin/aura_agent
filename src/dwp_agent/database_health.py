from __future__ import annotations

import os

from psycopg import connect

from .run_store import database_status


def probe_database_status() -> str:
    startup_status = database_status()
    database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        return startup_status
    if startup_status != "READY":
        return startup_status
    try:
        with connect(
            database_url,
            connect_timeout=1,
            options="-c statement_timeout=1000",
        ) as connection:
            row = connection.execute(
                "SELECT to_regclass('public.ai_operational_gates')"
            ).fetchone()
        return "READY" if row and row[0] else "FAILED"
    except Exception:
        return "FAILED"
