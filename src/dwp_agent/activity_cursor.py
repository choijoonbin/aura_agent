from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from uuid import UUID

from .activity_store import ActivityFilters, ActivityStoreUnavailable


class InvalidActivityCursor(ValueError):
    pass


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _key() -> bytes:
    secret = os.getenv("DWP_AGENT_IDENTITY_SIGNING_SECRET", "").strip() or os.getenv(
        "DWP_AGENT_SERVICE_TOKEN", "").strip()
    if not secret:
        raise ActivityStoreUnavailable("Agent activity cursor signing is unavailable.")
    return hmac.digest(secret.encode(), b"dwp-agent-activity-cursor-v1", "sha256")


def _binding(tenant_id: str, user_id: str, filters: ActivityFilters) -> str:
    value = json.dumps([tenant_id, user_id, asdict(filters)], sort_keys=True,
                       separators=(",", ":"), default=str).encode()
    return hashlib.sha256(value).hexdigest()


def encode_cursor(*, tenant_id: str, user_id: str, filters: ActivityFilters,
                  snapshot_at: datetime, after: tuple[datetime, UUID] | None) -> str:
    payload = {"v": 1, "b": _binding(tenant_id, user_id, filters),
               "s": snapshot_at.isoformat(),
               "a": [after[0].isoformat(), str(after[1])] if after else None}
    encoded = _encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"{encoded}.{_encode(hmac.digest(_key(), encoded.encode(), 'sha256'))}"


def decode_cursor(cursor: str, *, tenant_id: str, user_id: str,
                  filters: ActivityFilters, now: datetime) -> tuple[datetime, tuple[datetime, UUID] | None]:
    try:
        if len(cursor) > 2048:
            raise ValueError("Too long")
        encoded, signature = cursor.split(".")
        if not hmac.compare_digest(hmac.digest(_key(), encoded.encode(), "sha256"), _decode(signature)):
            raise ValueError("Signature mismatch")
        payload = json.loads(_decode(encoded))
        if set(payload) != {"v", "b", "s", "a"} or payload["v"] != 1:
            raise ValueError("Invalid shape")
        if not hmac.compare_digest(payload["b"], _binding(tenant_id, user_id, filters)):
            raise ValueError("Invalid scope")
        snapshot_at = datetime.fromisoformat(payload["s"])
        if snapshot_at.tzinfo is None or snapshot_at > now + timedelta(seconds=5) or now - snapshot_at > timedelta(hours=1):
            raise ValueError("Expired cursor")
        after = None
        if payload["a"] is not None:
            if len(payload["a"]) != 2:
                raise ValueError("Invalid position")
            after = (datetime.fromisoformat(payload["a"][0]), UUID(payload["a"][1]))
            if after[0].tzinfo is None or after[0] > snapshot_at:
                raise ValueError("Invalid position")
        return snapshot_at.astimezone(timezone.utc), after
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise InvalidActivityCursor("Activity cursor is invalid, expired or outside this scope.") from error
