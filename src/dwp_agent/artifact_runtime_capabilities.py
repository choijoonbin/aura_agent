from __future__ import annotations

from .artifact_contracts import ArtifactCapabilities
from .governed_worker_runtime import governed_worker_available


def artifact_runtime_capabilities() -> ArtifactCapabilities:
    return ArtifactCapabilities(
        source_verification_available=True,
        source_verification_scope="SERVER_BOUND_CONVERSATION_CITATIONS_ONLY",
        export_execution_available=governed_worker_available("ARTIFACT_EXPORT"),
    )
