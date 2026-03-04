from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from utils.config import settings


FIELD_LABELS = {
    "hr_status": "근태 상태",
    "hrstatus": "근태 상태",
    "hrstatusraw": "원본 근태 상태",
    "mcc_code": "업종 코드",
    "mcccode": "업종 코드",
    "mcctype": "업종 유형",
    "budat": "전표 기준일",
    "cputm": "입력 시각",
    "occurredat": "발생 일시",
    "merchantname": "가맹점명",
    "amount": "금액",
    "expenseType": "경비 유형",
    "expensetype": "경비 유형",
    "budgetExceeded": "예산 초과 여부",
    "budgetexceeded": "예산 초과 여부",
    "document": "전표 문서",
    "items": "라인아이템",
    "isHoliday": "휴일 여부",
    "isholiday": "휴일 여부",
}


def _display_label(key: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]", "", str(key or "")).replace("_", "").lower()
    return FIELD_LABELS.get(str(key), FIELD_LABELS.get(normalized, str(key)))


def _humanize_context(value: Any) -> Any:
    if isinstance(value, dict):
        return {_display_label(k): _humanize_context(v) for k, v in value.items()}
    if isinstance(value, list):
        if all(isinstance(v, str) for v in value):
            return [_display_label(v) for v in value]
        return [_humanize_context(v) for v in value]
    return value


def _setup_aura_import_path() -> None:
    p = Path(settings.aura_platform_path)
    if not p.exists():
        raise RuntimeError(f"AURA_PLATFORM_PATH not found: {p}")
    env_path = p / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    debug_value = (os.getenv("DEBUG") or "").strip().lower()
    if debug_value and debug_value not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
        os.environ["DEBUG"] = "false"
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
    os.environ.setdefault("TENANT_DEFAULT", str(settings.default_tenant_id))


def _safe_json_fragment(text: str) -> dict[str, str] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {k: str(v or "") for k, v in data.items()}
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        if isinstance(data, dict):
            return {k: str(v or "") for k, v in data.items()}
    except Exception:
        return None
    return None


def _trim(value: str | None, fallback: str, limit: int = 180) -> str:
    text = (value or "").strip() or fallback
    return text[:limit].rstrip()


async def generate_working_note(
    *,
    node: str,
    role: str,
    context: dict[str, Any],
    fallback_message: str,
    fallback_thought: str | None = None,
    fallback_action: str | None = None,
    fallback_observation: str | None = None,
) -> dict[str, str]:
    fallback = {
        "message": fallback_message,
        "thought": fallback_thought or fallback_message,
        "action": fallback_action or "현재 단계 실행",
        "observation": fallback_observation or fallback_message,
        "source": "fallback",
    }

    try:
        _setup_aura_import_path()
        from core.llm.client import get_llm_client

        client = get_llm_client(None)
        note_model = getattr(client, "model", None) or getattr(client, "model_name", None)
        if isinstance(note_model, str):
            note_model = note_model.strip() or None
        if not note_model and hasattr(client, "get_default_model"):
            note_model = getattr(client.get_default_model(), "name", None)
        note_model = note_model or settings.reasoning_llm_label
        prompt = [
            {
                "role": "system",
                "content": (
                    "당신은 엔터프라이즈 감사 에이전트의 실행 로그 작성자다. "
                    "내부 비공개 chain-of-thought를 노출하지 말고, 현재 단계에서 외부에 공개 가능한 작업 메모만 JSON으로 작성하라. "
                    "반드시 JSON 객체만 반환하고 키는 message, thought, action, observation 네 개만 사용하라. "
                    "각 값은 한국어 한 문장 또는 두 문장 이내로, 사실 기반으로만 작성하라."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "node": node,
                        "role": role,
                        "context": _humanize_context(context),
                        "rules": {
                            "grounded_only": True,
                            "no_hidden_reasoning": True,
                            "tone": "operational_and_concise",
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        raw = await client.ainvoke(prompt)
        payload = _safe_json_fragment(raw) or {}
        return {
            "message": _trim(payload.get("message"), fallback["message"]),
            "thought": _trim(payload.get("thought"), fallback["thought"]),
            "action": _trim(payload.get("action"), fallback["action"]),
            "observation": _trim(payload.get("observation"), fallback["observation"]),
            "source": "llm",
            "note_model": note_model,
        }
    except Exception:
        return fallback
