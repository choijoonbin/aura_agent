from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 계층적 청킹용 패턴
# [^\S\n]* : 수평 공백(스페이스/탭)만 소비 — \s*는 개행(\n)까지 소비해 다음 줄의 ①을 group(2)에 흡수하는 버그 발생
_ARTICLE_PATTERN = re.compile(r"^(제\s*\d+\s*조(?:\s*\([^)]+\))?)[^\S\n]*(.*)$", re.MULTILINE)
_CHAPTER_PATTERN = re.compile(r"^(제\s*\d+\s*장[^\n]*)", re.MULTILINE)
_SECTION_PATTERN = re.compile(r"^(제\s*\d+\s*절[^\n]*)", re.MULTILINE)
# 호(號) 마커 패턴: 줄 시작에서만 매칭 (문장 내부 "표 1. 참조" 오매칭 방지)
# 1. 2. 형식 + 1) 2) 형식 + 가. 나. 형식 + 가) 나) 형식 지원
_ITEM_PATTERN = re.compile(
    r"(?:^|\n)\s*(\d+\.\s+|\d+\)\s+|[가나다라마바사아자차카타파하]\.\s+|[가나다라마바사아자차카타파하]\)\s+)"
)


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
# 동의어/토큰 확장 (BM25·search_tokens용)
# ─────────────────────────────────────────────────────────────────────────────

_SYNONYM_MAP: dict[str, list[str]] = {
    "식대": ["식비", "식사비", "음식비", "식음료비"],
    "심야": ["야간", "야심", "23시", "22시", "자정"],
    "휴일": ["주말", "공휴일", "토요일", "일요일", "휴무일"],
    "한도": ["기준한도", "상한", "한도액", "허용한도", "초과"],
    "접대비": ["접대", "업무추진비", "외부 미팅비"],
    "교통비": ["출장비", "이동비", "택시비", "대중교통"],
    "승인": ["결재", "허가", "인가", "사전승인"],
    "증빙": ["영수증", "청구서", "카드전표"],
    "사적": ["개인적", "사적 사용", "업무 외"],
    "고위험": ["제한업종", "주류", "유흥", "도박"],
}

_JOSA_PATTERNS = ["은", "는", "이", "가", "을", "를", "의", "에", "에서", "으로", "로", "와", "과"]


def _expand_tokens(text: str) -> str:
    """
    텍스트에서 동의어 확장 + 조사 제거한 토큰 문자열 생성.
    search_tokens 컬럼 저장 및 BM25 확장 매칭용.
    """
    if not text or not text.strip():
        return ""
    tokens = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", text))
    expanded = set(tokens)
    for canonical, synonyms in _SYNONYM_MAP.items():
        if canonical in tokens:
            expanded.update(synonyms)
        for syn in synonyms:
            if syn in tokens or any(syn in t for t in tokens):
                expanded.add(canonical)
    for token in list(tokens):
        for josa in _JOSA_PATTERNS:
            if token.endswith(josa) and len(token) > len(josa) + 1:
                expanded.add(token[: -len(josa)])
                break
    for canonical, synonyms in _SYNONYM_MAP.items():
        if canonical in expanded:
            expanded.update(synonyms)
    return " ".join(sorted(expanded))


# ─────────────────────────────────────────────────────────────────────────────
# 계층적 청킹 (Parent-Child): hierarchical_parent_child 전략
# ─────────────────────────────────────────────────────────────────────────────


PARENT_MIN = 120  # 이 길이 미만인 ARTICLE은 다음 조문과 병합 (Fix 1 후 body에 ① 포함 → 기존 200에서 조정)


@dataclass
class ChunkNode:
    """계층적 청크 노드."""
    node_type: str  # "ARTICLE" | "CLAUSE" | "ITEM"
    regulation_article: str | None
    regulation_clause: str | None
    parent_title: str | None
    chunk_text: str
    search_text: str
    contextual_header: str = ""
    children: list["ChunkNode"] = field(default_factory=list)
    chunk_index: int = 0
    page_no: int = 1
    semantic_group: str = ""  # 장(章) 또는 장 > 절(節) 그룹 (예: "제1장 총칙", "제3장 경비 유형별 기준 > 제1절 식대·접대비")
    merged_with: str | None = None  # 병합된 조문 번호 (ARTICLE 병합 시)
    merged_articles: list[str] = field(default_factory=list)  # 병합에 포함된 원본 조문 번호 목록
    current_section: str = ""  # 절(節) 헤더 (검색/필터용 metadata 저장)
    regulation_item: str | None = None  # 호(號) 마커 (예: "1.", "가.") — ITEM 노드 전용
    parent_clause_chunk_index: int = -1  # ITEM 노드의 부모 CLAUSE chunk_index (DB 링크용)


def _extract_article_title(header_line: str) -> tuple[str, str]:
    """'제23조 (식대)' → ('제23조', '(식대)')."""
    m = re.match(r"(제\s*\d+\s*조)\s*(\([^)]+\))?(.*)$", header_line.strip())
    if not m:
        return header_line.strip(), ""
    article = m.group(1).strip()
    title = (m.group(2) or m.group(3) or "").strip()
    return article, title


def _split_into_clauses(article_body: str) -> list[tuple[str, str]]:
    """조문 본문을 항(①②③) 단위로만 분리. 반환: [(clause_marker, clause_text), ...]

    호(1. / 가.) 마커는 포함하지 않는다. _split_into_items()에서 ITEM 노드로 별도 처리.
    """
    pattern = re.compile(r"([①②③④⑤⑥⑦⑧⑨⑩])")
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


def _split_into_items(clause_text: str) -> list[tuple[str, str]]:
    """항(clause) 본문을 호(1. / 가.) 단위로 분리.

    반환: [(item_marker, item_text), ...]
    호 패턴이 없으면 빈 리스트 반환 (ITEM 노드 미생성).
    """
    matches = list(_ITEM_PATTERN.finditer(clause_text))
    if not matches:
        return []
    items: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        marker = match.group(1).strip()  # "1." 또는 "가."
        content_start = match.end()
        content_end = matches[i + 1].start() if i + 1 < len(matches) else len(clause_text)
        item_text = clause_text[content_start:content_end].strip()
        if item_text:
            items.append((marker, item_text))
    return items


def _build_contextual_header(
    article: str,
    title: str,
    chapter_context: str = "",
    section_context: str = "",
) -> str:
    """Contextual RAG: 각 청크 앞에 붙는 조문 맥락 요약."""
    parts = []
    if chapter_context:
        parts.append(chapter_context)
    if section_context:
        parts.append(section_context)
    if article:
        parts.append(article)
    if title:
        parts.append(title)
    if parts:
        return f"[{' > '.join(parts)}] "
    return ""


def _build_item_contextual_header(
    article: str,
    title: str,
    clause_marker: str,
    chapter_context: str = "",
    section_context: str = "",
) -> str:
    """ITEM 노드용 contextual header. 항(clause) 마커까지 포함.

    예: [제7장 계정별 세부 집행 기준 > 제23조 > (식대) > ③]
    """
    parts = []
    if chapter_context:
        parts.append(chapter_context)
    if section_context:
        parts.append(section_context)
    if article:
        parts.append(article)
    if title:
        parts.append(title)
    if clause_marker:
        parts.append(clause_marker)
    if parts:
        return f"[{' > '.join(parts)}] "
    return ""


def _merge_short_articles(
    articles: list[dict[str, Any]],
    parent_min: int = PARENT_MIN,
) -> list[dict[str, Any]]:
    """
    길이가 parent_min 미만인 ARTICLE을 바로 다음 ARTICLE과 병합한다.
    병합 시 clauses 원소를 (marker, clause_text, source_article) 3-튜플로 정규화해
    뒤 조문의 항이 앞 조문 번호로 귀속되는 왜곡을 방지한다.
    """
    if not articles:
        return articles

    def _normalize_clauses(raw_clauses: list[Any], source_article: str) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for item in raw_clauses or []:
            if not isinstance(item, tuple):
                continue
            if len(item) >= 3:
                marker = str(item[0] or "")
                clause_text = str(item[1] or "")
                src_article = str(item[2] or source_article or "")
            elif len(item) == 2:
                marker = str(item[0] or "")
                clause_text = str(item[1] or "")
                src_article = source_article
            else:
                continue
            out.append((marker, clause_text, src_article))
        return out

    merged: list[dict[str, Any]] = []
    skip_next = False

    for i, art in enumerate(articles):
        if skip_next:
            skip_next = False
            continue

        body = art.get("body") or ""
        body_len = len(body)
        has_next = i + 1 < len(articles)

        art_article = str(art.get("regulation_article") or "")
        art_clauses = _normalize_clauses(list(art.get("clauses") or []), art_article)

        if body_len < parent_min and has_next:
            next_art = articles[i + 1]
            next_article = str(next_art.get("regulation_article") or "")
            next_clauses = _normalize_clauses(list(next_art.get("clauses") or []), next_article)
            merged_title = f"{art.get('full_title') or art.get('regulation_article', '')} ~ {next_art.get('full_title') or next_art.get('regulation_article', '')}"
            # 조항 경계 마커를 삽입해 병합된 내용이 섞이지 않도록 시각적으로 구분
            next_boundary = f"[{next_art.get('full_title') or next_article}]"
            merged_body = body + f"\n\n{next_boundary}\n" + (next_art.get("body") or "")
            merged_clauses = art_clauses + next_clauses
            title_map = {
                art_article: str(art.get("full_title") or art_article),
                next_article: str(next_art.get("full_title") or next_article),
            }
            merged.append({
                "regulation_article": art_article,
                "full_title": merged_title,
                "article_header": merged_title,
                "body": merged_body,
                "clauses": merged_clauses,
                "contextual_header": art.get("contextual_header", ""),
                "current_chapter": art.get("current_chapter", ""),
                "current_section": art.get("current_section", ""),
                "semantic_group": art.get("semantic_group", ""),
                "merged_with": next_art.get("regulation_article"),
                "merged_articles": [a for a in [art_article, next_article] if a],
                "source_title_map": title_map,
            })
            skip_next = True
        elif body_len < parent_min and not has_next and merged:
            prev = merged[-1]
            boundary = f"[{art.get('full_title') or art_article}]"
            prev["body"] = (prev.get("body") or "") + f"\n\n{boundary}\n" + body
            prev["full_title"] = f"{prev.get('full_title', '')} ~ {art.get('full_title') or art_article}"
            prev["article_header"] = prev["full_title"]
            prev["clauses"] = list(prev.get("clauses") or []) + art_clauses
            prev["merged_articles"] = list(prev.get("merged_articles") or []) + ([art_article] if art_article else [])
            source_title_map = dict(prev.get("source_title_map") or {})
            if art_article:
                source_title_map[art_article] = str(art.get("full_title") or art_article)
            prev["source_title_map"] = source_title_map
        else:
            entry = dict(art)
            entry["clauses"] = art_clauses
            entry["merged_articles"] = [art_article] if art_article else []
            entry["source_title_map"] = {art_article: str(art.get("full_title") or art_article)} if art_article else {}
            merged.append(entry)

    return merged


def hierarchical_chunk(text: str) -> list[ChunkNode]:
    """
    규정집 텍스트를 조문-항/호 계층으로 분리.
    ARTICLE 노드(조문 전체) + CLAUSE 노드(항 단위). 단항 조문은 ARTICLE만.
    초단편( body < PARENT_MIN ) ARTICLE은 다음 조문과 병합 후 노드 생성.
    """
    articles: list[dict[str, Any]] = []

    # 장/절 컨텍스트를 유지한 블록으로 먼저 분할한 뒤, 블록 내부에서 조문을 추출한다.
    blocks: list[tuple[str, str, str]] = []
    current_chapter = ""
    current_section = ""
    buffer: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line and _CHAPTER_PATTERN.fullmatch(line):
            if buffer:
                blocks.append((current_chapter, current_section, "\n".join(buffer).strip()))
                buffer = []
            current_chapter = line
            current_section = ""
            continue
        if line and _SECTION_PATTERN.fullmatch(line):
            if buffer:
                blocks.append((current_chapter, current_section, "\n".join(buffer).strip()))
                buffer = []
            current_section = line
            continue
        buffer.append(raw_line)
    if buffer:
        blocks.append((current_chapter, current_section, "\n".join(buffer).strip()))

    for chapter_ctx, section_ctx, block_text in blocks:
        if not block_text:
            continue
        matches = list(_ARTICLE_PATTERN.finditer(block_text))
        if not matches:
            continue
        semantic_group = " > ".join([x for x in [chapter_ctx, section_ctx] if x]).strip()
        for idx, match in enumerate(matches):
            article_header = f"{match.group(1)} {match.group(2) or ''}".strip()
            body_start = match.end()
            body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(block_text)
            article_body = block_text[body_start:body_end].strip()

            article_num, article_title = _extract_article_title(article_header)
            full_title = f"{article_num} {article_title}".strip()
            contextual_header = _build_contextual_header(
                article_num,
                article_title,
                chapter_context=chapter_ctx,
                section_context=section_ctx,
            )
            clauses = _split_into_clauses(article_body)
            articles.append({
                "regulation_article": article_num,
                "full_title": full_title,
                "article_header": article_header,
                "body": article_body,
                "contextual_header": contextual_header,
                "current_chapter": chapter_ctx,
                "current_section": section_ctx,
                "semantic_group": semantic_group,
                "clauses": clauses,
            })

    # 초단편 ARTICLE 병합
    articles = _merge_short_articles(articles, parent_min=PARENT_MIN)

    # ChunkNode 변환
    nodes: list[ChunkNode] = []
    chunk_index = 0
    for art in articles:
        full_title = art.get("full_title") or art.get("regulation_article") or ""
        body = art.get("body") or ""
        article_header = art.get("article_header") or full_title
        contextual_header = art.get("contextual_header") or ""
        semantic_group = art.get("semantic_group") or ""
        regulation_article = art.get("regulation_article")
        merged_with = art.get("merged_with")
        merged_articles = list(art.get("merged_articles") or [])
        source_title_map = dict(art.get("source_title_map") or {})
        clauses = art.get("clauses") or []

        article_full_text = f"{article_header}\n{body}".strip()
        current_section = art.get("current_section") or ""
        article_node = ChunkNode(
            node_type="ARTICLE",
            regulation_article=regulation_article,
            regulation_clause=None,
            parent_title=full_title,
            chunk_text=article_full_text,
            search_text=body,
            contextual_header=contextual_header,
            chunk_index=chunk_index,
            semantic_group=semantic_group,
            merged_with=merged_with,
            merged_articles=merged_articles,
            current_section=current_section,
        )
        chunk_index += 1

        if len(clauses) >= 2:
            for clause_item in clauses:
                if isinstance(clause_item, tuple) and len(clause_item) >= 3:
                    marker, clause_text, source_article = clause_item[:3]
                elif isinstance(clause_item, tuple) and len(clause_item) == 2:
                    marker, clause_text = clause_item
                    source_article = regulation_article
                else:
                    continue
                src_article = str(source_article or regulation_article or "")
                source_title = str(source_title_map.get(src_article) or full_title or src_article)
                source_contextual_header = _build_contextual_header(
                    src_article,
                    source_title,
                    chapter_context=art.get("current_chapter") or "",
                    section_context=art.get("current_section") or "",
                )
                clause_chunk_text = f"{source_contextual_header}{marker} {clause_text}".strip()
                clause_node = ChunkNode(
                    node_type="CLAUSE",
                    regulation_article=src_article or regulation_article,
                    regulation_clause=marker or None,
                    parent_title=source_title,
                    chunk_text=clause_chunk_text,
                    search_text=clause_text,
                    contextual_header=source_contextual_header,
                    chunk_index=chunk_index,
                    semantic_group=semantic_group,
                    current_section=current_section,
                )
                chunk_index += 1
                article_node.children.append(clause_node)
                nodes.append(clause_node)

                # ITEM 노드 생성: 호(1./가.) 단위 분리
                # marker 여부와 무관하게 항상 실행 — 빈 marker clause에도 1./가. 항목이 존재할 수 있음
                items = _split_into_items(clause_text)
                if items:
                    for item_marker, item_text in items:
                        item_contextual_header = _build_item_contextual_header(
                            src_article,
                            source_title,
                            clause_marker=marker,
                            chapter_context=art.get("current_chapter") or "",
                            section_context=art.get("current_section") or "",
                        )
                        item_chunk_text = f"{item_contextual_header}{item_marker} {item_text}".strip()
                        item_node = ChunkNode(
                            node_type="ITEM",
                            regulation_article=src_article or regulation_article,
                            regulation_clause=marker or None,
                            regulation_item=item_marker,
                            parent_clause_chunk_index=clause_node.chunk_index,
                            parent_title=source_title,
                            chunk_text=item_chunk_text,
                            search_text=item_text,
                            contextual_header=item_contextual_header,
                            chunk_index=chunk_index,
                            semantic_group=semantic_group,
                            current_section=current_section,
                        )
                        chunk_index += 1
                        clause_node.children.append(item_node)
                        nodes.append(item_node)

            # ARTICLE을 모든 자손(CLAUSE + ITEM) 앞에 삽입
            total_desc = len(article_node.children) + sum(
                len(c.children) for c in article_node.children
            )
            nodes.insert(len(nodes) - total_desc, article_node)
        else:
            nodes.append(article_node)

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
            "chunk_type": (
                "parent" if node.node_type == "ARTICLE"
                else "leaf" if node.node_type == "ITEM"
                else "child"  # CLAUSE는 중간 계층
            ),
            "node_type": node.node_type,
            "regulation_article": node.regulation_article,
            "regulation_clause": node.regulation_clause,
            "regulation_item": getattr(node, "regulation_item", None),
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
