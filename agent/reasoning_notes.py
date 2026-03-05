from __future__ import annotations

from typing import Any


def extract_reasoning(node_output: Any) -> str:
    """노드 출력에서 reasoning 필드 추출. workspace.md 추가보완: generate_working_note 폐기 후 사용."""
    if hasattr(node_output, "reasoning"):
        return str(node_output.reasoning or "").strip()
    if isinstance(node_output, dict):
        return str(node_output.get("reasoning") or "").strip()
    return ""


# generate_working_note() 폐기됨 — workspace.md 추가보완: 노드 실제 추론(reasoning)을 스트림으로 전달하고
# THINKING_TOKEN/THINKING_DONE으로 UI에 표시. 추출만 필요 시 extract_reasoning() 사용.
