from __future__ import annotations

from dataclasses import dataclass
from html import escape
from io import BytesIO
from textwrap import wrap
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .artifact_contracts import ArtifactDraftContent, ExportFormat


@dataclass(frozen=True)
class RenderedArtifactExport:
    content: bytes
    media_type: str
    extension: str


def render_artifact_export(
    content: ArtifactDraftContent,
    export_format: ExportFormat,
) -> RenderedArtifactExport:
    if export_format == ExportFormat.MARKDOWN:
        rendered = f"# {content.title}\n\n{content.body.rstrip()}\n".encode("utf-8")
        return RenderedArtifactExport(rendered, "text/markdown; charset=utf-8", "md")
    if export_format == ExportFormat.DOCX:
        return RenderedArtifactExport(
            _render_docx(content),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
        )
    if export_format == ExportFormat.PDF:
        return RenderedArtifactExport(_render_pdf(content), "application/pdf", "pdf")
    raise ValueError("The artifact export format is unsupported.")


def _render_docx(content: ArtifactDraftContent) -> bytes:
    paragraphs = [
        _word_paragraph(content.title, style="Title"),
        *(_word_paragraph(line) for line in content.body.splitlines() or [""]),
    ]
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(paragraphs)}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
        "</w:sectPr></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        _zip_write(archive, "[Content_Types].xml", content_types)
        _zip_write(archive, "_rels/.rels", relationships)
        _zip_write(archive, "word/document.xml", document)
    return output.getvalue()


def _word_paragraph(value: str, *, style: str | None = None) -> str:
    properties = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    if not value:
        return f"<w:p>{properties}</w:p>"
    return (
        f"<w:p>{properties}<w:r><w:t xml:space=\"preserve\">"
        f"{escape(value)}"
        "</w:t></w:r></w:p>"
    )


def _zip_write(archive: ZipFile, name: str, value: str) -> None:
    entry = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = ZIP_DEFLATED
    entry.external_attr = 0o600 << 16
    archive.writestr(entry, value.encode("utf-8"))


def _render_pdf(content: ArtifactDraftContent) -> bytes:
    lines = [content.title, ""]
    for paragraph in content.body.splitlines() or [""]:
        lines.extend(wrap(paragraph, width=64, replace_whitespace=False) or [""])
    pages = [lines[index : index + 46] for index in range(0, len(lines), 46)] or [[""]]

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    page_ids = [5 + (index * 2) for index in range(len(pages))]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(
        b"<< /Type /Font /Subtype /Type0 /BaseFont /HYSMyeongJo-Medium "
        b"/Encoding /UniKS-UCS2-H /DescendantFonts [4 0 R] >>"
    )
    objects.append(
        b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HYSMyeongJo-Medium "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Korea1) /Supplement 2 >> >>"
    )
    for index, page_lines in enumerate(pages):
        content_id = page_ids[index] + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode()
        )
        commands = ["BT", "/F1 11 Tf", "1 0 0 1 48 794 Tm"]
        for line_index, line in enumerate(page_lines):
            if line_index:
                commands.append("0 -16 Td")
            commands.append(f"<{_pdf_unicode_hex(line)}> Tj")
        commands.append("ET")
        stream = "\n".join(commands).encode("ascii")
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"\nendstream"
        )

    output = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode())
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(output)


def _pdf_unicode_hex(value: str) -> str:
    return (b"\xfe\xff" + value.encode("utf-16-be", errors="replace")).hex().upper()
