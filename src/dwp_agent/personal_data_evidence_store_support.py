from __future__ import annotations

from typing import Mapping

from .governed_domain_core import GovernedDomainConflict
from .personal_data_evidence_contracts import PersonalDataEvidenceAction


def public_evidence_result(result: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def validated_evidence_parameters(
    action: PersonalDataEvidenceAction,
    parameters: Mapping[str, object],
) -> dict[str, object]:
    allowed = {
        PersonalDataEvidenceAction.BACKUP_LEDGER: {"ledgerScope"},
        PersonalDataEvidenceAction.SRE_ESCALATION: {"priority"},
        PersonalDataEvidenceAction.LEGAL_HOLD_APPEAL: {"appealReason"},
        PersonalDataEvidenceAction.SIGNED_CERTIFICATE: {"locale"},
        PersonalDataEvidenceAction.SIEM_SYNC: {"destination"},
    }[action]
    if not set(parameters).issubset(allowed):
        raise GovernedDomainConflict(
            "The evidence command contains unsupported provider parameters."
        )
    normalized: dict[str, object] = {}
    for key, value in parameters.items():
        if not isinstance(value, str):
            raise GovernedDomainConflict(
                "Evidence command provider parameters must be strings."
            )
        value = " ".join(value.split())
        if not value or len(value) > 1_000:
            raise GovernedDomainConflict(
                "Evidence command provider parameters are invalid."
            )
        normalized[key] = value
    if action == PersonalDataEvidenceAction.SRE_ESCALATION and normalized.get(
        "priority"
    ) not in {None, "P1", "P2", "P3"}:
        raise GovernedDomainConflict("The SRE escalation priority is invalid.")
    if action == PersonalDataEvidenceAction.SIGNED_CERTIFICATE and normalized.get(
        "locale"
    ) not in {None, "ko-KR", "en-US"}:
        raise GovernedDomainConflict("The certificate locale is invalid.")
    if action == PersonalDataEvidenceAction.LEGAL_HOLD_APPEAL and not normalized.get(
        "appealReason"
    ):
        raise GovernedDomainConflict("A legal-hold appeal reason is required.")
    return normalized
