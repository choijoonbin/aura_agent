from __future__ import annotations

import json
from collections.abc import Mapping


def canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    """Serialize a mapping with the stable encoding used by security fingerprints."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
