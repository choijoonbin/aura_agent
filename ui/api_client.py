from __future__ import annotations

from typing import Any

import requests

from utils.config import settings

API = settings.api_base_url.rstrip("/")


def get(path: str) -> dict[str, Any]:
    response = requests.get(f"{API}{path}", timeout=30)
    response.raise_for_status()
    return response.json()


def post(
    path: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    response = requests.post(f"{API}{path}", params=params or {}, json=json_body, timeout=timeout)
    response.raise_for_status()
    return response.json()


def delete(path: str) -> dict[str, Any]:
    response = requests.delete(f"{API}{path}", timeout=30)
    response.raise_for_status()
    return response.json()
