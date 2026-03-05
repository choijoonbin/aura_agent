from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 계층적 청킹용 패턴
_ARTICLE_PATTERN = re.compile(r"^(제\s*\d+\s*조(?:\s*\([^)]+\))?)\s*(.*)$", re.MULTILINE)
_CLAUSE_PATTERN = re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩]|^\d+\.\s|^[가-힣]\.\s", re.MULTILINE)
_CHAPTER_PATTERN = re.compile(r"^(제\s*\d+\s*장[^\n]*)", re.MULTILINE)
_SECTION_PATTERN = re.compile(r"^(제\s*\d+\s*절[^\n]*)", re.MULTILINE)


RULEBOOK_ROOT = Path("/Users/joonbinchoi/Work/AuraAgent/규정집")
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


# ─────────────────────────────────────────────────────────────────────────────
# 계층적 청킹 (Parent-Child): hierarchical_parent_child 전략
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ChunkNode:
    """계층적 청크 노드."""
    node_type: str  # "ARTICLE" | "CLAUSE" | "PARAGRAPH"
    regulation_article: str | None
    regulation_clause: str | None
    parent_title: str | None
    chunk_text: str
    search_text: str
    contextual_header: str = ""
    children: list["ChunkNode"] = field(default_factory=list)
    chunk_index: int = 0
    page_no: int = 1


def _extract_article_title(header_line: str) -> tuple[str, str]:
    """'제23조 (식대)' → ('제23조', '(식대)')."""
    m = re.match(r"(제\s*\d+\s*조)\s*(\([^)]+\))?(.*)$", header_line.strip())
    if not m:
        return header_line.strip(), ""
    article = m.group(1).strip()
    title = (m.group(2) or m.group(3) or "").strip()
    return article, title


def _split_into_clauses(article_body: str) -> list[tuple[str, str]]:
    """조문 본문을 항/호 단위로 분리. 반환: [(clause_marker, clause_text), ...]."""
    pattern = re.compile(r"([①②③④⑤⑥⑦⑧⑨⑩]|\d+\.\s|[가나다라마바사아자차카타파하]\.\s)")
    parts = pattern.split(article_body)
    clauses: list[tuple[str, str]] = []
    marker = ""
    buffer: list[str] = []
    for part in parts:
        if part.strip() and pattern.fullmatch(part.strip()):
            if buffer and "".join(buffer).strip():
                clauses.append((marker, "".join(buffer).strip()))
            marker = part.strip()
            buffer = []
        else:
            buffer.append(part)
    if buffer and "".join(buffer).strip():
        clauses.append((marker, "".join(buffer).strip()))
    return clauses


def _build_contextual_header(article: str, title: str, chapter_context: str = "") -> str:
    """Contextual RAG: 각 청크 앞에 붙는 조문 맥락 요약."""
    parts = []
    if chapter_context:
        parts.append(chapter_context)
    if article:
        parts.append(article)
    if title:
        parts.append(title)
    if parts:
        return f"[{' > '.join(parts)}] "
    return ""


def hierarchical_chunk(text: str) -> list[ChunkNode]:
    """
    규정집 텍스트를 조문-항/호 계층으로 분리.
    ARTICLE 노드(조문 전체) + CLAUSE 노드(항 단위). 단항 조문은 ARTICLE만.
    """
    nodes: list[ChunkNode] = []
    chunk_index = 0
    current_chapter = ""

    chapter_splits = _CHAPTER_PATTERN.split(text)

    for part in chapter_splits:
        if part.strip() and _CHAPTER_PATTERN.fullmatch(part.strip()):
            current_chapter = part.strip()
            continue

        article_splits = _ARTICLE_PATTERN.split(part)
        i = 0
        while i < len(article_splits):
            segment = article_splits[i].strip()
            if not segment:
                i += 1
                continue

            if _ARTICLE_PATTERN.fullmatch(segment):
                article_header = segment
                i += 1
                # 본문: 다음 조문 헤더가 나올 때까지 모든 세그먼트를 이어 붙임 (.*$는 한 줄만 매칭하므로)
                body_parts = []
                while i < len(article_splits):
                    seg = article_splits[i].strip()
                    if not seg:
                        i += 1
                        continue
                    if _ARTICLE_PATTERN.fullmatch(seg):
                        break
                    body_parts.append(seg)
                    i += 1
                article_body = "\n".join(body_parts)

                article_num, article_title = _extract_article_title(article_header)
                full_title = f"{article_num} {article_title}".strip()
                contextual_header = _build_contextual_header(article_num, article_title, current_chapter)

                article_full_text = f"{article_header}\n{article_body}".strip()
                article_node = ChunkNode(
                    node_type="ARTICLE",
                    regulation_article=article_num,
                    regulation_clause=None,
                    parent_title=full_title,
                    chunk_text=article_full_text,
                    search_text=article_body,
                    contextual_header=contextual_header,
                    chunk_index=chunk_index,
                )
                chunk_index += 1

                clauses = _split_into_clauses(article_body)
                if len(clauses) >= 2:
                    for marker, clause_text in clauses:
                        clause_chunk_text = f"{contextual_header}{marker} {clause_text}".strip()
                        clause_node = ChunkNode(
                            node_type="CLAUSE",
                            regulation_article=article_num,
                            regulation_clause=marker or None,
                            parent_title=full_title,
                            chunk_text=clause_chunk_text,
                            search_text=clause_text,
                            contextual_header=contextual_header,
                            chunk_index=chunk_index,
                        )
                        chunk_index += 1
                        article_node.children.append(clause_node)
                        nodes.append(clause_node)
                    nodes.insert(len(nodes) - len(article_node.children), article_node)
                else:
                    nodes.append(article_node)
            else:
                i += 1

    return nodes


def preview_chunks_hierarchical(text: str) -> list[dict[str, Any]]:
    """UI 미리보기용. preview_chunks()와 동일한 형태로 반환."""
    nodes = hierarchical_chunk(text)
    return [
        {
            "title": f"{node.regulation_article or ''} {node.parent_title or ''} [{node.node_type}]".strip(),
            "content": node.chunk_text,
            "search_text": node.search_text,
            "contextual_header": node.contextual_header,
            "length": len(node.chunk_text),
            "strategy": "hierarchical_parent_child",
            "chunk_type": "parent" if node.node_type == "ARTICLE" else "leaf",
            "node_type": node.node_type,
            "regulation_article": node.regulation_article,
            "regulation_clause": node.regulation_clause,
        }
        for node in nodes
    ]


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
    if strategy in ("hierarchical_parent_child", "parent_child"):
        return preview_chunks_hierarchical(text)
    if strategy == "article_first":
        return [
            {"title": title, "content": body, "length": len(body), "strategy": strategy, "chunk_type": "parent"}
            for title, body in _split_article_sections(text)
            if body
        ]
    if strategy == "sliding_window":
        return [
            {"title": f"윈도우 {idx}", "content": chunk, "length": len(chunk), "strategy": strategy, "chunk_type": "leaf"}
            for idx, chunk in enumerate(_window_split(text, chunk_size=700, overlap=120), start=1)
        ]
    # hybrid_policy
    out: list[dict[str, Any]] = []
    for title, body in _split_article_sections(text):
        if len(body) <= 900:
            out.append({"title": title, "content": body, "length": len(body), "strategy": strategy, "chunk_type": "parent"})
            continue
        for idx, chunk in enumerate(_window_split(body, chunk_size=650, overlap=100), start=1):
            out.append({"title": f"{title} · part {idx}", "content": chunk, "length": len(chunk), "strategy": strategy, "chunk_type": "leaf"})
    return out
