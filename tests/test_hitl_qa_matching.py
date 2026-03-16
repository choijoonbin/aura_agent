"""
Sprint 3: HITL Q&A 매칭 테스트

_match_questions_to_prior_answers 함수의 단위 테스트.
LLM 호출은 unittest.mock으로 패치하여 네트워크 없이 실행.
"""
from __future__ import annotations

import json
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_llm_response(items: list[dict]) -> MagicMock:
    """LLM 응답 mock 객체 생성."""
    msg = MagicMock()
    msg.content = json.dumps(items)
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _body_evidence(bktxt: str = "", user_reason: str = "", sgtxt: str = "") -> dict:
    return {"memo": {"bktxt": bktxt, "user_reason": user_reason, "sgtxt": sgtxt}}


def _mock_settings(base_url: str = "https://api.openai.com/v1", api_key: str = "test-key"):
    """settings mock 생성 (non-azure)."""
    s = MagicMock()
    s.openai_base_url = base_url
    s.openai_api_key = api_key
    s.openai_api_version = "2024-12-01-preview"
    s.reasoning_llm_model = "gpt-4o-mini"
    return s


def _make_openai_module(mock_client: AsyncMock) -> ModuleType:
    """openai 모듈을 mock으로 대체하는 가짜 모듈 생성."""
    fake_openai = ModuleType("openai")
    fake_openai.AsyncOpenAI = MagicMock(return_value=mock_client)  # type: ignore[attr-defined]
    fake_openai.AsyncAzureOpenAI = MagicMock(return_value=mock_client)  # type: ignore[attr-defined]
    return fake_openai


# ──────────────────────────────────────────────────────────────────────────────
# 1. 정상 매칭: 질문 2개 모두 커버됨
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_match_both_covered():
    from agent.langgraph_verification_logic import _match_questions_to_prior_answers

    llm_items = [
        {"index": 0, "covered": True, "matched_answer": "사전 승인 완료", "basis_field": "user_reason"},
        {"index": 1, "covered": True, "matched_answer": "팀 전략회의 후 식대", "basis_field": "bktxt"},
    ]
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_make_llm_response(llm_items))

    questions = ["휴일 사용 사전 승인이 있었나요?", "업무 목적을 설명해 주세요"]
    body = _body_evidence(bktxt="팀 전략회의 후 식대", user_reason="사전 승인 완료")

    with patch.dict(sys.modules, {"openai": _make_openai_module(mock_client)}), \
         patch("agent.langgraph_verification_logic.settings", _mock_settings()):
        result = await _match_questions_to_prior_answers(questions, body)

    assert len(result) == 2
    assert result[0]["covered"] is True
    assert result[0]["matched_answer"] == "사전 승인 완료"
    assert result[0]["basis_field"] == "user_reason"
    assert result[1]["covered"] is True
    assert result[1]["basis_field"] == "bktxt"


# ──────────────────────────────────────────────────────────────────────────────
# 2. 일부 미확인: Q1 커버, Q2 미확인
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_match_partial_covered():
    from agent.langgraph_verification_logic import _match_questions_to_prior_answers

    llm_items = [
        {"index": 0, "covered": True, "matched_answer": "김팀장 사전 승인", "basis_field": "user_reason"},
        {"index": 1, "covered": False, "matched_answer": "", "basis_field": ""},
    ]
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_make_llm_response(llm_items))

    questions = ["사전 승인 여부를 확인했는가?", "참석자 명단을 제출했는가?"]
    body = _body_evidence(user_reason="김팀장 사전 승인")

    with patch.dict(sys.modules, {"openai": _make_openai_module(mock_client)}), \
         patch("agent.langgraph_verification_logic.settings", _mock_settings()):
        result = await _match_questions_to_prior_answers(questions, body)

    assert result[0]["covered"] is True
    assert result[1]["covered"] is False
    assert result[1]["matched_answer"] == ""


# ──────────────────────────────────────────────────────────────────────────────
# 3. 기존 답변 없음 → LLM 호출 없이 즉시 fallback
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_match_no_prior_answers_skips_llm():
    from agent.langgraph_verification_logic import _match_questions_to_prior_answers

    questions = ["사전 승인 여부를 확인했는가?"]
    body = _body_evidence()  # 모두 빈 문자열

    fake_openai = ModuleType("openai")
    fake_openai.AsyncOpenAI = MagicMock()  # type: ignore[attr-defined]
    fake_openai.AsyncAzureOpenAI = MagicMock()  # type: ignore[attr-defined]

    # LLM 호출이 일어나면 안 됨
    with patch.dict(sys.modules, {"openai": fake_openai}):
        result = await _match_questions_to_prior_answers(questions, body)
        fake_openai.AsyncOpenAI.assert_not_called()

    assert len(result) == 1
    assert result[0]["covered"] is False
    assert result[0]["matched_answer"] == ""


# ──────────────────────────────────────────────────────────────────────────────
# 4. 질문 없음 → 빈 리스트 반환
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_match_empty_questions():
    from agent.langgraph_verification_logic import _match_questions_to_prior_answers

    result = await _match_questions_to_prior_answers([], _body_evidence(user_reason="무언가"))
    assert result == []


# ──────────────────────────────────────────────────────────────────────────────
# 5. LLM 호출 실패 → fallback (모두 covered=False)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_match_llm_error_fallback():
    from agent.langgraph_verification_logic import _match_questions_to_prior_answers

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("API 오류"))

    questions = ["사전 승인 여부?", "참석자 명단?"]
    body = _body_evidence(user_reason="김팀장 승인")

    with patch.dict(sys.modules, {"openai": _make_openai_module(mock_client)}), \
         patch("agent.langgraph_verification_logic.settings", _mock_settings()):
        result = await _match_questions_to_prior_answers(questions, body)

    assert len(result) == 2
    assert all(not r["covered"] for r in result)
    assert all(r["matched_answer"] == "" for r in result)


# ──────────────────────────────────────────────────────────────────────────────
# 6. LLM이 dict 래퍼로 반환 ({"matches": [...]}) → 정상 파싱
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_match_llm_wrapped_dict_response():
    from agent.langgraph_verification_logic import _match_questions_to_prior_answers

    llm_items = [{"index": 0, "covered": True, "matched_answer": "승인됨", "basis_field": "sgtxt"}]
    wrapped = {"matches": llm_items}  # ← 래퍼 dict
    msg = MagicMock()
    msg.content = json.dumps(wrapped)
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=resp)

    questions = ["사전 승인 여부?"]
    body = _body_evidence(sgtxt="승인됨")

    with patch.dict(sys.modules, {"openai": _make_openai_module(mock_client)}), \
         patch("agent.langgraph_verification_logic.settings", _mock_settings()):
        result = await _match_questions_to_prior_answers(questions, body)

    assert result[0]["covered"] is True
    assert result[0]["basis_field"] == "sgtxt"
