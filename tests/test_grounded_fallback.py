import json

from dwp_agent.context_broker import GroundedContext, GroundedSource
from dwp_agent.contracts import AskCitation, CitationSourceType
from dwp_agent.grounded_fallback import MAX_FALLBACK_SOURCES, grounded_evidence_fallback
from dwp_agent.grounded_response_status import (
    GROUNDED_FALLBACK_STATUS,
    normalize_legacy_ask_response_payload,
    normalize_legacy_grounded_status,
)


def test_grounded_fallback_is_bounded_localized_and_citation_complete() -> None:
    sources = tuple(
        GroundedSource(
            citation=AskCitation(
                source_id=f"src-{index:02d}",
                source_type=CitationSourceType.WORK_ITEM,
                title=f"Verified work item {index}",
                source_system="DWP Work",
                excerpt="Evidence " * 50,
            ),
            evidence="Evidence " * 50,
            rank=index,
        )
        for index in range(1, 9)
    )

    result = grounded_evidence_fallback(
        GroundedContext(sources=sources, attempted_sources=("WORK_ITEM",), unavailable_sources=()),
        "ko-KR",
    )

    assert result.provider == "DWP_GROUNDED_FALLBACK"
    assert result.model == "evidence-snapshot-v1"
    assert result.confidence == "LOW"
    assert len(result.cited_source_ids) == MAX_FALLBACK_SOURCES
    assert result.answer is not None
    assert "현재 권한으로 확인된 업무 근거" in result.answer
    assert "[src-05]" in result.answer
    assert "[src-06]" not in result.answer
    assert len(max(result.answer.splitlines(), key=len)) < 330


def test_grounded_fallback_prioritizes_urgent_evidence_without_exposing_metadata() -> None:
    sources = (
        GroundedSource(
            citation=AskCitation(
                source_id="src-01",
                source_type=CitationSourceType.MAIL,
                title="Newsletter",
                source_system="DWP Mail",
                excerpt="A product newsletter. | importance=LOW | unread=False",
            ),
            evidence="A product newsletter. | importance=LOW | unread=False",
            rank=1,
        ),
        GroundedSource(
            citation=AskCitation(
                source_id="src-02",
                source_type=CitationSourceType.WORK_ITEM,
                title="Access expires today",
                source_system="DWP Work",
                excerpt="Renew the customer access link. | priority=URGENT | status=DUE_SOON",
            ),
            evidence="Renew the customer access link. | priority=URGENT | status=DUE_SOON",
            rank=2,
        ),
        GroundedSource(
            citation=AskCitation(
                source_id="src-03",
                source_type=CitationSourceType.WORK_ITEM,
                title="Review access request",
                source_system="DWP Work",
                excerpt="Review the request today. | priority=HIGH | status=DUE_SOON",
            ),
            evidence="Review the request today. | priority=HIGH | status=DUE_SOON",
            rank=3,
        ),
    )

    result = grounded_evidence_fallback(
        GroundedContext(sources=sources, attempted_sources=("MAIL", "WORK_ITEM"), unavailable_sources=()),
        "ko-KR",
    )

    assert result.cited_source_ids == ("src-02", "src-03", "src-01")
    assert result.answer is not None
    assert result.answer.splitlines()[1].startswith("1. [긴급] Access expires today")
    assert "importance=" not in result.answer
    assert "priority=" not in result.answer
    assert "unread=" not in result.answer


def test_legacy_fallback_status_is_normalized_only_for_the_fallback_provider() -> None:
    legacy_payload = json.dumps(
        {
            "statusCode": "ANSWER_GROUNDED",
            "modelRoute": {"provider": "DWP_GROUNDED_FALLBACK"},
        }
    ).encode("utf-8")

    normalized = json.loads(normalize_legacy_ask_response_payload(legacy_payload))

    assert normalized["statusCode"] == GROUNDED_FALLBACK_STATUS
    assert normalize_legacy_grounded_status(
        "DWP_GROUNDED_FALLBACK", "ANSWER_GROUNDED"
    ) == GROUNDED_FALLBACK_STATUS
    assert normalize_legacy_grounded_status(
        "AZURE_OPENAI", "ANSWER_GROUNDED"
    ) == "ANSWER_GROUNDED"
