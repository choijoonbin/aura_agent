from __future__ import annotations

import json
from typing import Any


GROUNDED_ANSWER_STATUS = "ANSWER_GROUNDED"
GROUNDED_FALLBACK_PROVIDER = "DWP_GROUNDED_FALLBACK"
GROUNDED_FALLBACK_STATUS = "ANSWER_GROUNDED_FALLBACK"


def grounded_status_for_provider(provider: str | None) -> str:
    if provider == GROUNDED_FALLBACK_PROVIDER:
        return GROUNDED_FALLBACK_STATUS
    return GROUNDED_ANSWER_STATUS


def normalize_legacy_grounded_status(provider: str | None, status_code: str | None) -> str | None:
    if provider == GROUNDED_FALLBACK_PROVIDER and status_code == GROUNDED_ANSWER_STATUS:
        return GROUNDED_FALLBACK_STATUS
    return status_code


def normalize_legacy_ask_response_payload(payload: bytes) -> bytes:
    decoded: Any = json.loads(payload)
    if not isinstance(decoded, dict):
        return payload
    model_route = decoded.get("modelRoute", decoded.get("model_route"))
    if not isinstance(model_route, dict):
        return payload
    status_key = "statusCode" if "statusCode" in decoded else "status_code"
    normalized = normalize_legacy_grounded_status(
        model_route.get("provider"),
        decoded.get(status_key),
    )
    if normalized == decoded.get(status_key):
        return payload
    return json.dumps({**decoded, status_key: normalized}).encode("utf-8")
