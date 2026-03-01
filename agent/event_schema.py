from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class AgentEvent:
    event_type: str
    message: str
    node: str | None = None
    phase: str | None = None
    tool: str | None = None
    decision_code: str | None = None
    input_hash: str | None = None
    output_ref: str | None = None
    evidence_ids: list[str] = field(default_factory=list)
    thought: str | None = None
    action: str | None = None
    observation: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "message": self.message,
            "node": self.node,
            "phase": self.phase,
            "tool": self.tool,
            "decision_code": self.decision_code,
            "input_hash": self.input_hash,
            "output_ref": self.output_ref,
            "evidence_ids": self.evidence_ids,
            "thought": self.thought,
            "action": self.action,
            "observation": self.observation,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }
