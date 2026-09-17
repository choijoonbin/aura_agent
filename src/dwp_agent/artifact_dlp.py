from __future__ import annotations

import re
from dataclasses import dataclass

from .artifact_contracts import ArtifactDraftContent, DlpFinding, DlpOutcome


@dataclass(frozen=True)
class DlpAssessment:
    outcome: DlpOutcome
    findings: list[DlpFinding]


_RULES = (
    (
        "PRIVATE_KEY_MATERIAL",
        DlpOutcome.BLOCKED,
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    ),
    (
        "CREDENTIAL_ASSIGNMENT",
        DlpOutcome.BLOCKED,
        re.compile(
            r"\b(?:password|passwd|api[_ -]?key|access[_ -]?token|secret)\b\s*[:=]\s*\S+",
            re.IGNORECASE,
        ),
    ),
    (
        "KOREAN_RESIDENT_ID",
        DlpOutcome.BLOCKED,
        re.compile(r"(?<!\d)\d{6}-?[1-4]\d{6}(?!\d)"),
    ),
    (
        "EMAIL_ADDRESS",
        DlpOutcome.REVIEW,
        re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])"),
    ),
    (
        "PHONE_NUMBER",
        DlpOutcome.REVIEW,
        re.compile(r"(?<!\d)(?:\+?82[- ]?)?0?1[016789][- ]?\d{3,4}[- ]?\d{4}(?!\d)"),
    ),
    (
        "FINANCIAL_ID",
        DlpOutcome.REVIEW,
        re.compile(r"(?<![A-Za-z0-9])(?:\d[ -]?){10,16}(?![A-Za-z0-9])"),
    ),
)

_REMEDIATION_RULES = (
    (
        "PRIVATE_KEY_MATERIAL",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
            r"(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\Z)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "CREDENTIAL_ASSIGNMENT",
        re.compile(
            r"\b(?:password|passwd|api[_ -]?key|access[_ -]?token|secret)\b\s*[:=]\s*\S+",
            re.IGNORECASE,
        ),
    ),
    ("KOREAN_RESIDENT_ID", _RULES[2][2]),
    ("EMAIL_ADDRESS", _RULES[3][2]),
    ("PHONE_NUMBER", _RULES[4][2]),
    (
        "FINANCIAL_ID",
        re.compile(r"(?<![A-Za-z0-9])(?:\d[ -]?){10,16}(?![A-Za-z0-9])"),
    ),
)


def assess_artifact(
    content: ArtifactDraftContent,
    *,
    all_sources_verified: bool,
) -> DlpAssessment:
    findings: list[DlpFinding] = []
    for field, value in (("title", content.title), ("body", content.body)):
        for code, severity, pattern in _RULES:
            if pattern.search(value):
                findings.append(DlpFinding(code=code, severity=severity, field=field))
    if not all_sources_verified:
        findings.append(
            DlpFinding(
                code="SOURCE_NOT_VERIFIED",
                severity=DlpOutcome.REVIEW,
                field="source",
            )
        )
    findings.sort(key=lambda item: (item.severity.value, item.field, item.code))
    outcome = DlpOutcome.PASS
    if any(finding.severity == DlpOutcome.BLOCKED for finding in findings):
        outcome = DlpOutcome.BLOCKED
    elif findings:
        outcome = DlpOutcome.REVIEW
    return DlpAssessment(outcome=outcome, findings=findings)


def remediate_artifact_content(
    content: ArtifactDraftContent,
    *,
    synthetic: bool,
) -> tuple[ArtifactDraftContent, int, list[str]]:
    title, title_count, title_codes = remediate_artifact_text(
        content.title, synthetic=synthetic
    )
    body, body_count, body_codes = remediate_artifact_text(
        content.body, synthetic=synthetic
    )
    return (
        ArtifactDraftContent(title=title, body=body, format=content.format),
        title_count + body_count,
        sorted(title_codes | body_codes),
    )


def remediate_artifact_text(
    value: str,
    *,
    synthetic: bool,
) -> tuple[str, int, set[str]]:
    result = value
    total = 0
    remediated_codes: set[str] = set()
    counters: dict[str, int] = {}
    for code, pattern in _REMEDIATION_RULES:

        def replacement(_: re.Match[str], *, category: str = code) -> str:
            counters[category] = counters.get(category, 0) + 1
            if synthetic:
                return f"[SYNTHETIC_{category}_{counters[category]:03d}]"
            return f"[MASKED_{category}]"

        result, count = pattern.subn(replacement, result)
        if count:
            remediated_codes.add(code)
            total += count
    return result, total, remediated_codes
