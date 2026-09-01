from __future__ import annotations


def normalized_header_values(value: str | None) -> tuple[str, ...]:
    """Return the canonical, de-duplicated authority values from a DWP header."""
    if value is None:
        return ()
    return tuple(
        sorted({item.strip().upper() for item in value.split(",") if item.strip()})
    )
