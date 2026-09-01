from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Any

from .canonical_json import canonical_json_bytes
from .key_provider import load_versioned_key_material, normalized_environment


_PROFILE = "dwp-agent-proactive-analysis-v1"


@dataclass(frozen=True)
class ProposalAnalysisFingerprints:
    key_materials: tuple[bytes, ...]

    def __post_init__(self) -> None:
        if not self.key_materials or any(len(key) != 32 for key in self.key_materials):
            raise ValueError("Analysis fingerprint keys must contain 32 bytes.")

    @classmethod
    def load(cls) -> "ProposalAnalysisFingerprints":
        material = load_versioned_key_material()
        encoded = [material.active_key]
        encoded.extend(value for _, value in sorted(material.previous_keys.items()))
        return cls(tuple(base64.b64decode(value, validate=True) for value in encoded))

    @classmethod
    def ephemeral(cls) -> "ProposalAnalysisFingerprints":
        return cls((os.urandom(32),))

    def source_revision(self, tenant_id: str | int, payload: dict[str, Any]) -> str:
        return self._digest(
            tenant_id, "source-revision", canonical_json_bytes(payload), 0
        )

    def session(self, tenant_id: str | int, session_id: str) -> str:
        return self._digest(tenant_id, "auth-session", session_id.encode("utf-8"), 0)

    def matches_session(
        self, stored: str, tenant_id: str | int, session_id: str
    ) -> bool:
        payload = session_id.encode("utf-8")
        return any(
            hmac.compare_digest(
                stored, self._digest(tenant_id, "auth-session", payload, index)
            )
            for index in range(len(self.key_materials))
        )

    def analysis_command(self, tenant_id: str | int, *, locale: str) -> str:
        return self._digest(
            tenant_id,
            "analysis-command",
            canonical_json_bytes({"locale": locale.strip().lower()}),
            0,
        )

    def matches_analysis_command(
        self, stored: str, tenant_id: str | int, *, locale: str
    ) -> bool:
        payload = canonical_json_bytes({"locale": locale.strip().lower()})
        return any(
            hmac.compare_digest(
                stored,
                self._digest(tenant_id, "analysis-command", payload, index),
            )
            for index in range(len(self.key_materials))
        )

    def preference(
        self, tenant_id: str | int, *, enabled: bool, expected_revision: int
    ) -> str:
        return self._digest(
            tenant_id,
            "preference-command",
            canonical_json_bytes(
                {"enabled": enabled, "expectedRevision": expected_revision}
            ),
            0,
        )

    def matches_preference(
        self,
        stored: str,
        tenant_id: str | int,
        *,
        enabled: bool,
        expected_revision: int,
    ) -> bool:
        payload = canonical_json_bytes(
            {"enabled": enabled, "expectedRevision": expected_revision}
        )
        return any(
            hmac.compare_digest(
                stored,
                self._digest(tenant_id, "preference-command", payload, index),
            )
            for index in range(len(self.key_materials))
        )

    def _digest(
        self, tenant_id: str | int, purpose: str, payload: bytes, key_index: int
    ) -> str:
        context = "|".join(
            (
                _PROFILE,
                normalized_environment(),
                str(tenant_id).strip(),
                purpose,
            )
        ).encode("utf-8")
        purpose_key = hmac.new(
            self.key_materials[key_index], context, hashlib.sha256
        ).digest()
        return hmac.new(purpose_key, payload, hashlib.sha256).hexdigest()
