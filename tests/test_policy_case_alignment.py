from __future__ import annotations

from agent.agent_tools import _adoption_reason_for_ref
from agent.langgraph_verification_logic import _filter_hitl_content_by_case
from services.policy_case_alignment import case_alignment_score, is_clear_case_mismatch


def _holiday_body_non_entertainment() -> dict[str, object]:
    return {
        "case_type": "HOLIDAY_USAGE",
        "isHoliday": True,
        "merchantName": "가온 식당",
        "expenseTypeName": "식대",
        "document": {
            "items": [{"sgtxt": "주말 식대", "hkont": "511001"}],
        },
    }


def test_holiday_non_entertainment_marks_article24_mismatch():
    body = _holiday_body_non_entertainment()
    ref = {
        "article": "제24조",
        "parent_title": "제24조 (접대비/업무추진비)",
        "chunk_text": "접대비는 외부 이해관계자와의 업무 목적이 명확한 경우에 한하여 허용한다.",
    }
    assert is_clear_case_mismatch(ref, body) is True


def test_holiday_alignment_score_prefers_article23_over_article24():
    body = _holiday_body_non_entertainment()
    ref23 = {
        "article": "제23조",
        "parent_title": "제23조 (식대)",
        "chunk_text": "주말/공휴일 식대(예외 승인 없는 경우)는 검토 대상이다.",
    }
    ref24 = {
        "article": "제24조",
        "parent_title": "제24조 (접대비/업무추진비)",
        "chunk_text": "접대비 필수 증빙: 참석자 명단, 외부 참석자 소속",
    }
    assert case_alignment_score(ref23, body) > case_alignment_score(ref24, body)


def test_adoption_reason_for_mismatch_is_not_direct_match():
    body = _holiday_body_non_entertainment()
    ref = {
        "article": "제24조",
        "parent_title": "제24조 (접대비/업무추진비)",
        "chunk_text": "접대비 필수 증빙: 참석자 명단, 외부 참석자 소속",
    }
    reason = _adoption_reason_for_ref(ref, body)
    assert "직접 일치" not in reason
    assert "보조 참고용" in reason


def test_filter_hitl_content_by_case_removes_entertainment_only_prompts():
    body = _holiday_body_non_entertainment()
    required_inputs = [
        {"field": "dinner_expense", "reason": "주말/공휴일 식대 확인", "guide": "주말/공휴일 식대를 입력하세요."},
        {"field": "contact_list", "reason": "접대비 참석자 명단 필요", "guide": "접대비의 참석자 명단을 입력하세요."},
    ]
    review_questions = [
        "휴일 사용 사전 승인 여부를 확인했는가?",
        "접대비 참석자 명단을 확인했는가?",
    ]

    filtered_inputs, filtered_questions = _filter_hitl_content_by_case(
        body=body,
        required_inputs=required_inputs,
        review_questions=review_questions,
    )

    assert len(filtered_inputs) == 1
    assert filtered_inputs[0]["field"] == "dinner_expense"
    assert len(filtered_questions) == 1
    assert "접대비" not in filtered_questions[0]

