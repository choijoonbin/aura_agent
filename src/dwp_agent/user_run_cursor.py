from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

from .user_run_contracts import AgentRunState
from .user_run_store_errors import UserRunStoreUnavailable


class InvalidUserRunCursor(ValueError):
    pass


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _key() -> bytes:
    secret = os.getenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", "").strip() or os.getenv(
        "DWP_AGENT_SERVICE_TOKEN", ""
    ).strip()
    if not secret:
        raise UserRunStoreUnavailable("Agent run cursor signing is unavailable.")
    return hmac.digest(secret.encode(), b"dwp-agent-user-run-cursor-v1", "sha256")


def _binding(
    tenant_id: str,
    user_id: str,
    run_state: AgentRunState | None,
    from_at: datetime | None,
    to_at: datetime | None,
) -> str:
    values = [
        tenant_id,
        user_id,
        run_state.value if run_state else None,
        from_at.isoformat() if from_at else None,
        to_at.isoformat() if to_at else None,
    ]
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":")).encode()
    ).hexdigest()


def encode_user_run_cursor(
    *,
    tenant_id: str,
    user_id: str,
    run_state: AgentRunState | None,
    from_at: datetime | None,
    to_at: datetime | None,
    snapshot_at: datetime,
    after: tuple[datetime, UUID],
) -> str:
    payload = {
        "v": 1,
        "b": _binding(tenant_id, user_id, run_state, from_at, to_at),
        "s": snapshot_at.isoformat(),
        "a": [after[0].isoformat(), str(after[1])],
    }
    encoded = _encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = _encode(hmac.digest(_key(), encoded.encode(), "sha256"))
    return f"{encoded}.{signature}"


def decode_user_run_cursor(
    cursor: str,
    *,
    tenant_id: str,
    user_id: str,
    run_state: AgentRunState | None,
    from_at: datetime | None,
    to_at: datetime | None,
    now: datetime,
) -> tuple[datetime, tuple[datetime, UUID]]:
    try:
        if len(cursor) > 2048:
            raise ValueError("Too long")
        encoded, signature = cursor.split(".")
        if not hmac.compare_digest(
            hmac.digest(_key(), encoded.encode(), "sha256"), _decode(signature)
        ):
            raise ValueError("Signature mismatch")
        payload = json.loads(_decode(encoded))
        if set(payload) != {"v", "b", "s", "a"} or payload["v"] != 1:
            raise ValueError("Invalid shape")
        expected = _binding(tenant_id, user_id, run_state, from_at, to_at)
        if not hmac.compare_digest(payload["b"], expected):
            raise ValueError("Invalid scope")
        snapshot_at = datetime.fromisoformat(payload["s"])
        if (
            snapshot_at.tzinfo is None
            or snapshot_at > now + timedelta(seconds=5)
            or now - snapshot_at > timedelta(hours=1)
        ):
            raise ValueError("Expired cursor")
        if not isinstance(payload["a"], list) or len(payload["a"]) != 2:
            raise ValueError("Invalid position")
        after = (datetime.fromisoformat(payload["a"][0]), UUID(payload["a"][1]))
        if after[0].tzinfo is None or after[0] > snapshot_at:
            raise ValueError("Invalid position")
        return snapshot_at.astimezone(timezone.utc), (
            after[0].astimezone(timezone.utc),
            after[1],
        )
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise InvalidUserRunCursor(
            "Agent run cursor is invalid, expired or outside this scope."
        ) from error
