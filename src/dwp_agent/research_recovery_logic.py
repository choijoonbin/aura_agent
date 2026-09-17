from __future__ import annotations

from .dwaion_workflow_contracts import ResearchPlanDefinition, ResearchResult
from .research_recovery_contracts import ResearchSensitivityAssessment


def merge_research_definitions(
    server: ResearchPlanDefinition,
    local: ResearchPlanDefinition,
) -> ResearchPlanDefinition:
    criteria = list(dict.fromkeys([*server.success_criteria, *local.success_criteria]))[:20]
    deliverables = list(
        dict.fromkeys([*server.deliverable_types, *local.deliverable_types])
    )[:10]
    sources = {item.source_key: item for item in server.source_policies}
    sources.update({item.source_key: item for item in local.source_policies})
    return ResearchPlanDefinition(
        goal=local.goal,
        question=local.question,
        success_criteria=criteria,
        deliverable_types=deliverables,
        source_policies=list(sources.values())[:100],
        require_all_allowed_sources=(
            server.require_all_allowed_sources or local.require_all_allowed_sources
        ),
        budget={
            "maximumMinutes": min(
                server.budget.maximum_minutes, local.budget.maximum_minutes
            ),
            "maximumSources": min(
                server.budget.maximum_sources, local.budget.maximum_sources
            ),
            "maximumTokens": min(
                server.budget.maximum_tokens, local.budget.maximum_tokens
            ),
        },
    )


def assess_research_sensitivity(result: ResearchResult) -> ResearchSensitivityAssessment:
    text = "\n".join(
        [
            result.report_markdown,
            *(citation.evidence for citation in result.citations),
        ]
    ).casefold()
    indicator_groups = (
        (
            "RESTRICTED",
            95,
            ("password", "api key", "secret key", "ssn", "주민등록", "계좌번호"),
        ),
        (
            "CONFIDENTIAL",
            75,
            ("confidential", "client contract", "payroll", "salary", "인사정보"),
        ),
        ("INTERNAL", 40, ("internal only", "company private", "사내 전용")),
    )
    for classification, score, indicators in indicator_groups:
        matched = [indicator.upper() for indicator in indicators if indicator in text]
        if matched:
            return ResearchSensitivityAssessment(
                classification=classification,
                score=score,
                matched_indicators=matched,
            )
    return ResearchSensitivityAssessment(
        classification="PUBLIC", score=10, matched_indicators=[]
    )
