from __future__ import annotations

import json
from dataclasses import dataclass, field

from .contracts import AskCitation
from .run_observability import SourceHealthObservation


class ContextBrokerUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class GroundedSource:
    citation: AskCitation
    evidence: str
    rank: int


@dataclass(frozen=True)
class GroundedContext:
    sources: tuple[GroundedSource, ...]
    attempted_sources: tuple[str, ...]
    unavailable_sources: tuple[str, ...]
    source_health: tuple[SourceHealthObservation, ...] = field(default_factory=tuple)
    status_code: str | None = None

    def model_evidence(self) -> str:
        documents = [
            {
                "sourceId": source.citation.source_id,
                "sourceType": source.citation.source_type,
                "sourceSystem": source.citation.source_system,
                "title": source.citation.title,
                "occurredAt": (
                    source.citation.occurred_at.isoformat()
                    if source.citation.occurred_at is not None
                    else None
                ),
                "evidence": source.evidence,
            }
            for source in self.sources
        ]
        return json.dumps(documents, ensure_ascii=False, separators=(",", ":"))
