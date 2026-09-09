from __future__ import annotations

import re

from .governed_domain_core import GovernedDomainConflict
from .personal_memory_contracts import ExplicitMemoryValue
from .policy import contains_prompt_injection


_BLOCKED_PATTERNS = (
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:password|passwd|api[_ -]?key|client[_ -]?secret|"
        r"access[_ -]?token|refresh[_ -]?token)\b\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?<!\d)\d{6}-?[1-4]\d{6}(?!\d)"),
    re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    re.compile(
        r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])"
    ),
    re.compile(r"(?<!\d)(?:\+?82[- ]?)?0?1[016789][- ]?\d{3,4}[- ]?\d{4}(?!\d)"),
)

_PAYMENT_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def require_safe_explicit_memory(memory: ExplicitMemoryValue) -> None:
    value = memory.value
    if contains_prompt_injection(value) or any(pattern.search(value) for pattern in _BLOCKED_PATTERNS):
        _reject()
    for candidate in _PAYMENT_CARD_CANDIDATE.findall(value):
        digits = "".join(character for character in candidate if character.isdigit())
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            _reject()


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _reject() -> None:
    raise GovernedDomainConflict(
        "Credentials, direct identifiers, and regulated data cannot be stored "
        "as personal AI memory."
    )
