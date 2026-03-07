# Cursor 작업 프롬프트 — RAG 청킹 DB 적재 보완 4항목

## 사전 검토 결과 요약

CSV 실제 데이터(239개 청크)와 작업자 검토 의견을 교차 분석한 결과:

| 항목 | 작업자 진단 | 데이터 교차 검증 | 최종 판단 |
|------|-----------|----------------|---------|
| ① 초단편 ROOT 병합 | hierarchical_chunk()에 병합 로직 없음 | ROOT 63개 중 35개(55%) 200자 미만 — 진단 일치 | 수정 필수 |
| ② 임베딩 모델 | chunking.md는 768차원 설계 — bge-m3는 별도 회의안 | embedding_ko 768차원, L2 norm=1.0 정상 적재됨 | **현행 유지, 문서 정리만** |
| ③ parent_chunk_id | INSERT에 미포함 | child 176개 전부 parent_chunk_id 공백 — 진단 일치 | 수정 필요 |
| ④ semantic_group | 저장 경로에 metadata_json 미반영 | child 브레드크럼으로 13개 장(章) 전수 역추적 가능 | 수정 필요 |

> **②는 이 프롬프트에서 제외합니다.**
> 현재 768차원(paraphrase-multilingual-mpnet-base-v2)은 chunking.md 설계와 일치하며,
> 임베딩 품질(L2 norm=1.0, 768차원 전부 의미값)도 정상입니다.
> bge-m3(1024차원) 전환은 아키텍처 의사결정 후 별도 스프린트로 처리하세요.

---

## 작업 항목: ①③④ 3가지

---

## ① 초단편 ROOT 청크 병합 (`hierarchical_chunk()` 수정)

### 문제 정확한 위치

`services/chunking_pipeline.py` — `hierarchical_chunk()` 함수

현재는 조문 경계만 기준으로 ARTICLE 노드를 생성하며,
길이 기반 인접 조문 병합 로직이 없음.

### 병합 시뮬레이션 결과 (CSV 기반 사전 검증)

병합 가능 쌍: **21개**
- 적용 후 ROOT: 63개 → **42개** 예상
- 대표 병합 사례:

| 대상 | 현재 길이 | 병합 결과 |
|------|---------|---------|
| 제34조(108자) + 제35조(92자) | 각각 초단편 | 하나의 ARTICLE로 통합 |
| 제30조(110자) + 제31조(152자) | 각각 초단편 | 하나의 ARTICLE로 통합 |
| 제18조(154자) + 제19조(159자) | 각각 초단편 | 하나의 ARTICLE로 통합 |
| 제1조(36자) → 다음 조문과 병합 | 36자 단독 | 다음 조문에 흡수 |

### 구현 명세

**`services/chunking_pipeline.py`의 `hierarchical_chunk()` 함수 내부 수정**

ARTICLE 노드 리스트가 완성된 직후, 저장 전에 아래 병합 단계를 추가한다.

```python
# ── 상수 ──────────────────────────────────────────────────────────────────
PARENT_MIN = 200   # 이 길이 미만인 ARTICLE은 다음 조문과 병합


def _merge_short_articles(
    articles: list[dict],   # {"title": str, "body": str, "clauses": list[dict]}
    parent_min: int = PARENT_MIN,
) -> list[dict]:
    """
    길이가 parent_min 미만인 ARTICLE을 바로 다음 ARTICLE과 병합한다.

    병합 규칙:
    1. body 길이가 parent_min 미만인 경우만 병합 대상
    2. 마지막 조문은 이전 조문에 병합 (다음 조문이 없는 경우)
    3. 병합 제목: "제N조 ~ 제M조"
    4. 병합 본문: body 사이 빈 줄(\n\n) 구분
    5. 병합 시 두 조문의 clauses를 모두 유지
       → CLAUSE의 regulation_article은 대표 조문(첫 번째) 유지
    """
    if not articles:
        return articles

    merged: list[dict] = []
    skip_next = False

    for i, art in enumerate(articles):
        if skip_next:
            skip_next = False
            continue

        body_len = len(art.get("body") or "")
        has_next = i + 1 < len(articles)

        if body_len < parent_min and has_next:
            next_art = articles[i + 1]
            # 두 조문 병합
            merged_title = f"{art['title']} ~ {next_art['title']}"
            merged_body  = (art.get("body") or "") + "\n\n" + (next_art.get("body") or "")
            merged_clauses = list(art.get("clauses") or []) + list(next_art.get("clauses") or [])
            merged.append({
                "title":           merged_title,
                "body":            merged_body,
                "clauses":         merged_clauses,
                "regulation_article": art["regulation_article"],   # 첫 조문 번호 대표
                "merged_with":     next_art["regulation_article"],  # 로깅용
            })
            skip_next = True
        elif body_len < parent_min and not has_next and merged:
            # 마지막 조문이 짧으면 이전 결과에 흡수
            prev = merged[-1]
            prev["body"]     = (prev.get("body") or "") + "\n\n" + (art.get("body") or "")
            prev["title"]    = f"{prev['title']} ~ {art['title']}"
            prev["clauses"]  = list(prev.get("clauses") or []) + list(art.get("clauses") or [])
        else:
            merged.append(art)

    return merged
```

**`hierarchical_chunk()` 내부 호출 위치:**

```python
def hierarchical_chunk(text: str, ...) -> list[ChunkNode]:
    # ... 기존 조문 파싱 로직 ...

    articles = _parse_articles(text)      # 기존 파싱

    # ── 신규: 초단편 ARTICLE 병합 ─────────────────────────────────────────
    articles = _merge_short_articles(articles, parent_min=PARENT_MIN)
    # ─────────────────────────────────────────────────────────────────────

    nodes: list[ChunkNode] = []
    for art in articles:
        article_node = ChunkNode(
            chunk_text       = art["body"],
            node_type        = "ARTICLE",
            chunk_level      = "root",
            regulation_article = art["regulation_article"],
            # ... 기존 나머지 필드 ...
        )
        nodes.append(article_node)

        for clause in (art.get("clauses") or []):
            clause_node = ChunkNode(
                chunk_text         = clause["text"],
                node_type          = "CLAUSE",
                chunk_level        = "child",
                regulation_article = art["regulation_article"],   # 병합 대표 조문
                parent_article     = art["regulation_article"],
                # ... 기존 나머지 필드 ...
            )
            nodes.append(clause_node)

    return nodes
```

### 검증 방법

```bash
# 재청킹 후 품질 확인
python3 -c "
from services.chunking_pipeline import hierarchical_chunk
from services.rag_chunk_lab_service import load_rulebook_text

text = load_rulebook_text('/path/to/규정집.txt')
nodes = hierarchical_chunk(text)
roots = [n for n in nodes if n.chunk_level == 'root']
short = [n for n in roots if len(n.chunk_text) < 200]

print(f'ROOT 수: {len(roots)} (이전: 63개, 기대: 42개)')
print(f'200자 미만 ROOT: {len(short)} (이전: 35개, 기대: 5개 이하)')
"
```

### 기대 결과

| 항목 | 변경 전 | 변경 후 |
|------|---------|---------|
| ROOT 청크 수 | 63개 | ~42개 |
| 200자 미만 ROOT | 35개 (55%) | 5개 이하 (12% 이하) |
| 병합 ARTICLE 쌍 | 0개 | 21개 |

---

## ③ parent_chunk_id FK 연결 (`chunking_pipeline.py` 수정)

### 문제 정확한 위치

`services/chunking_pipeline.py` — `save_hierarchical_chunks()` (또는 INSERT 수행 함수)

현재 CLAUSE 노드 INSERT 시 `parent_chunk_id` 컬럼이 INSERT 대상에 없음.

### CSV 기반 정확한 연결 관계 확인

```
child chunk_id=4908, parent_id=4791  → parent_chunk_id 필요값: '4791'
child chunk_id=4909, parent_id=4792  → parent_chunk_id 필요값: '4792'
child chunk_id=4910, parent_id=4792  → parent_chunk_id 필요값: '4792'
```

`parent_chunk_id`는 `varchar(128)` 컬럼이며,
`parent_id`(int FK)와 **동일한 값을 문자열로 저장**하면 됨.

### 구현 명세

**`services/chunking_pipeline.py`의 저장 함수 수정**

ARTICLE INSERT 후 반환받은 `chunk_id`를 CLAUSE INSERT 시 `parent_chunk_id`에 설정.

```python
def save_hierarchical_chunks(db: Session, nodes: list[ChunkNode], doc_id: int, tenant_id: int) -> None:
    article_chunk_id_map: dict[str, int] = {}   # regulation_article → DB chunk_id

    for node in nodes:
        if node.chunk_level == "root":
            # ── ARTICLE INSERT ─────────────────────────────────────────────
            result = db.execute(
                text("""
                    INSERT INTO dwp_aura.rag_chunk
                        (tenant_id, doc_id, chunk_text, ..., chunk_level, regulation_article,
                         parent_chunk_id, ...)
                    VALUES
                        (:tenant_id, :doc_id, :chunk_text, ..., 'root', :regulation_article,
                         NULL, ...)              -- ARTICLE은 parent_chunk_id = NULL
                    RETURNING chunk_id
                """),
                {
                    "tenant_id":          tenant_id,
                    "doc_id":             doc_id,
                    "chunk_text":         node.chunk_text,
                    "regulation_article": node.regulation_article,
                    # ... 기존 나머지 파라미터 ...
                }
            )
            new_chunk_id = result.scalar_one()
            # ── 핵심: 조문 번호 → chunk_id 맵 저장 ───────────────────────
            article_chunk_id_map[node.regulation_article] = new_chunk_id

        else:  # chunk_level == "child" (CLAUSE)
            # 부모 chunk_id 조회
            parent_article    = node.regulation_article   # 또는 node.parent_article
            parent_chunk_id_int = article_chunk_id_map.get(parent_article)

            db.execute(
                text("""
                    INSERT INTO dwp_aura.rag_chunk
                        (tenant_id, doc_id, chunk_text, ..., chunk_level, regulation_article,
                         parent_id, parent_chunk_id, ...)
                    VALUES
                        (:tenant_id, :doc_id, :chunk_text, ..., 'child', :regulation_article,
                         :parent_id, :parent_chunk_id, ...)
                """),
                {
                    "tenant_id":          tenant_id,
                    "doc_id":             doc_id,
                    "chunk_text":         node.chunk_text,
                    "regulation_article": node.regulation_article,
                    "parent_id":          parent_chunk_id_int,
                    "parent_chunk_id":    str(parent_chunk_id_int) if parent_chunk_id_int else None,  # ← 핵심 신규
                    # ... 기존 나머지 파라미터 ...
                }
            )

    db.commit()
```

> **주의**: ARTICLE과 CLAUSE를 한 번에 bulk INSERT하는 구조라면,
> ARTICLE을 먼저 flush/commit 후 CLAUSE INSERT로 순서를 분리해야
> RETURNING chunk_id를 안정적으로 받을 수 있습니다.

### 검증 방법

```sql
-- 재적재 후 DB에서 확인
SELECT
    chunk_id,
    chunk_level,
    regulation_article,
    parent_id,
    parent_chunk_id
FROM dwp_aura.rag_chunk
WHERE doc_id = 26
  AND chunk_level = 'child'
LIMIT 10;

-- 기대: parent_id와 parent_chunk_id 값이 동일 (parent_chunk_id = parent_id::varchar)
-- 예: parent_id=4791, parent_chunk_id='4791'
```

### 기대 결과

| 항목 | 변경 전 | 변경 후 |
|------|---------|---------|
| CHILD parent_chunk_id | 176개 전부 NULL | 176개 전부 str(parent_chunk_id_int) |
| policy_service.py SELECT | parent_chunk_id 항상 NULL 반환 | 실제 값 반환 |

---

## ④ semantic_group 적용 (`chunking_pipeline.py` 수정)

### 분석 결과: 브레드크럼에서 이미 추출 가능

CSV child 청크의 `chunk_text` 브레드크럼 형태:
```
[제7장 계정별 세부 집행 기준 > 제23조 > (식대)] ① 업무상 식대는...
[제8장 시간·금액·거래처·업종 공통 제약 > 제38조 > (시간대 제약)] ② 심야시간...
[제1장 총칙 > 제2조 > (정의)] ① 이 규정에서...
```

첫 번째 `[` ~ `>` 사이가 정확히 **13개 장(章)**으로 분류됨:

| 장 | 해당 조문 수 |
|----|-----------|
| 제1장 총칙 | 5개 조 |
| 제7장 계정별 세부 집행 기준 | 46개 child (가장 많음) |
| 제8장 시간·금액·거래처·업종 공통 제약 | 17개 child |
| 제10장 Agent AI 판정 및 운영 | 16개 child |
| ... (총 13개 장) | |

또한 **ROOT 63개 전부** child 브레드크럼으로 chapter 역추적 가능.

### 구현 명세

#### STEP 1: `hierarchical_chunk()`에서 장 정보 추출

```python
import re

_CHAPTER_PAT = re.compile(r"(제\s*\d+\s*장[^\n]*)")   # "제N장 제목" 패턴

def hierarchical_chunk(text: str, ...) -> list[ChunkNode]:
    current_chapter: str = ""   # 현재 장 추적

    for line in text.splitlines():
        chapter_match = _CHAPTER_PAT.match(line.strip())
        if chapter_match:
            current_chapter = chapter_match.group(1).strip()
            continue

        # ARTICLE 파싱 시 current_chapter를 node에 붙임
        if _is_article(line):
            article_node.semantic_group = current_chapter   # ← 신규 필드
            ...
```

#### STEP 2: `save_hierarchical_chunks()`에서 metadata_json에 저장

ARTICLE(root), CLAUSE(child) 모두 INSERT 시 `metadata_json` 컬럼에 저장:

```python
import json

# ARTICLE INSERT 시
metadata = {
    "semantic_group":    node.semantic_group or "",      # 예: "제7장 계정별 세부 집행 기준"
    "regulation_article": node.regulation_article,       # 예: "제23조"
}
# 병합 조문의 경우
if hasattr(node, "merged_with") and node.merged_with:
    metadata["merged_with"] = node.merged_with           # 예: "제24조"

# CLAUSE INSERT 시
metadata = {
    "semantic_group":    node.semantic_group or "",
    "regulation_article": node.regulation_article,
    "parent_article":    node.parent_article or node.regulation_article,
    "child_index":       node.child_index,
}

# INSERT 파라미터
"metadata_json": json.dumps(metadata, ensure_ascii=False)
```

#### STEP 3: `ChunkNode` 모델에 필드 추가

```python
# services/chunking_pipeline.py 또는 schemas.py
class ChunkNode:
    # ... 기존 필드 ...
    semantic_group: str = ""        # 신규: 장(章) 단위 그룹
    merged_with: str | None = None  # 신규: 병합된 조문 번호 (① 병합 작업과 연동)
```

### 검증 방법

```sql
-- 재적재 후 DB에서 확인
SELECT
    regulation_article,
    metadata_json->>'semantic_group' AS semantic_group,
    chunk_level,
    left(chunk_text, 40) AS preview
FROM dwp_aura.rag_chunk
WHERE doc_id = 26
  AND is_active = true
ORDER BY chunk_id
LIMIT 20;

-- 기대:
-- 제23조 | "제7장 계정별 세부 집행 기준" | root | "제23조 (식대)\n① 업무상 식대..."
-- 제23조 | "제7장 계정별 세부 집행 기준" | child | "[제7장 ... > 제23조 ..."
-- 제38조 | "제8장 시간·금액·거래처·업종 공통 제약" | root | "제38조 (시간대 제약)..."
```

### 기대 결과

| 항목 | 변경 전 | 변경 후 |
|------|---------|---------|
| metadata_json | NULL 또는 {} | {"semantic_group": "제N장...", "regulation_article": "제N조"} |
| 장별 필터링 검색 | 불가 | `metadata_json->>'semantic_group'` 조건 검색 가능 |
| ROOT도 장 정보 보유 | 없음 | child 브레드크럼 역추적으로 63개 전부 적용 |

---

## 작업 순서 및 재적재 절차

```
① hierarchical_chunk() 수정 (병합 로직 추가)
    ↓
③ save_hierarchical_chunks() 수정 (parent_chunk_id INSERT)
    ↓
④ save_hierarchical_chunks() 수정 (metadata_json INSERT)
    ↓
기존 doc_id=26 청크 비활성화 (is_active=false) 또는 삭제
    ↓
재청킹 및 재적재 실행
    ↓
검증 쿼리 실행 (위 각 항목 검증 SQL)
```

### 재적재 전 기존 데이터 처리

```sql
-- 기존 청크 비활성화 (hard delete 대신 soft delete 권장)
UPDATE dwp_aura.rag_chunk
SET is_active = false
WHERE doc_id = 26
  AND tenant_id = 1;
```

---

## 검증 체크리스트

```bash
python3 -c "
import csv, json

rows = []
with open('rag_chunk_재적재후.csv') as f:
    for row in csv.DictReader(f):
        rows.append(row)

roots    = [r for r in rows if r['chunk_level'] == 'root']
children = [r for r in rows if r['chunk_level'] == 'child']

# ① 병합 검증
short = [r for r in roots if len(r['chunk_text']) < 200]
print(f'[①] ROOT 수: {len(roots)} (기대: ~42) / 200자 미만: {len(short)} (기대: ≤5)')

# ③ parent_chunk_id 검증
filled = sum(1 for r in children if r.get('parent_chunk_id','').strip())
print(f'[③] parent_chunk_id 채움: {filled}/{len(children)} (기대: {len(children)})')

# ④ semantic_group 검증
sg = [r for r in rows if r.get('metadata_json','') not in ('','null','{}')]
groups = set()
for r in sg:
    try:
        m = json.loads(r['metadata_json'])
        if m.get('semantic_group'):
            groups.add(m['semantic_group'])
    except: pass
print(f'[④] metadata_json 채움: {len(sg)}/{len(rows)} (기대: {len(rows)})')
print(f'[④] semantic_group 고유 값: {len(groups)} (기대: 13개 장)')
"
```

---

## ② 임베딩 모델 — 현행 유지 결정

작업자 검토 의견 및 데이터 검증 결과, 이번 스프린트에서는 **변경하지 않습니다**.

| 근거 | 내용 |
|------|------|
| chunking.md 설계와 일치 | 768차원 모델(jhgan/ko-sroberta 계열)이 원래 설계 |
| 품질 정상 | L2 norm=1.0, 768차원 전부 의미값, 중복 없음 |
| bge-m3 전환 비용 | DB 스키마 변경(vector 차원), 재색인, HNSW 인덱스 재생성 필요 |

**bge-m3(1024차원) 전환을 원한다면** 별도 스프린트에서:
1. DB: `embedding_ko` 컬럼을 `vector(1024)`로 변경 또는 `embedding_bge vector(1024)` 신규 컬럼 추가
2. 코드: `chunking_pipeline.py`의 `get_embedding_model()`에서 모델명/차원 변경
3. 전체 재색인 실행
4. `policy_service.py`의 벡터 검색 쿼리 컬럼명 수정




#추가 작업
# Cursor 작업 프롬프트 — 청킹 UI 2가지 긴급 보완

## 사전 분석으로 확인된 정확한 원인

### 문제 1 — "초단편 208개" 카운팅 오류

CSV 데이터(239개 청크) 실측 결과:

```
현재 short_chunk 판정: 전체 청크 200자 미만 → 208개 (87%)

내역:
  child(CLAUSE) 중 200자 미만: 173개
  root(ARTICLE)  중 200자 미만:  35개
  합계:                         208개  ← UI에 표시되는 숫자
```

**원인**: `short_chunk_rate` 계산이 `chunk_level` 구분 없이 전체 청크에 적용됨.
CLAUSE(child) 청크는 항목(①②③) 단위로 분리된 것이라 100자 내외가 구조적으로 정상인데,
short 청크로 잘못 집계되고 있음.

**올바른 기준**:
- `ROOT(ARTICLE)` 청크만 200자 기준 적용 → 35개 / 63개 (56%)
- `CHILD(CLAUSE)` 청크는 short 판정 제외 (구조적으로 짧음)

---

### 문제 2 — parent_child 전략 품질 점수 0점

두 가지 연쇄 원인:

**원인 A**: `rag.py` 전략 비교 `compare` dict에 `parent_child` 전략이 없음
```python
# 현재 코드 (rag.py L93~95)
compare = {
    "하이브리드 정책형": preview_chunks(text, "hybrid_policy"),
    "조항 우선":         preview_chunks(text, "article_first"),
    "슬라이딩 윈도우":   preview_chunks(text, "sliding_window"),
    # parent_child 없음 ← 누락
}
```
→ 드롭다운에서 `parent_child` 선택해도 비교 카드에 미포함

**원인 B**: 품질 점수 계산식이 parent_child 구조를 인식 못 함
```python
# 현재 계산 (이전 프롬프트 설계안)
quality_score = max(0, 100 - short_count * 3)
# short_count = chunk_level 무관 전체 200자 미만 수
# → parent_child는 child 청크가 많아 short_count가 높게 나옴 → 0점
```

**올바른 계산**: parent_child는 ROOT(ARTICLE) 기준으로만 short 판정

---

## 작업 범위

| 파일 | 함수/위치 | 작업 |
|------|---------|------|
| `services/rag_chunk_lab_service.py` | `preview_chunks()` | `parent_child` 전략 추가 |
| `ui/rag.py` | `compare` dict | `parent_child` 항목 추가 |
| `ui/rag.py` | 전략 비교 카드 품질 점수 계산 | chunk_level 구분 적용 |
| `services/chunking_pipeline.py` (또는 quality report 계산부) | `short_chunk_rate` 계산 | ROOT 청크만 기준으로 수정 |

---

## 상세 구현 명세

---

### ① `services/rag_chunk_lab_service.py` — `preview_chunks()`에 `parent_child` 추가

기존 `preview_chunks()` 함수 끝에 아래 분기를 추가한다.

```python
def preview_chunks(text: str, strategy: str) -> list[dict[str, Any]]:
    if strategy == "article_first":
        ...
    if strategy == "sliding_window":
        ...
    if strategy == "parent_child":
        # ── Parent-Child 계층형 미리보기 ────────────────────────────────
        PARENT_MIN = 200
        out: list[dict[str, Any]] = []
        sections = _split_article_sections(text)
        used: set[int] = set()

        for i, (title, body) in enumerate(sections):
            if i in used:
                continue

            # 초단편 조문 → 다음 조문과 병합
            if len(body) < PARENT_MIN and i + 1 < len(sections):
                next_title, next_body = sections[i + 1]
                merged_title = f"{title} ~ {next_title}"
                merged_body  = body + "\n\n" + next_body
                used.add(i + 1)
                title, body = merged_title, merged_body

            used.add(i)

            # Parent 청크 (ARTICLE)
            out.append({
                "title":      f"[Parent] {title}",
                "content":    body,
                "length":     len(body),
                "strategy":   strategy,
                "chunk_type": "parent",
                "article":    title,
            })

            # Child 청크: 항목 기호(①②③...) 기준 분할
            item_pat = re.compile(r"(?=[①②③④⑤⑥⑦⑧⑨⑩])")
            children = [c.strip() for c in item_pat.split(body) if c.strip()]
            if len(children) > 1:
                for c_idx, child_text in enumerate(children, start=1):
                    out.append({
                        "title":      f"  └ [Child {c_idx}] {title}",
                        "content":    child_text,
                        "length":     len(child_text),
                        "strategy":   strategy,
                        "chunk_type": "child",
                        "article":    title,
                    })

        return out

    # hybrid_policy (기본)
    ...
```

---

### ② `ui/rag.py` — 전략 드롭다운 및 `compare` dict 수정

**수정 위치 1 — 전략 드롭다운 (L84~88)**

기존:
```python
strategy = st.selectbox(
    "청킹 전략",
    options=["hybrid_policy", "article_first", "sliding_window"],
    format_func=lambda x: {
        "hybrid_policy":  "하이브리드 정책형",
        "article_first":  "조항 우선",
        "sliding_window": "슬라이딩 윈도우",
    }[x]
)
```

변경 후:
```python
strategy = st.selectbox(
    "청킹 전략",
    options=["parent_child", "hybrid_policy", "article_first", "sliding_window"],
    format_func=lambda x: {
        "parent_child":   "🆕 Parent-Child 계층형 (권장)",
        "hybrid_policy":  "하이브리드 정책형",
        "article_first":  "조항 우선",
        "sliding_window": "슬라이딩 윈도우",
    }[x]
)
```

---

**수정 위치 2 — `compare` dict (L93~97) 및 전략 비교 카드 (L98~112)**

기존:
```python
compare = {
    "하이브리드 정책형": preview_chunks(text, "hybrid_policy"),
    "조항 우선":         preview_chunks(text, "article_first"),
    "슬라이딩 윈도우":   preview_chunks(text, "sliding_window"),
}
compare_cols = st.columns(3)
for col, (label, rows) in zip(compare_cols, compare.items()):
    with col:
        with stylable_container(...):
            st.caption(label)
            st.subheader(str(len(rows)))
            avg_len = f"{(sum(r['length'] for r in rows) / len(rows)):.0f}" if rows else "0"
            st.caption(f"평균 길이 {avg_len} chars")
```

변경 후:
```python
compare = {
    "Parent-Child 계층형": preview_chunks(text, "parent_child"),
    "하이브리드 정책형":    preview_chunks(text, "hybrid_policy"),
    "조항 우선":            preview_chunks(text, "article_first"),
    "슬라이딩 윈도우":      preview_chunks(text, "sliding_window"),
}
compare_cols = st.columns(4)   # 3 → 4열로 변경


def _quality_score(chunks: list[dict], strategy: str) -> int:
    """
    전략별 품질 점수 계산.

    parent_child: ROOT(parent) 청크만 short 판정 기준 적용
    기타 전략:    전체 청크 기준

    short 기준: 200자 미만
    감점: short 1개당 -3점
    """
    if not chunks:
        return 0
    if strategy == "parent_child":
        # parent(ARTICLE) 청크만 기준
        parent_chunks = [c for c in chunks if c.get("chunk_type") == "parent"]
        if not parent_chunks:
            return 0
        short_count = sum(1 for c in parent_chunks if c["length"] < 200)
        return max(0, 100 - short_count * 3)
    else:
        short_count = sum(1 for c in chunks if c["length"] < 200)
        return max(0, 100 - short_count * 3)


_STRATEGY_KEYS = {
    "Parent-Child 계층형": "parent_child",
    "하이브리드 정책형":    "hybrid_policy",
    "조항 우선":            "article_first",
    "슬라이딩 윈도우":      "sliding_window",
}

for col, (label, rows) in zip(compare_cols, compare.items()):
    strategy_key = _STRATEGY_KEYS[label]
    q_score = _quality_score(rows, strategy_key)
    is_recommended = strategy_key == "parent_child"

    # 권장 전략은 파란 테두리 강조
    border_style = "border: 2px solid #2563eb;" if is_recommended else "border: 1px solid #e5e7eb;"

    with col:
        with stylable_container(
            key=f"rag_compare_{label}",
            css_styles=f"{{{border_style} padding: 14px 16px; border-radius: 16px; background: rgba(255,255,255,0.98); box-shadow: 0 8px 22px rgba(15,23,42,0.04); min-height: 148px;}}"
        ):
            header = f"{'🏆 ' if is_recommended else ''}{label}"
            st.caption(header)
            st.subheader(str(len(rows)))
            avg_len = f"{(sum(r['length'] for r in rows) / len(rows)):.0f}" if rows else "0"
            st.caption(f"평균 길이 {avg_len} chars")

            # 품질 점수 게이지
            score_color = "#059669" if q_score >= 70 else "#d97706" if q_score >= 40 else "#dc2626"
            st.markdown(
                f"<div style='margin-top:8px;'>"
                f"<span style='font-size:11px;color:#6b7280;'>품질점수</span><br>"
                f"<span style='font-size:22px;font-weight:700;color:{score_color};'>{q_score}점</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
```

---

### ③ 청크 미리보기 — parent_child 구조 표시

**수정 위치 (L113~118, "청크 미리보기" 섹션)**

기존:
```python
for idx, chunk in enumerate(chunks[:12], start=1):
    with st.expander(f"{idx}. {chunk['title']} · {chunk['length']} chars", expanded=(idx == 1)):
        st.write(chunk["content"])
```

변경 후:
```python
for idx, chunk in enumerate(chunks[:12], start=1):
    chunk_type = chunk.get("chunk_type", "")

    if chunk_type == "parent":
        icon  = "📦"
        color = "#1d4ed8"   # 파란색
    elif chunk_type == "child":
        icon  = "🔹"
        color = "#6b7280"   # 회색
    else:
        icon  = ""
        color = "#111827"

    label = f"{icon} {chunk['title']} · {chunk['length']} chars"
    with st.expander(label, expanded=(idx == 1)):
        st.markdown(
            f"<span style='color:{color};font-size:12px;font-weight:600;'>"
            f"{'ARTICLE (Parent)' if chunk_type == 'parent' else 'CLAUSE (Child)' if chunk_type == 'child' else ''}"
            f"</span>",
            unsafe_allow_html=True,
        )
        st.write(chunk["content"])
```

---

### ④ `short_chunk_rate` 계산 수정 (quality report 생성부)

`services/chunking_pipeline.py` 또는 quality report를 계산하는 함수에서
`short_chunk_rate` 계산 시 `chunk_level == 'root'`인 청크만 대상으로 한정한다.

**현재 (추정 코드):**
```python
short_chunks = [c for c in chunks if len(c.chunk_text) < 200]
short_chunk_rate = len(short_chunks) / len(chunks) if chunks else 0.0
```

**변경 후:**
```python
# ROOT(ARTICLE) 청크만 short 판정 대상
# CHILD(CLAUSE) 청크는 항목 단위로 짧은 것이 구조적으로 정상
root_chunks  = [c for c in chunks if getattr(c, "chunk_level", "root") == "root"]
short_chunks = [c for c in root_chunks if len(c.chunk_text) < 200]

short_chunk_rate = len(short_chunks) / len(root_chunks) if root_chunks else 0.0
# ← 분모를 root_chunks로 변경하는 것이 핵심
```

**DB에 이미 적재된 경우 — `rag_document_quality_report` 재계산:**
```sql
-- 현재 잘못된 short_chunk_rate 확인
SELECT doc_id, input_chunks, final_chunks, short_chunk_rate
FROM dwp_aura.rag_document_quality_report
ORDER BY created_at DESC
LIMIT 5;

-- 재계산 후 업데이트 (재청킹 실행 시 자동 갱신됨)
```

---

## 검증 방법

```bash
# 1. preview_chunks parent_child 동작 확인
python3 -c "
from services.rag_chunk_lab_service import preview_chunks, load_rulebook_text

text = load_rulebook_text('/path/to/규정집.txt')
chunks = preview_chunks(text, 'parent_child')
parents  = [c for c in chunks if c.get('chunk_type') == 'parent']
children = [c for c in chunks if c.get('chunk_type') == 'child']
short_parents = [c for c in parents if c['length'] < 200]

print(f'전체: {len(chunks)}개 (parent {len(parents)} + child {len(children)})')
print(f'parent 200자 미만: {len(short_parents)}개 / {len(parents)}개')

from ui.rag import _quality_score
score = _quality_score(chunks, 'parent_child')
print(f'품질점수: {score}점 (기대: 0점 초과, 병합 후 40~60점대)')
"

# 2. 전략 비교 카드 확인 포인트
# ✅ 전략 드롭다운에 "🆕 Parent-Child 계층형 (권장)" 표시
# ✅ 전략 비교 카드 4개 (Parent-Child 포함)
# ✅ Parent-Child 카드에 파란 테두리 강조
# ✅ 각 카드에 품질점수 표시 (Parent-Child는 병합 전 0점 → 병합 후 양수)
# ✅ 청크 미리보기에서 📦 [Parent] / 🔹 └ [Child] 구분 표시
# ✅ UI 품질 리포트 초단편 수: 208개 → ROOT 기준으로 35개로 감소
```

---

## 핵심 요약

| 문제 | 원인 | 수정 내용 |
|------|------|---------|
| 초단편 208개 카운팅 | CHILD 청크가 구조상 짧음에도 short 판정 포함 | `short_chunk_rate` 계산을 ROOT만 대상으로 변경 |
| parent_child 품질점수 0점 (A) | `compare` dict에 `parent_child` 미포함 | `compare` dict에 추가, 4열로 확장 |
| parent_child 품질점수 0점 (B) | 품질점수 계산이 child 청크까지 포함해 short 과다 집계 | `_quality_score()`에서 `parent_child`는 parent만 기준 |

> **병합 작업(①) 완료 후** parent_child 품질점수 변화:
> - 병합 전: ROOT 35개 short → max(0, 100 - 35×3) = **0점**
> - 병합 후: ROOT ~5개 short → max(0, 100 - 5×3) = **85점** (예상)