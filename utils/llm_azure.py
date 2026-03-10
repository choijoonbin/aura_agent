"""
Azure OpenAI 호출 시 temperature=0.0 미지원으로 인한 400 방지.
전체 LLM chat.completions.create 호출에서 동일 규칙 적용용 공통 유틸.
"""
from __future__ import annotations

from typing import Any


def is_azure_openai(base_url: str | None) -> bool:
    """base_url이 Azure OpenAI 엔드포인트인지 여부."""
    return bool(base_url and ".openai.azure.com" in (base_url or "").strip())


def completion_kwargs_for_azure(base_url: str | None, **kwargs: Any) -> dict[str, Any]:
    """
    chat.completions.create에 넘길 kwargs를 반환.
    Azure이고 temperature가 0.0이면 temperature를 제거해 400 방지.
    """
    out = dict(kwargs)
    if not is_azure_openai(base_url):
        return out
    temp = out.get("temperature")
    if temp is not None and float(temp) == 0.0:
        out.pop("temperature", None)
    return out
