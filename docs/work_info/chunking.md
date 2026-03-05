# Cursor 작업 프롬프트 — RAG 청킹·검색 파이프라인 전면 고도화

## 현황 진단 (소스 분석 결과)

### 발견된 결정적 문제

소스 전체를 분석한 결과, **가장 심각한 문제는 아키텍처 불일치**다:

```
[DB 스키마]  rag_chunk 테이블
  embedding public.vector(1536)  ← OpenAI 1536차원 벡터 컬럼 존재
  search_tsv tsvector             ← BM25 검색용 tsvector 컬럼 존재
  ix_rag_chunk_search_tsv (GIN)  ← tsvector GIN 인덱스 존재

[실제 검색]  services/policy_service.py
  → lower(coalesce(chunk_text, '')) LIKE :p0  ← 단순 LIKE 검색만 사용
  → embedding 컬럼: 전혀 사용 안 함  ← pgvector가 있는데 사용 안 함
  → search_tsv 컬럼: 전혀 사용 안 함  ← tsvector GIN 인덱스가 있는데 사용 안 함
```

**결론: DB에는 pgvector + tsvector 인프라가 이미 갖춰져 있으나, 실제 검색은 단순 LIKE에만 의존하고 있다.**

### 현재 청킹 전략 현황

`services/rag_chunk_lab_service.py`에 3가지 전략이 정의됨:

| 전략 | 방식 | 문제점 |
|------|------|--------|
| `article_first` | 조문 단위(`제N조`) 분리 | 긴 조문이 분리 없이 전체가 1청크 → 검색 정밀도 저하 |
| `sliding_window` | 700자 + 120자 overlap | 조문 경계 무시, 제목 없는 청크 생성 → 문맥 단절 |
| `hybrid_policy` | article_first + sliding_window | 조문당 900자 초과 시 분할하나 **조문 제목이 각 청크에 복사 안 됨** |

`policy_service.py`의 실제 검색은 위 3가지 청킹과 무관하게 단순 LIKE를 사용.

### 실제 쿼리에서 사용되는 점수 계산

```sql
-- 현재 lexical_score 계산 방식
(case when lower(chunk_text) like :p0 then 3 else 0 end) +  -- chunk_text: 가중치 3
(case when lower(parent_title) like :p0 then 5 else 0 end) + -- parent_title: 가중치 5
(case when lower(regulation_article) like :p0 then 7 else 0 end) + -- article: 가중치 7
(case when lower(regulation_clause) like :p0 then 4 else 0 end)   -- clause: 가중치 4
```

이 방식의 문제:
- "식대"라는 단어가 있으면 +3점, 없으면 0점 — 의미적 유사성 0%
- "야간 식대"와 "심야 식대"는 전혀 다른 키워드로 취급됨
- 단어 순서, 문맥, 동의어를 전혀 반영하지 못함

---

## 목표 아키텍처

```
[현재]  텍스트 → 단순 LIKE 검색 → lexical 점수 → rerank(cross-encoder 선택)
[목표]  텍스트 → ① 계층적 청킹(조문-항-호)
               → ② Hybrid 검색 (BM25 tsvector + Dense Vector)
               → ③ RRF 융합 (Reciprocal Rank Fusion)
               → ④ Cross-Encoder 재정렬 (한국어 특화)
               → ⑤ Contextual 청크 보강 (부모 조문 요약 prepend)
```

---

## 기술 선택 근거

### 임베딩 모델: `jhgan/ko-sroberta-multitask`

**선택 이유:**
- 한국어 법령/규정 텍스트에 특화된 sentence-transformers 모델
- 768차원 (OpenAI ada-002의 1536차원 대비 절반 비용)
- ko-sroberta는 한국어 NLI + STS로 fine-tuned → 규정 조항 유사도에 직접 적합
- HuggingFace에서 무료, 로컬 실행 가능 (API 비용 없음)
- 대안: `snunlp/KR-ELECTRA-discriminator` (분류 특화), `BM-K/KoSimCSE-roberta` (SimCSE)
- OpenAI `text-embedding-ada-002`는 성능은 우수하나 API 비용 발생, 로컬 불가

### Cross-Encoder: `Dongjin-kr/ko-reranker`

**선택 이유:**
- 한국어 특화 cross-encoder (현재 코드의 `ms-marco-MiniLM-L6-v2`는 영어 전용)
- 규정 텍스트 쿼리-패시지 관련도 정확도 대폭 향상
- HuggingFace 공개 모델, 무료

### 검색 방식: Hybrid (BM25 + Dense) with RRF

**선택 이유:**
- BM25: 정확한 조문 번호(제23조), 법적 용어("식대", "한도") 매칭에 강함
- Dense: "야간 식사"→"심야 식대", "초과"→"한도 위반" 동의어/의미 검색
- RRF(Reciprocal Rank Fusion): 두 결과를 단순 가중합이 아닌 순위 기반으로 융합 → 안정적
- pgvector가 이미 DB에 설치되어 있으므로 추가 인프라 없음

### 청킹: 계층적 Parent-Child

**선택 이유:**
- 규정집은 `제N장 → 제N조 → ① ② ③` 구조가 명확 → 계층 경계가 자연스러운 청크 단위
- Parent(조문 전체) + Child(항/호 단위) 이중 저장으로 정밀도/재현율 동시 확보
- `rag_chunk` 테이블에 이미 `parent_id`, `node_type(ARTICLE/CLAUSE/PARAGRAPH)`, `parent_title` 컬럼 존재 → 스키마 변경 없음

### pgvector 유지 vs 대안 검토

| 옵션 | 장점 | 단점 | 판정 |
|------|------|------|------|
| **pgvector (현재 DB)** | 스키마 이미 존재, 추가 인프라 없음 | 수백만 벡터 수준에선 느림 | ✅ **채택** (규정집 규모 적합) |
| Qdrant | 고성능, 필터링 강력 | 별도 서버 필요, 인프라 추가 | ⛔ Over-engineering |
| Weaviate | 멀티모달, GraphQL | 복잡한 설정, 오버헤드 큼 | ⛔ 불필요 |
| Chroma | 설치 간단 | 프로덕션 안정성 미흡 | ⛔ PoC 수준 |

→ **현재 규정집 규모(수백~수천 청크)에서는 pgvector로 충분. 인프라 변경 없이 고도화 가능.**

---

## 작업 범위 (수정/신규 파일 목록)

| 파일 | 작업 |
|------|------|
| `services/rag_chunk_lab_service.py` | ① 계층적 청킹 함수 전면 재설계 |
| `services/policy_service.py` | ② Hybrid 검색 (BM25 + Dense) + RRF 융합 |
| `services/retrieval_quality.py` | ③ Cross-Encoder 한국어 모델 교체 |
| `services/chunking_pipeline.py` | ④ 신규 — Contextual 청크 저장 파이프라인 |
| `requirements.txt` | ⑤ 의존성 추가 |
| `tests/test_rag_chunking.py` | ⑥ 신규 — 청킹·검색 단위 테스트 |

---

## 상세 구현 명세

---

### ① `services/rag_chunk_lab_service.py` — 계층적 청킹 재설계

기존 3개 전략(`article_first`, `sliding_window`, `hybrid_policy`)은 **유지**하고,
신규 전략 `hierarchical_parent_child`를 추가한다. (하위 호환)

```python
# ─────────────────────────────────────────────────────────────────────────────
# 신규 추가: 계층적 청킹 (Parent-Child)
# ─────────────────────────────────────────────────────────────────────────────

import re
from dataclasses import dataclass, field
from typing import Any

# 한국 법령 조문 패턴
_ARTICLE_PATTERN = re.compile(r"^(제\s*\d+\s*조(?:\s*\([^)]+\))?)\s*(.*)$", re.MULTILINE)
_CLAUSE_PATTERN = re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩]|^\d+\.\s|^[가-힣]\.\s", re.MULTILINE)
_CHAPTER_PATTERN = re.compile(r"^(제\s*\d+\s*장[^\n]*)", re.MULTILINE)
_SECTION_PATTERN = re.compile(r"^(제\s*\d+\s*절[^\n]*)", re.MULTILINE)


@dataclass
class ChunkNode:
    """계층적 청크 노드."""
    node_type: str          # "ARTICLE" | "CLAUSE" | "PARAGRAPH"
    regulation_article: str | None   # "제23조"
    regulation_clause: str | None    # "①", "1.", "가."
    parent_title: str | None         # 조문 제목 (예: "(식대)")
    chunk_text: str                  # 실제 저장되는 청크 텍스트
    search_text: str                 # 임베딩/검색용 정제 텍스트 (prefix 없음)
    contextual_header: str = ""      # Contextual RAG용 조문 요약 prefix
    children: list["ChunkNode"] = field(default_factory=list)
    chunk_index: int = 0
    page_no: int = 1


def _extract_article_title(header_line: str) -> tuple[str, str]:
    """
    "제23조 (식대)" → ("제23조", "(식대)")
    "제23조(식대)" → ("제23조", "(식대)")
    "제23조" → ("제23조", "")
    """
    m = re.match(r"(제\s*\d+\s*조)\s*(\([^)]+\))?(.*)$", header_line.strip())
    if not m:
        return header_line.strip(), ""
    article = m.group(1).strip()
    title = (m.group(2) or m.group(3) or "").strip()
    return article, title


def _split_into_clauses(article_body: str) -> list[tuple[str, str]]:
    """
    조문 본문을 항/호 단위로 분리.
    반환: [(clause_marker, clause_text), ...]
    예: [("①", "업무상 식대는..."), ("②", "다음 각 호의...")]
    """
    # 원형 숫자 ①②③... 또는 숫자+점 "1. 2. 3." 패턴으로 분리
    pattern = re.compile(r"([①②③④⑤⑥⑦⑧⑨⑩]|\d+\.\s|[가나다라마바사아자차카타파하]\.\s)")
    parts = pattern.split(article_body)
    clauses: list[tuple[str, str]] = []
    marker = ""
    buffer: list[str] = []
    for part in parts:
        if pattern.fullmatch(part.strip()):
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
    """
    Contextual RAG: 각 청크 앞에 붙는 조문 맥락 요약.
    "이 청크는 [장] [조문] [제목]에 관한 내용입니다."
    """
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
    핵심 함수: 규정집 텍스트를 조문-항/호 계층으로 분리.

    출력 구조:
    - ARTICLE 노드: 조문 전체 텍스트 (parent 역할)
    - CLAUSE 노드: 각 항(①②③...) 단위 (child 역할)
    - 단항 조문은 CLAUSE 없이 ARTICLE 단독

    각 노드의 chunk_text는 독립적으로 검색 가능해야 하므로,
    CLAUSE 노드에도 부모 조문 제목을 prefix로 포함(contextual_header).
    """
    nodes: list[ChunkNode] = []
    chunk_index = 0
    current_chapter = ""

    # 장(章) 분리
    chapter_splits = _CHAPTER_PATTERN.split(text)

    for part in chapter_splits:
        if _CHAPTER_PATTERN.fullmatch(part.strip()):
            current_chapter = part.strip()
            continue

        # 조문 분리
        article_splits = _ARTICLE_PATTERN.split(part)
        i = 0
        while i < len(article_splits):
            segment = article_splits[i].strip()
            if not segment:
                i += 1
                continue

            # 조문 헤더 감지
            if _ARTICLE_PATTERN.fullmatch(segment):
                article_header = segment
                i += 1
                article_body = article_splits[i].strip() if i < len(article_splits) else ""
                i += 1

                article_num, article_title = _extract_article_title(article_header)
                full_title = f"{article_num} {article_title}".strip()
                contextual_header = _build_contextual_header(article_num, article_title, current_chapter)

                # ARTICLE 노드 (조문 전체 — parent로 저장)
                article_full_text = f"{article_header}\n{article_body}".strip()
                article_node = ChunkNode(
                    node_type="ARTICLE",
                    regulation_article=article_num,
                    regulation_clause=None,
                    parent_title=full_title,
                    chunk_text=article_full_text,
                    search_text=article_body,   # prefix(조문번호) 제거한 순수 본문
                    contextual_header=contextual_header,
                    chunk_index=chunk_index,
                )
                chunk_index += 1

                # 항/호 분리 시도
                clauses = _split_into_clauses(article_body)
                if len(clauses) >= 2:
                    for marker, clause_text in clauses:
                        # CLAUSE 노드: 항 단위 (child)
                        # chunk_text에 조문 제목 포함 → 독립 검색 시 맥락 유지
                        clause_chunk_text = (
                            f"{contextual_header}{marker} {clause_text}".strip()
                        )
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
                i += 1

    return nodes


def preview_chunks_hierarchical(text: str) -> list[dict[str, Any]]:
    """
    UI 미리보기용. `preview_chunks()` 기존 인터페이스와 동일한 형태로 반환.
    """
    nodes = hierarchical_chunk(text)
    return [
        {
            "title": f"{node.regulation_article or ''} {node.parent_title or ''} [{node.node_type}]".strip(),
            "content": node.chunk_text,
            "search_text": node.search_text,
            "contextual_header": node.contextual_header,
            "length": len(node.chunk_text),
            "strategy": "hierarchical_parent_child",
            "node_type": node.node_type,
            "regulation_article": node.regulation_article,
            "regulation_clause": node.regulation_clause,
        }
        for node in nodes
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 기존 preview_chunks() 함수에 hierarchical_parent_child 전략 분기 추가
# ─────────────────────────────────────────────────────────────────────────────
# 기존 함수 내부에 아래 분기를 추가 (기존 3개 전략은 그대로 유지):
#
# def preview_chunks(text: str, strategy: str) -> list[dict[str, Any]]:
#     if strategy == "hierarchical_parent_child":       # ← 신규 분기 추가
#         return preview_chunks_hierarchical(text)
#     if strategy == "article_first":
#         ...
```

---

### ② `services/chunking_pipeline.py` — 신규 파일: 임베딩 생성 + DB 저장 파이프라인

```python
"""
계층적 청킹 → 임베딩 생성 → pgvector 저장 파이프라인.

임베딩 모델: jhgan/ko-sroberta-multitask (HuggingFace, 768차원)
저장 대상: dwp_aura.rag_chunk (embedding 컬럼, search_text 컬럼)
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.rag_chunk_lab_service import hierarchical_chunk, ChunkNode
from utils.config import settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 임베딩 모델 로더 (싱글톤)
# 모델: jhgan/ko-sroberta-multitask
# 차원: 768  ← DB의 embedding vector(1536)와 차원 불일치 주의!
#   → DB 마이그레이션 필요: ALTER TABLE rag_chunk ALTER COLUMN embedding TYPE vector(768)
#   → 또는 별도 컬럼(embedding_ko vector(768)) 추가
# ─────────────────────────────────────────────────────────────────────────────

_EMBEDDING_MODEL: Any = None
_EMBEDDING_DIM = 768


def get_embedding_model():
    """
    ko-sroberta-multitask 모델 싱글톤 로더.
    sentence-transformers 미설치 시 None 반환 (graceful degradation).
    """
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is not None:
        return _EMBEDDING_MODEL
    try:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading ko-sroberta-multitask embedding model...")
        _EMBEDDING_MODEL = SentenceTransformer("jhgan/ko-sroberta-multitask")
        logger.info(f"Embedding model loaded. Dim: {_EMBEDDING_MODEL.get_sentence_embedding_dimension()}")
        return _EMBEDDING_MODEL
    except ImportError:
        logger.warning("sentence-transformers not installed. Embedding will be skipped.")
        return None
    except Exception as e:
        logger.error(f"Failed to load embedding model: {e}")
        return None


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """
    텍스트 목록을 배치 임베딩. 실패 시 None 반환.
    search_text(prefix 제거된 순수 본문)를 임베딩 대상으로 사용.
    """
    model = get_embedding_model()
    if model is None:
        return None
    try:
        vectors = model.encode(texts, batch_size=32, show_progress_bar=False, normalize_embeddings=True)
        return [v.tolist() for v in vectors]
    except Exception as e:
        logger.error(f"Embedding failed: {e}")
        return None


def save_hierarchical_chunks(
    db: Session,
    doc_id: int,
    nodes: list[ChunkNode],
    *,
    version: str | None = None,
    effective_from: str | None = None,
    effective_to: str | None = None,
) -> dict[str, Any]:
    """
    계층적 청크 노드 목록을 rag_chunk 테이블에 저장.

    저장 전략:
    1. 기존 청크 비활성화 (is_active=false)
    2. ARTICLE 노드 먼저 저장 (parent_id 확보)
    3. CLAUSE 노드 저장 (parent_id = 해당 ARTICLE의 chunk_id)
    4. 임베딩 생성 후 embedding 컬럼 업데이트
    5. tsvector 업데이트 (search_tsv)
    """
    tenant_id = settings.default_tenant_id

    # Step 1: 기존 청크 비활성화
    db.execute(
        text("UPDATE dwp_aura.rag_chunk SET is_active = false WHERE doc_id = :doc_id AND tenant_id = :tid"),
        {"doc_id": doc_id, "tid": tenant_id},
    )

    # Step 2: ARTICLE 노드 먼저 저장 (parent_id 없음)
    article_nodes = [n for n in nodes if n.node_type == "ARTICLE"]
    clause_nodes = [n for n in nodes if n.node_type != "ARTICLE"]

    # article_key → chunk_id 매핑 (CLAUSE 노드의 parent_id 설정용)
    article_chunk_id_map: dict[str, int] = {}

    insert_sql = text("""
        INSERT INTO dwp_aura.rag_chunk (
            tenant_id, doc_id, chunk_text, search_text,
            regulation_article, regulation_clause,
            parent_title, node_type, parent_id,
            chunk_level, chunk_index, page_no,
            version, effective_from, effective_to,
            is_active, created_at
        ) VALUES (
            :tenant_id, :doc_id, :chunk_text, :search_text,
            :regulation_article, :regulation_clause,
            :parent_title, :node_type, :parent_id,
            :chunk_level, :chunk_index, :page_no,
            :version, :effective_from, :effective_to,
            true, now()
        ) RETURNING chunk_id
    """)

    saved_chunks: list[dict[str, Any]] = []

    for node in article_nodes:
        row = db.execute(insert_sql, {
            "tenant_id": tenant_id,
            "doc_id": doc_id,
            "chunk_text": node.chunk_text,
            "search_text": node.search_text,
            "regulation_article": node.regulation_article,
            "regulation_clause": node.regulation_clause,
            "parent_title": node.parent_title,
            "node_type": "ARTICLE",
            "parent_id": None,
            "chunk_level": "root",
            "chunk_index": node.chunk_index,
            "page_no": node.page_no,
            "version": version,
            "effective_from": effective_from,
            "effective_to": effective_to,
        }).fetchone()
        chunk_id = row[0]
        article_chunk_id_map[node.regulation_article or str(node.chunk_index)] = chunk_id
        saved_chunks.append({"chunk_id": chunk_id, "search_text": node.search_text, "node_type": "ARTICLE"})

    for node in clause_nodes:
        parent_id = article_chunk_id_map.get(node.regulation_article or "")
        row = db.execute(insert_sql, {
            "tenant_id": tenant_id,
            "doc_id": doc_id,
            "chunk_text": node.chunk_text,
            "search_text": node.search_text,
            "regulation_article": node.regulation_article,
            "regulation_clause": node.regulation_clause,
            "parent_title": node.parent_title,
            "node_type": "CLAUSE",
            "parent_id": parent_id,
            "chunk_level": "child",
            "chunk_index": node.chunk_index,
            "page_no": node.page_no,
            "version": version,
            "effective_from": effective_from,
            "effective_to": effective_to,
        }).fetchone()
        chunk_id = row[0]
        saved_chunks.append({"chunk_id": chunk_id, "search_text": node.search_text, "node_type": "CLAUSE"})

    # Step 3: 임베딩 생성 및 업데이트
    #
    # ⚠️ 중요: DB의 embedding 컬럼이 vector(1536)로 선언되어 있으나
    #   ko-sroberta-multitask는 768차원을 출력한다.
    #   아래 두 가지 중 하나를 선택해서 실행해야 한다:
    #
    #   [방법 A] DB 마이그레이션 (권장):
    #     ALTER TABLE dwp_aura.rag_chunk ALTER COLUMN embedding TYPE vector(768)
    #       USING NULL;
    #
    #   [방법 B] 별도 컬럼 추가 (기존 데이터 보존):
    #     ALTER TABLE dwp_aura.rag_chunk
    #       ADD COLUMN IF NOT EXISTS embedding_ko vector(768);
    #
    # 아래 코드는 [방법 A] 적용을 가정. 방법 B 선택 시 컬럼명을 embedding_ko로 변경.
    #
    embed_column = "embedding"   # 방법 B 선택 시 "embedding_ko"로 변경
    search_texts = [c["search_text"] for c in saved_chunks]
    vectors = embed_texts(search_texts)

    if vectors:
        for chunk, vector in zip(saved_chunks, vectors):
            db.execute(
                text(f"UPDATE dwp_aura.rag_chunk SET {embed_column} = :vec WHERE chunk_id = :cid"),
                {"vec": str(vector), "cid": chunk["chunk_id"]},
            )
        logger.info(f"Embedding saved for {len(saved_chunks)} chunks")
    else:
        logger.warning("Embedding skipped (model unavailable)")

    # Step 4: tsvector 업데이트 (BM25용)
    # PostgreSQL의 to_tsvector('simple', ...) 사용
    # 한국어는 'simple' config 사용 (형태소 분석기 없이 공백 분리)
    for chunk in saved_chunks:
        db.execute(
            text("""
                UPDATE dwp_aura.rag_chunk
                SET search_tsv = to_tsvector('simple', coalesce(search_text, chunk_text, ''))
                WHERE chunk_id = :cid
            """),
            {"cid": chunk["chunk_id"]},
        )

    db.commit()

    return {
        "doc_id": doc_id,
        "total_chunks": len(saved_chunks),
        "article_chunks": len(article_nodes),
        "clause_chunks": len(clause_nodes),
        "embedding_saved": vectors is not None,
    }


def run_chunking_pipeline(
    db: Session,
    doc_id: int,
    raw_text: str,
    *,
    version: str | None = None,
    effective_from: str | None = None,
    effective_to: str | None = None,
) -> dict[str, Any]:
    """전체 파이프라인 실행: 청킹 → 임베딩 → 저장."""
    nodes = hierarchical_chunk(raw_text)
    if not nodes:
        return {"error": "청킹 결과가 없습니다. 텍스트 형식을 확인하세요."}
    return save_hierarchical_chunks(
        db, doc_id, nodes,
        version=version,
        effective_from=effective_from,
        effective_to=effective_to,
    )
```

---

### ③ `services/policy_service.py` — Hybrid 검색 (BM25 + Dense) + RRF

기존 `search_policy_chunks()` 함수를 아래 3단계 검색으로 교체한다.
기존 LIKE 검색 기반 함수는 `_search_lexical_legacy()`로 이름을 바꾸어 **하위 호환 유지**.

```python
# ─────────────────────────────────────────────────────────────────────────────
# 신규 추가 함수들 (기존 함수는 유지하고 search_policy_chunks만 교체)
# ─────────────────────────────────────────────────────────────────────────────

def _search_bm25(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    limit: int = 20,
    effective_date: Any = None,
) -> list[dict[str, Any]]:
    """
    BM25 검색: PostgreSQL tsvector GIN 인덱스 활용.
    search_tsv 컬럼의 GIN 인덱스(ix_rag_chunk_search_tsv)를 사용.
    LIKE 대신 @@ 연산자로 전문 검색 → 성능 및 관련도 대폭 향상.
    """
    keywords = build_policy_keywords(body_evidence)
    if not keywords:
        return []

    # tsquery 구성: 키워드를 | (OR)로 연결
    # "식대 | 휴일 | 주말" 형태
    ts_query = " | ".join(
        f"'{kw}':*" for kw in keywords[:15] if kw.strip()
    )
    if not ts_query:
        return []

    sql = text("""
        SELECT
            chunk_id, doc_id, regulation_article, regulation_clause,
            parent_title, chunk_text, search_text, node_type, parent_id,
            version, effective_from, effective_to, page_no, chunk_index,
            ts_rank_cd(search_tsv, query) AS bm25_score
        FROM dwp_aura.rag_chunk,
             to_tsquery('simple', :ts_query) AS query
        WHERE tenant_id = :tenant_id
          AND is_active = true
          AND search_tsv @@ query
          AND (:effective_date IS NULL OR coalesce(effective_from, :effective_date) <= :effective_date)
          AND (:effective_date IS NULL OR coalesce(effective_to, :effective_date) >= :effective_date)
        ORDER BY bm25_score DESC
        LIMIT :limit
    """)
    rows = db.execute(sql, {
        "tenant_id": settings.default_tenant_id,
        "ts_query": ts_query,
        "effective_date": effective_date,
        "limit": limit,
    }).mappings().all()
    return [dict(row) for row in rows]


def _search_dense(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    limit: int = 20,
    effective_date: Any = None,
    embed_column: str = "embedding",
) -> list[dict[str, Any]]:
    """
    Dense 벡터 검색: pgvector <=> (cosine distance) 연산자 활용.
    쿼리 텍스트를 임베딩하여 가장 유사한 청크를 검색.

    embed_column: "embedding" (방법A) 또는 "embedding_ko" (방법B)
    """
    try:
        from services.chunking_pipeline import embed_texts
    except ImportError:
        return []

    # 쿼리 텍스트 구성: 전표의 맥락을 자연어로 변환
    keywords = build_policy_keywords(body_evidence)
    case_type = body_evidence.get("case_type") or ""
    merchant = body_evidence.get("merchantName") or ""
    query_text = f"{case_type} {merchant} {' '.join(keywords[:10])}".strip()

    if not query_text:
        return []

    vectors = embed_texts([query_text])
    if not vectors:
        return []

    query_vector = vectors[0]

    # pgvector cosine distance: <=> 연산자
    # 1 - cosine_similarity 이므로 낮을수록 유사
    sql = text(f"""
        SELECT
            chunk_id, doc_id, regulation_article, regulation_clause,
            parent_title, chunk_text, search_text, node_type, parent_id,
            version, effective_from, effective_to, page_no, chunk_index,
            1 - ({embed_column} <=> :query_vec::vector) AS dense_score
        FROM dwp_aura.rag_chunk
        WHERE tenant_id = :tenant_id
          AND is_active = true
          AND {embed_column} IS NOT NULL
          AND (:effective_date IS NULL OR coalesce(effective_from, :effective_date) <= :effective_date)
          AND (:effective_date IS NULL OR coalesce(effective_to, :effective_date) >= :effective_date)
        ORDER BY {embed_column} <=> :query_vec::vector
        LIMIT :limit
    """)
    rows = db.execute(sql, {
        "tenant_id": settings.default_tenant_id,
        "query_vec": str(query_vector),
        "effective_date": effective_date,
        "limit": limit,
    }).mappings().all()
    return [dict(row) for row in rows]


def _reciprocal_rank_fusion(
    bm25_results: list[dict[str, Any]],
    dense_results: list[dict[str, Any]],
    *,
    k: int = 60,
    bm25_weight: float = 0.5,
    dense_weight: float = 0.5,
) -> list[dict[str, Any]]:
    """
    Reciprocal Rank Fusion (RRF):
    두 검색 결과를 순위 기반으로 융합.

    RRF_score(d) = Σ weight / (k + rank(d))
    k=60: 표준 RRF 상수 (논문 권장값)

    단순 가중합보다 안정적: 점수 스케일이 달라도 순위 기반이므로 공정한 합산.
    """
    scores: dict[int, float] = {}
    chunk_data: dict[int, dict[str, Any]] = {}

    for rank, item in enumerate(bm25_results, start=1):
        cid = item.get("chunk_id")
        if cid is None:
            continue
        scores[cid] = scores.get(cid, 0.0) + bm25_weight / (k + rank)
        chunk_data[cid] = {**item, "bm25_rank": rank, "bm25_score": item.get("bm25_score", 0)}

    for rank, item in enumerate(dense_results, start=1):
        cid = item.get("chunk_id")
        if cid is None:
            continue
        scores[cid] = scores.get(cid, 0.0) + dense_weight / (k + rank)
        if cid not in chunk_data:
            chunk_data[cid] = item
        chunk_data[cid]["dense_rank"] = rank
        chunk_data[cid]["dense_score"] = item.get("dense_score", 0)

    # RRF 점수로 정렬
    ranked = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)
    result = []
    for cid in ranked:
        item = dict(chunk_data[cid])
        item["rrf_score"] = round(scores[cid], 6)
        result.append(item)
    return result


def search_policy_chunks(
    db: Session,
    body_evidence: dict[str, Any],
    limit: int = 5,
) -> list[dict[str, Any]]:
    """
    메인 검색 함수 (기존 인터페이스 완전 유지).

    검색 파이프라인:
    1. BM25 (tsvector @@ tsquery) — 정확한 키워드 매칭
    2. Dense (pgvector cosine) — 의미 유사도 검색
    3. RRF 융합 — 두 결과를 순위 기반 통합
    4. Contextual 보강 — Parent 조문 청크로 문맥 확장
    5. Cross-Encoder 재정렬 — 한국어 reranker
    """
    from datetime import date

    effective_date = None
    occurred_at = body_evidence.get("occurredAt")
    if occurred_at:
        try:
            effective_date = date.fromisoformat(str(occurred_at)[:10])
        except Exception:
            pass

    candidate_limit = max(limit * 6, 20)

    # ── Step 1: BM25 검색 ──────────────────────────────────────────────────
    bm25_results = _search_bm25(
        db, body_evidence, limit=candidate_limit, effective_date=effective_date
    )

    # ── Step 2: Dense 검색 ────────────────────────────────────────────────
    dense_results = _search_dense(
        db, body_evidence, limit=candidate_limit, effective_date=effective_date
    )

    # ── Step 3: RRF 융합 ──────────────────────────────────────────────────
    if bm25_results and dense_results:
        fused = _reciprocal_rank_fusion(bm25_results, dense_results, k=60)
    elif bm25_results:
        # Dense 검색 불가(임베딩 미생성) → BM25만 사용
        fused = sorted(bm25_results, key=lambda x: x.get("bm25_score", 0), reverse=True)
    else:
        # 둘 다 없으면 legacy LIKE 검색으로 fallback
        fused = _search_lexical_legacy(db, body_evidence, limit=candidate_limit, effective_date=effective_date)

    # ── Step 4: Contextual 보강 (CLAUSE → ARTICLE parent 포함) ───────────
    enriched = _enrich_with_parent_context(db, fused[:candidate_limit])

    # ── Step 5: Cross-Encoder 재정렬 ──────────────────────────────────────
    keywords = build_policy_keywords(body_evidence)
    query_str = " ".join(keywords[:12])
    try:
        from services.retrieval_quality import rerank_with_cross_encoder
        enriched = rerank_with_cross_encoder(enriched, query_str)
    except Exception:
        pass

    # ── 반환 형식 정규화 (기존 인터페이스 호환) ───────────────────────────
    results = []
    for item in enriched[:limit]:
        results.append({
            "doc_id": item.get("doc_id"),
            "article": item.get("regulation_article"),
            "clause": item.get("regulation_clause"),
            "parent_title": item.get("parent_title"),
            "chunk_text": item.get("chunk_text"),
            "version": item.get("version"),
            "effective_from": str(item.get("effective_from")) if item.get("effective_from") else None,
            "effective_to": str(item.get("effective_to")) if item.get("effective_to") else None,
            "chunk_ids": [item.get("chunk_id")] if item.get("chunk_id") else [],
            "context_chunk_ids": item.get("context_chunk_ids", []),
            "retrieval_score": item.get("cross_encoder_score") or item.get("rrf_score") or item.get("bm25_score") or 0,
            "source_strategy": "hybrid_bm25_dense_rrf",
            # 디버깅용 점수 분해
            "score_detail": {
                "bm25_score": item.get("bm25_score"),
                "dense_score": item.get("dense_score"),
                "rrf_score": item.get("rrf_score"),
                "cross_encoder_score": item.get("cross_encoder_score"),
            },
        })
    return results


def _enrich_with_parent_context(
    db: Session,
    chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    CLAUSE 노드에 대해 부모 ARTICLE 청크의 텍스트를 prepend.
    단독으로 검색된 항(①②③)도 조문 전체 맥락에서 이해 가능하게 보강.
    """
    parent_ids = [
        c.get("parent_id") for c in chunks
        if c.get("node_type") == "CLAUSE" and c.get("parent_id")
    ]
    if not parent_ids:
        return chunks

    parent_sql = text("""
        SELECT chunk_id, chunk_text, regulation_article, parent_title
        FROM dwp_aura.rag_chunk
        WHERE chunk_id = ANY(:ids) AND tenant_id = :tid
    """)
    rows = db.execute(parent_sql, {
        "ids": parent_ids,
        "tid": settings.default_tenant_id,
    }).mappings().all()
    parent_map = {row["chunk_id"]: dict(row) for row in rows}

    enriched = []
    for chunk in chunks:
        if chunk.get("node_type") == "CLAUSE" and chunk.get("parent_id") in parent_map:
            parent = parent_map[chunk["parent_id"]]
            chunk = dict(chunk)
            chunk["context_chunk_ids"] = [parent["chunk_id"]]
            # chunk_text에 부모 조문 제목 prepend (이미 contextual_header가 있으면 스킵)
            if chunk.get("chunk_text") and parent.get("parent_title"):
                if not chunk["chunk_text"].startswith("["):
                    chunk["chunk_text"] = (
                        f"[{parent['regulation_article']} {parent['parent_title']}] "
                        + chunk["chunk_text"]
                    )
        enriched.append(chunk)
    return enriched


# ─────────────────────────────────────────────────────────────────────────────
# 기존 LIKE 검색 → _search_lexical_legacy로 이름 변경 (fallback 용도)
# ─────────────────────────────────────────────────────────────────────────────
def _search_lexical_legacy(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    limit: int = 20,
    effective_date: Any = None,
) -> list[dict[str, Any]]:
    """
    기존 LIKE 기반 검색 (fallback).
    BM25/Dense 모두 실패 시 사용.
    기존 search_policy_chunks() 로직을 그대로 이전.
    """
    # (기존 search_policy_chunks 내부 로직을 여기에 그대로 복사)
    ...
```

---

### ④ `services/retrieval_quality.py` — 한국어 Cross-Encoder 교체

```python
# 기존:
# _get_cross_encoder(model_name="cross-encoder/ms-marco-MiniLM-L6-v2")
# → 영어 전용 모델. 한국어 규정 텍스트에 부적합.

# 변경 후:
_KO_CROSS_ENCODER_MODEL_NAME = "Dongjin-kr/ko-reranker"

def _get_cross_encoder(model_name: str | None = None):
    global _CROSS_ENCODER_MODEL
    if _CROSS_ENCODER_MODEL is not None:
        return _CROSS_ENCODER_MODEL
    target_model = model_name or _KO_CROSS_ENCODER_MODEL_NAME
    try:
        from sentence_transformers import CrossEncoder
        _CROSS_ENCODER_MODEL = CrossEncoder(target_model)
        return _CROSS_ENCODER_MODEL
    except Exception:
        return None
```

---

### ⑤ `requirements.txt` — 의존성 추가

```txt
# Phase F-2: 한국어 임베딩 + cross-encoder rerank
# 설치: pip install torch sentence-transformers
# 미설치 시 임베딩은 스킵되고 BM25만 사용 (graceful degradation)
# torch 버전은 Python/CUDA 환경에 맞게 별도 설치 필요
# https://pytorch.org/get-started/locally/
sentence-transformers>=3.0.0
```

---

### ⑥ `tests/test_rag_chunking.py` — 신규 테스트 파일

```python
"""
RAG 청킹 및 검색 단위 테스트.
"""
import unittest

SAMPLE_TEXT = """
제3장 경비 유형별 기준
======================================================================

제23조 (식대)
① 업무상 식대는 인당 기준한도 및 참석자 기준을 충족하여야 하며, 사적 목적 식대를 금지한다.
② 다음 각 호에 해당하는 식대는 검토 대상으로 분류한다.
1. 23:00~06:00 심야 식대
2. 주말/공휴일 식대(예외 승인 없는 경우)
3. 인당 한도 초과 식대
③ 식대 증빙은 참석자 명단과 영수증을 포함하여야 한다.

제24조 (접대비)
① 접대비는 사전 승인을 받아야 한다.
② 접대비 한도는 별표에 따른다.
"""


class TestHierarchicalChunking(unittest.TestCase):

    def test_article_extraction(self):
        """조문이 ARTICLE 노드로 추출되어야 한다."""
        from services.rag_chunk_lab_service import hierarchical_chunk
        nodes = hierarchical_chunk(SAMPLE_TEXT)
        article_nodes = [n for n in nodes if n.node_type == "ARTICLE"]
        self.assertGreaterEqual(len(article_nodes), 2, "제23조, 제24조 최소 2개 추출")

    def test_clause_extraction(self):
        """항(①②③)이 CLAUSE 노드로 추출되어야 한다."""
        from services.rag_chunk_lab_service import hierarchical_chunk
        nodes = hierarchical_chunk(SAMPLE_TEXT)
        clause_nodes = [n for n in nodes if n.node_type == "CLAUSE"]
        self.assertGreater(len(clause_nodes), 0, "CLAUSE 노드가 1개 이상 추출되어야 함")

    def test_clause_has_parent_reference(self):
        """CLAUSE 노드의 chunk_text에 부모 조문 제목이 포함되어야 한다."""
        from services.rag_chunk_lab_service import hierarchical_chunk
        nodes = hierarchical_chunk(SAMPLE_TEXT)
        clause_nodes = [n for n in nodes if n.node_type == "CLAUSE"]
        for node in clause_nodes:
            self.assertTrue(
                node.contextual_header or node.regulation_article,
                f"CLAUSE 노드에 맥락 정보 없음: {node.chunk_text[:50]}"
            )

    def test_regulation_article_populated(self):
        """모든 ARTICLE 노드에 regulation_article이 채워져야 한다."""
        from services.rag_chunk_lab_service import hierarchical_chunk
        nodes = hierarchical_chunk(SAMPLE_TEXT)
        for node in nodes:
            if node.node_type == "ARTICLE":
                self.assertIsNotNone(node.regulation_article)
                self.assertIn("제", node.regulation_article)

    def test_search_text_excludes_prefix(self):
        """search_text는 조문 번호 prefix 없이 순수 본문만 포함해야 한다."""
        from services.rag_chunk_lab_service import hierarchical_chunk
        nodes = hierarchical_chunk(SAMPLE_TEXT)
        for node in [n for n in nodes if n.node_type == "CLAUSE"]:
            # search_text에 조문 번호가 그대로 들어가면 임베딩 품질 저하
            self.assertNotIn("제23조", node.search_text[:10],
                             "search_text 앞에 조문 번호가 들어가서는 안 됨")

    def test_rrf_fusion(self):
        """RRF 융합이 두 결과를 올바르게 합산해야 한다."""
        from services.policy_service import _reciprocal_rank_fusion
        bm25 = [{"chunk_id": 1, "bm25_score": 0.9}, {"chunk_id": 2, "bm25_score": 0.5}]
        dense = [{"chunk_id": 2, "dense_score": 0.95}, {"chunk_id": 3, "dense_score": 0.8}]
        result = _reciprocal_rank_fusion(bm25, dense, k=60)
        ids = [r["chunk_id"] for r in result]
        # chunk_id=2는 두 리스트에 모두 있으므로 최상위에 와야 함
        self.assertEqual(ids[0], 2, "양쪽 결과에 모두 있는 chunk_id=2가 1위여야 함")

    def test_rrf_empty_dense(self):
        """Dense 결과가 없어도 BM25만으로 반환되어야 한다."""
        from services.policy_service import _reciprocal_rank_fusion
        bm25 = [{"chunk_id": 1, "bm25_score": 0.9}]
        result = _reciprocal_rank_fusion(bm25, [], k=60)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["chunk_id"], 1)

    def test_preview_chunks_hierarchical(self):
        """preview_chunks_hierarchical이 기존 인터페이스와 호환되는 형태를 반환해야 한다."""
        from services.rag_chunk_lab_service import preview_chunks_hierarchical
        results = preview_chunks_hierarchical(SAMPLE_TEXT)
        self.assertGreater(len(results), 0)
        for item in results:
            self.assertIn("title", item)
            self.assertIn("content", item)
            self.assertIn("length", item)
            self.assertIn("strategy", item)
            self.assertEqual(item["strategy"], "hierarchical_parent_child")
```

---

## DB 마이그레이션 필요 사항

### 벡터 차원 변경 (반드시 실행)

현재 `embedding vector(1536)`(OpenAI 차원)이나 `ko-sroberta-multitask`는 768차원.
아래 중 하나를 선택:

```sql
-- [방법 A] embedding 컬럼을 768차원으로 변경 (기존 데이터 초기화)
ALTER TABLE dwp_aura.rag_chunk
  ALTER COLUMN embedding TYPE vector(768) USING NULL;

-- HNSW 인덱스 추가 (벡터 검색 성능 최적화, 수천 청크 규모에 적합)
CREATE INDEX IF NOT EXISTS ix_rag_chunk_embedding_hnsw
  ON dwp_aura.rag_chunk
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- [방법 B] 별도 컬럼 추가 (기존 데이터 보존, 향후 마이그레이션)
ALTER TABLE dwp_aura.rag_chunk
  ADD COLUMN IF NOT EXISTS embedding_ko vector(768);

CREATE INDEX IF NOT EXISTS ix_rag_chunk_embedding_ko_hnsw
  ON dwp_aura.rag_chunk
  USING hnsw (embedding_ko vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
```

---

## 구현 완료 검증 체크리스트

```bash
# 1. 신규 단위 테스트
python -m pytest tests/test_rag_chunking.py -v

# 2. 청킹 결과 미리보기 (DB 불필요)
python -c "
from services.rag_chunk_lab_service import hierarchical_chunk
text = open('규정집/사내_경비_지출_관리_규정_v2.0_확장판.txt', encoding='utf-8').read()
nodes = hierarchical_chunk(text)
articles = [n for n in nodes if n.node_type == 'ARTICLE']
clauses = [n for n in nodes if n.node_type == 'CLAUSE']
print(f'ARTICLE: {len(articles)}개, CLAUSE: {len(clauses)}개')
for n in nodes[:5]:
    print(f'  [{n.node_type}] {n.regulation_article} | {n.parent_title[:30]}')
    print(f'  search_text: {n.search_text[:60]}...')
"

# 3. 임베딩 모델 로드 확인
python -c "
from services.chunking_pipeline import get_embedding_model
model = get_embedding_model()
if model:
    dim = model.get_sentence_embedding_dimension()
    print(f'모델 로드 성공. 차원: {dim}')
    vecs = model.encode(['휴일 식대 검토 대상'])
    print(f'임베딩 벡터 샘플: {vecs[0][:5]}...')
else:
    print('모델 미설치 — pip install torch sentence-transformers 필요')
"

# 4. RRF 동작 확인
python -c "
from services.policy_service import _reciprocal_rank_fusion
b = [{'chunk_id': i, 'bm25_score': 1/(i+1)} for i in range(10)]
d = [{'chunk_id': i+5, 'dense_score': 1/(i+1)} for i in range(10)]
result = _reciprocal_rank_fusion(b, d)
print('RRF 상위 5:', [(r['chunk_id'], round(r['rrf_score'], 4)) for r in result[:5]])
"
```

---

## 병행 작업 제안

### A. 청킹 실험실 UI 업데이트 (독립 작업)

`ui/rag.py`의 청킹 전략 선택 드롭다운에 `hierarchical_parent_child` 항목 추가.
미리보기에 `node_type`, `regulation_article`, `contextual_header` 표시.

### B. pgvector HNSW 인덱스 생성 (DB 작업, 병행 가능)

위 SQL 마이그레이션 중 HNSW 인덱스 생성은 별도로 진행 가능.
인덱스 없이도 Sequential Scan으로 동작하나, 청크 수가 1000개 이상이면 성능 차이 발생.

### C. 기존 청크 재색인 (파이프라인 구현 후 실행)

```python
# 기존 규정집 문서를 새 청킹 전략으로 재처리
# run_chunking_pipeline(db, doc_id=1, raw_text=...) 호출
```