from __future__ import annotations

import re
from pathlib import Path
from typing import Any


RULEBOOK_ROOT = Path("/Users/joonbinchoi/Work/MaterTask/규정집")
UPLOAD_ROOT = RULEBOOK_ROOT / "uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)


def list_rulebook_files() -> list[dict[str, Any]]:
    files = sorted(list(RULEBOOK_ROOT.glob("*.txt")) + list(UPLOAD_ROOT.glob("*.txt")))
    out: list[dict[str, Any]] = []
    for path in files:
        out.append(
            {
                "name": path.name,
                "path": str(path),
                "size": path.stat().st_size,
                "source": "upload" if path.parent == UPLOAD_ROOT else "bundled",
            }
        )
    return out


def save_uploaded_rulebook(name: str, content: bytes) -> str:
    safe_name = Path(name).name
    if not safe_name.endswith(".txt"):
        safe_name = f"{safe_name}.txt"
    target = UPLOAD_ROOT / safe_name
    target.write_bytes(content)
    return str(target)


def load_rulebook_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _split_article_sections(text: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"(제\s*\d+\s*조[^\n]*)")
    parts = pattern.split(text)
    sections: list[tuple[str, str]] = []
    current_title = "문서 개요"
    current_body: list[str] = []
    for part in parts:
        stripped = part.strip()
        if not stripped:
            continue
        if pattern.fullmatch(stripped):
            if current_body:
                sections.append((current_title, "\n".join(current_body).strip()))
            current_title = stripped
            current_body = []
        else:
            current_body.append(stripped)
    if current_body:
        sections.append((current_title, "\n".join(current_body).strip()))
    return sections


def _window_split(text: str, chunk_size: int = 700, overlap: int = 120) -> list[str]:
    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        chunk = text[cursor : cursor + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
        cursor += max(1, chunk_size - overlap)
    return chunks


def preview_chunks(text: str, strategy: str) -> list[dict[str, Any]]:
    if strategy == "article_first":
        return [
            {"title": title, "content": body, "length": len(body), "strategy": strategy}
            for title, body in _split_article_sections(text)
            if body
        ]
    if strategy == "sliding_window":
        return [
            {"title": f"윈도우 {idx}", "content": chunk, "length": len(chunk), "strategy": strategy}
            for idx, chunk in enumerate(_window_split(text, chunk_size=700, overlap=120), start=1)
        ]
    # hybrid_policy
    out: list[dict[str, Any]] = []
    for title, body in _split_article_sections(text):
        if len(body) <= 900:
            out.append({"title": title, "content": body, "length": len(body), "strategy": strategy})
            continue
        for idx, chunk in enumerate(_window_split(body, chunk_size=650, overlap=100), start=1):
            out.append({"title": f"{title} · part {idx}", "content": chunk, "length": len(chunk), "strategy": strategy})
    return out
