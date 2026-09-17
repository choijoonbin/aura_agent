from __future__ import annotations

from collections.abc import Mapping
from textwrap import wrap


def render_deletion_certificate(fields: Mapping[str, object]) -> bytes:
    """Render a small dependency-free, deterministic one-page PDF certificate."""

    lines = ["DWAI-ON Personal Data Destruction Certificate"]
    for key, value in fields.items():
        lines.extend(_field_lines(key, value))
    text = ["BT", "/F1 9 Tf", "36 806 Td"]
    for index, line in enumerate(lines):
        if index:
            text.append("0 -13 Td")
        text.append(f"({_escape(line)}) Tj")
    text.append("ET")
    stream = "\n".join(text).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, value in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(value)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def _field_lines(key: object, value: object) -> list[str]:
    label = str(key).encode("ascii", "backslashreplace").decode("ascii")
    rendered = str(value).encode("ascii", "backslashreplace").decode("ascii")
    chunks = wrap(
        rendered,
        width=72,
        replace_whitespace=False,
        drop_whitespace=False,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [""]
    if len(chunks) == 1:
        return [f"{label}: {chunks[0]}"]
    return [
        f"{label} [{index}/{len(chunks)}]: {chunk}"
        for index, chunk in enumerate(chunks, start=1)
    ]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
