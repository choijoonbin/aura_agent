from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from uuid import UUID

from .canonical_json import canonical_json_bytes
from .key_provider import load_versioned_key_material, normalized_environment
from .proposal_contracts import CreateAgentProposalRequest, DecideAgentProposalRequest


_FINGERPRINT_PROFILE = "dwp-agent-proposal-request-v1"


@dataclass(frozen=True)
class ProposalRequestFingerprints:
    """Produces tenant-scoped, rotation-aware request fingerprints."""

    key_materials: tuple[bytes, ...]

    def __post_init__(self) -> None:
        if not self.key_materials or any(len(key) != 32 for key in self.key_materials):
            raise ValueError("Proposal fingerprint keys must contain 32 bytes.")

    @classmethod
    def load(cls) -> "ProposalRequestFingerprints":
        material = load_versioned_key_material()
        encoded_keys = [material.active_key]
        encoded_keys.extend(
            value for _, value in sorted(material.previous_keys.items())
        )
        return cls(tuple(base64.b64decode(value, validate=True) for value in encoded_keys))

    @classmethod
    def ephemeral(cls) -> "ProposalRequestFingerprints":
        return cls((os.urandom(32),))

    def create(
        self, tenant_id: str | int, request: CreateAgentProposalRequest
    ) -> str:
        return self._digest(tenant_id, _create_payload(request), self.key_materials[0])

    def matches_create(
        self,
        stored: str,
        tenant_id: str | int,
        request: CreateAgentProposalRequest,
    ) -> bool:
        return self._matches(stored, tenant_id, _create_payload(request))

    def decision(
        self,
        tenant_id: str | int,
        proposal_id: UUID,
        request: DecideAgentProposalRequest,
    ) -> str:
        return self._digest(
            tenant_id,
            _decision_payload(proposal_id, request),
            self.key_materials[0],
        )

    def matches_decision(
        self,
        stored: str | None,
        tenant_id: str | int,
        proposal_id: UUID,
        request: DecideAgentProposalRequest,
    ) -> bool:
        if stored is None:
            return False
        return self._matches(stored, tenant_id, _decision_payload(proposal_id, request))

    def _matches(self, stored: str, tenant_id: str | int, payload: bytes) -> bool:
        return any(
            hmac.compare_digest(stored, self._digest(tenant_id, payload, key))
            for key in self.key_materials
        )

    def _digest(self, tenant_id: str | int, payload: bytes, key: bytes) -> str:
        context = "|".join(
            (
                _FINGERPRINT_PROFILE,
                normalized_environment(),
                "dwp-agent",
                "agent-proposal",
                str(tenant_id).strip(),
            )
        ).encode("utf-8")
        purpose_key = hmac.new(key, context, hashlib.sha256).digest()
        return hmac.new(purpose_key, payload, hashlib.sha256).hexdigest()


def _create_payload(request: CreateAgentProposalRequest) -> bytes:
    payload = request.model_dump(mode="json", by_alias=True)
    payload.pop("commandId", None)
    return canonical_json_bytes(payload)


def _decision_payload(
    proposal_id: UUID, request: DecideAgentProposalRequest
) -> bytes:
    payload = request.model_dump(mode="json", by_alias=True)
    payload.pop("commandId", None)
    payload["proposalId"] = str(proposal_id)
    return canonical_json_bytes(payload)
