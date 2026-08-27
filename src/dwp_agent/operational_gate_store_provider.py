from __future__ import annotations

import os
import threading

from .governance_store import GovernanceStoreUnavailable
from .operational_gate_store import PostgresOperationalGateStore


_STORE: PostgresOperationalGateStore | None = None
_STORE_LOCK = threading.Lock()


def get_operational_gate_store() -> PostgresOperationalGateStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        database_url = os.getenv("DWP_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            raise GovernanceStoreUnavailable(
                "DWAI-ON operational gates require the configured Agent database."
            )
        _STORE = PostgresOperationalGateStore(database_url)
        return _STORE


def reset_operational_gate_store_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None
