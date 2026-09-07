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
