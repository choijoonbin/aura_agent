from __future__ import annotations

import hashlib
import re
from pathlib import Path

from psycopg import connect

from .run_store_errors import RunStoreUnavailable


_NONTRANSACTIONAL_DIRECTIVE = "-- dwp:execute-in-transaction=false"
_NONTRANSACTIONAL_SEPARATOR = "-- dwp:next-statement"


def apply_migrations(database_url: str) -> None:
    migration_dir = Path(__file__).resolve().parent / "migrations"
    migrations = sorted(migration_dir.glob("V*__*.sql"), key=migration_sort_key)
    with connect(database_url, autocommit=True) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS sys_schema_history (
                   version VARCHAR(40) PRIMARY KEY,
                   description VARCHAR(200) NOT NULL,
                   checksum CHAR(64) NOT NULL,
                   installed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
        )
        connection.execute("SELECT pg_advisory_lock(%s)", (1_936_427_101,))
        try:
            applied = dict(connection.execute(
                "SELECT version, checksum FROM sys_schema_history"
            ).fetchall())
            for migration in migrations:
                version, description = migration.stem.split("__", 1)
                sql = migration.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                if version in applied:
                    if applied[version] != checksum:
                        raise RunStoreUnavailable(
                            f"Agent migration checksum mismatch: {version}"
                        )
                    continue
                if sql.lstrip().startswith(_NONTRANSACTIONAL_DIRECTIVE):
                    for statement in _nontransactional_statements(sql):
                        connection.execute(statement)
                    connection.execute(
                        """INSERT INTO sys_schema_history (version, description, checksum)
                           VALUES (%s, %s, %s)""",
                        (version, description.replace("_", " "), checksum),
                    )
                    continue
                with connection.transaction():
                    connection.execute(sql)
                    connection.execute(
                        """INSERT INTO sys_schema_history (version, description, checksum)
                           VALUES (%s, %s, %s)""",
                        (version, description.replace("_", " "), checksum),
                    )
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (1_936_427_101,))


def _nontransactional_statements(sql: str) -> list[str]:
    statements = [part.strip() for part in sql.split(_NONTRANSACTIONAL_SEPARATOR)]
    if not statements or any(not statement for statement in statements):
        raise RunStoreUnavailable("Invalid non-transactional Agent migration statements.")
    return statements


def migration_sort_key(path: Path) -> int:
    match = re.fullmatch(r"V(\d+)__.+", path.stem)
    if match is None:
        raise RunStoreUnavailable(f"Invalid Agent migration name: {path.name}")
    return int(match.group(1))
