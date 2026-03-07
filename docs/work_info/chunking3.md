# RAG 파이프라인 고도화 프롬프트 모음
> **분석 대상:** `사내_경비_지출_관리_규정_v2.0_확장판.txt` 기반 RAG  
> **범위:** 청킹(Chunking) → 저장(pgvector) → 검색(Retrieval) → 재정렬(Rerank) 전 구간  
> **구성:** 총 5개 독립 프롬프트 (Cursor에 개별 전달)

---

## 📊 현행 RAG 파이프라인 종합 진단

```
[현행 흐름]
.txt 파일
  └─ hierarchical_chunk()          ← 조문/항 분리 ✅ 구조는 좋으나 3가지 결함 존재
       └─ save_hierarchical_chunks()
            ├─ text-embedding-3-large (3072차원, halfvec)  ← 임베딩 대상이 search_text ✅
            ├─ to_tsvector('simple', search_text)          ← BM25용 tsvector ✅
            └─ embedding_az halfvec(3072) + HNSW 인덱스    ← 저장 구조 ✅

[현행 검색 흐름]
search_policy_chunks()
  ├─ _search_bm25()      ← ts_rank_cd + GIN 인덱스 ✅
  ├─ _search_dense()     ← pgvector cosine, query 구성 방식 ⚠️ 개선 여지
  ├─ _reciprocal_rank_fusion()  ← RRF k=60, 가중치 50/50 ⚠️ 고정값 문제
  ├─ _enrich_with_parent_context()  ← CLAUSE→ARTICLE prepend ✅
  └─ rerank_with_cross_encoder()    ← Dongjin-kr/ko-reranker ⚠️ query 품질 문제
```

### 발견된 고도화 포인트 5가지

| # | 위치 | 문제 | 심각도 |
|---|------|------|--------|
| 1 | `rag_chunk_lab_service.py` | 절(節) 구조 미파싱 — `제N절` 헤더가 `semantic_group`에 반영 안 됨 | 높음 |
| 2 | `policy_service.py` | Dense 검색 쿼리 텍스트 품질 저하 — 키워드 단순 concat으로 의미 손실 | 높음 |
| 3 | `policy_service.py` | RRF 가중치 50/50 고정 — 케이스 유형에 따른 동적 조정 없음 | 중간 |
| 4 | `policy_service.py` | `build_policy_keywords()`의 tsvector 쿼리 한계 — 형태소 분석 없이 단순 prefix 매칭 | 중간 |
| 5 | `chunking_pipeline.py` | `search_text` 임베딩 vs `chunk_text` 검색 불일치 — BM25는 search_text, 표시는 chunk_text | 낮음 |

---
---

## 🔴 프롬프트 1 — 청킹: 절(節) 계층 파싱 + semantic_group 완성

### 배경 및 문제

현재 `hierarchical_chunk()`는 **장(章)** 은 파싱하지만 **절(節)** 을 무시한다.

```python
# 현재: 장만 추적
_CHAPTER_PATTERN = re.compile(r"^(제\s*\d+\s*장[^\n]*)", re.MULTILINE)
_SECTION_PATTERN = re.compile(r"^(제\s*\d+\s*절[^\n]*)", re.MULTILINE)  # ← 정의만 있고 실제 사용 안 됨!
```

`사내_경비_지출_관리_규정_v2.0_확장판.txt` 문서 구조:

```
제1장 총칙
  제1절 목적 및 적용 범위
    제1조 (목적)
    제2조 (적용 범위)
  제2절 정의
    제3조 (용어 정의)
제2장 경비 사용 기준
  제1절 일반 기준
    ...
```

절(節)이 무시되면 `semantic_group`이 `"제1장 총칙"` 수준에서 멈춰 **같은 장 안의 서로 다른 절**에 속한 조문들이 동일 그룹으로 검색된다. 이는 특히 "제3장 경비 유형별 기준" 같은 장이 여러 절로 나뉠 때 검색 정밀도를 크게 떨어뜨린다.

추가로 `contextual_header`가 `[제1장 총칙 > 제23조 > (식대)]` 형태인데, 절 정보가 없어 중간 맥락이 빠진다. 이상적인 형태: `[제3장 경비 유형별 기준 > 제1절 식대·접대비 > 제23조 > (식대)]`.

---

### 구현 목표

`hierarchical_chunk()` 내부에서 절(節) 헤더를 파싱하여 `semantic_group`과 `contextual_header`에 반영한다.

---

### 수정 파일 및 상세 명세

#### `services/rag_chunk_lab_service.py` — `hierarchical_chunk()` 수정

**위치:** `chapter_splits` 루프 내부, `article_splits` 처리 전에 절 분리 추가

```python
def hierarchical_chunk(text: str) -> list[ChunkNode]:
    """
    규정집 텍스트를 조문-항/호 계층으로 분리.
    파싱 순서: 장(章) → 절(節) → 조(條) → 항/호(CLAUSE)
    """
    articles: list[dict[str, Any]] = []
    current_chapter = ""
    current_section = ""  # ← 신규: 현재 절 추적

    chapter_splits = _CHAPTER_PATTERN.split(text)

    for part in chapter_splits:
        part = part.strip()
        if not part:
            continue

        # 장 헤더 감지
        if _CHAPTER_PATTERN.fullmatch(part):
            current_chapter = part.strip()
            current_section = ""  # 장이 바뀌면 절 초기화
            continue

        # ── 신규: 절(節) 분리 ────────────────────────────────────────────
        # 각 장 내부를 절 단위로 다시 분리
        section_splits = _SECTION_PATTERN.split(part)

        for section_part in section_splits:
            section_part = section_part.strip()
            if not section_part:
                continue

            # 절 헤더 감지
            if _SECTION_PATTERN.fullmatch(section_part):
                current_section = section_part.strip()
                continue

            # 조문 분리 (기존 로직 유지, current_section 추가 전달)
            article_splits = _ARTICLE_PATTERN.split(section_part)
            i = 0
            while i < len(article_splits):
                segment = article_splits[i].strip()
                if not segment:
                    i += 1
                    continue

                if _ARTICLE_PATTERN.fullmatch(segment):
                    article_header = segment
                    i += 1
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

                    # ── 신규: 절 정보를 contextual_header에 포함 ──────────
                    contextual_header = _build_contextual_header(
                        article_num,
                        article_title,
                        chapter_context=current_chapter,
                        section_context=current_section,  # ← 신규 파라미터
                    )
                    # ────────────────────────────────────────────────────────

                    clauses = _split_into_clauses(article_body)

                    articles.append({
                        "regulation_article": article_num,
                        "full_title": full_title,
                        "article_header": article_header,
                        "body": article_body,
                        "contextual_header": contextual_header,
                        "current_chapter": current_chapter,
                        "current_section": current_section,  # ← 신규
                        "clauses": clauses,
                    })
                else:
                    i += 1
        # ─────────────────────────────────────────────────────────────────

    # 초단편 ARTICLE 병합 (기존 로직 유지)
    articles = _merge_short_articles(articles, parent_min=PARENT_MIN)

    # ChunkNode 변환 (신규: semantic_group에 절 정보 포함)
    nodes: list[ChunkNode] = []
    chunk_index = 0
    for art in articles:
        chapter = art.get("current_chapter") or ""
        section = art.get("current_section") or ""

        # semantic_group: "제3장 경비 유형별 기준 > 제1절 식대·접대비" 형태
        if chapter and section:
            semantic_group = f"{chapter} > {section}"
        elif chapter:
            semantic_group = chapter
        else:
            semantic_group = section

        # ... (기존 ChunkNode 생성 로직 유지, semantic_group만 교체)
        article_node = ChunkNode(
            node_type="ARTICLE",
            regulation_article=art.get("regulation_article"),
            regulation_clause=None,
            parent_title=art.get("full_title"),
            chunk_text=f"{art['article_header']}\n{art['body']}".strip(),
            search_text=art.get("body") or "",
            contextual_header=art.get("contextual_header") or "",
            chunk_index=chunk_index,
            semantic_group=semantic_group,  # ← 절 포함된 그룹명
            merged_with=art.get("merged_with"),
        )
        # ... 나머지 CLAUSE 노드 생성 로직은 동일
```

#### `_build_contextual_header()` 시그니처 확장

```python
def _build_contextual_header(
    article: str,
    title: str,
    chapter_context: str = "",
    section_context: str = "",   # ← 신규 파라미터
) -> str:
    """Contextual RAG: 장 > 절 > 조 > 제목 형태의 맥락 접두어."""
    parts = []
    if chapter_context:
        parts.append(chapter_context)
    if section_context:           # ← 신규
        parts.append(section_context)
    if article:
        parts.append(article)
    if title:
        parts.append(title)
    if parts:
        return f"[{' > '.join(parts)}] "
    return ""
```

#### DB `metadata_json` 저장 시 `current_section` 포함

```python
# chunking_pipeline.py — save_hierarchical_chunks() 내 meta 딕셔너리
meta = {
    "semantic_group": getattr(node, "semantic_group", "") or "",
    "regulation_article": node.regulation_article or "",
    "current_section": art.get("current_section") or "",  # ← 신규
}
```

---

### 검증 방법

```python
# 절(節)이 있는 규정집 텍스트로 검증
from services.rag_chunk_lab_service import hierarchical_chunk

text = open("규정집/사내_경비_지출_관리_규정_v2.0_확장판.txt", encoding="utf-8").read()
nodes = hierarchical_chunk(text)

# 1. semantic_group에 절 정보가 포함되어야 함
for n in nodes:
    if ">" in (n.semantic_group or ""):
        print(f"절 포함 OK: {n.semantic_group}")
        break

# 2. contextual_header에 절 정보 포함 확인
clause_with_section = [n for n in nodes if "절" in (n.contextual_header or "")]
print(f"절 포함 contextual_header: {len(clause_with_section)}개")

# 3. 기존 ARTICLE/CLAUSE 개수 regression 없어야 함
articles = [n for n in nodes if n.node_type == "ARTICLE"]
clauses = [n for n in nodes if n.node_type == "CLAUSE"]
print(f"ARTICLE: {len(articles)}, CLAUSE: {len(clauses)}")
```

---
---

## 🟠 프롬프트 2 — Dense 검색 쿼리: 자연어 변환 + HyDE (가설 문서 임베딩)

### 배경 및 문제

현재 `_search_dense()`의 쿼리 텍스트 구성:

```python
# 현재: 단순 concat — 의미적으로 빈약한 쿼리
keywords = build_policy_keywords(body_evidence)
case_type = body_evidence.get("case_type") or ""
merchant = body_evidence.get("merchantName") or ""
query_text = f"{case_type} {merchant} {' '.join(keywords[:10])}".strip()
# 결과 예시: "HOLIDAY_USAGE 스타벅스 휴일 주말 공휴일 식대 심야 5813 스타벅스 커피"
```

이 방식의 문제:
1. **"HOLIDAY_USAGE"** 같은 내부 코드가 임베딩 입력에 들어가 벡터 방향 오염
2. 키워드 나열은 자연어 문장보다 임베딩 품질이 낮음 — 문장으로 된 규정 텍스트와의 코사인 유사도 저하
3. `text-embedding-3-large`는 자연어 문장에서 최적 성능 발휘

**HyDE (Hypothetical Document Embedding) 적용 근거:**  
Dense 검색에서 "쿼리 → 규정 청크" 직접 매핑 대신,  
"쿼리 → 가설 규정 문장 생성 → 가설 문장 임베딩 → 규정 청크" 방식이 정답률을 크게 향상시킨다.  
(Gao et al., 2022: Precise Zero-Shot Dense Retrieval without Relevance Labels)

---

### 구현 목표

1. **1단계:** 쿼리 텍스트를 자연어 문장으로 변환 (LLM 불필요, 템플릿 기반)
2. **2단계 (선택):** HyDE — LLM으로 가설 규정 문장을 생성하여 임베딩

---

### 수정 파일 및 상세 명세

#### `services/policy_service.py` — `_build_dense_query()` 신규 함수 추가

**위치:** `_search_dense()` 함수 바로 위에 신규 추가

```python
def _build_dense_query(body_evidence: dict[str, Any]) -> str:
    """
    Dense 검색용 자연어 쿼리 문장 생성.

    전략:
    1. 케이스 유형별 자연어 템플릿 적용 (LLM 불필요)
    2. 전표 사실(시간, 금액, MCC, 근태)을 문장에 삽입
    3. 내부 코드(HOLIDAY_USAGE, LIMIT_EXCEED 등)는 제외

    예시 출력:
    "휴일(토요일)에 스타벅스(MCC 5813, 음료)에서 15,000원을 사용한 건에 대해
     적용되는 식대·경비 규정 조항을 찾아야 한다. 근태 상태는 휴가(LEAVE)이다."
    """
    case_type = str(body_evidence.get("case_type") or body_evidence.get("intended_risk_type") or "")
    merchant = body_evidence.get("merchantName") or "거래처 미상"
    amount = body_evidence.get("amount")
    amount_str = f"{int(amount):,}원" if amount else "금액 미상"
    is_holiday = bool(body_evidence.get("isHoliday"))
    hr_status = str(body_evidence.get("hrStatus") or "").upper()
    mcc_code = body_evidence.get("mccCode") or ""
    mcc_name = body_evidence.get("mccName") or ""
    occurred_at = str(body_evidence.get("occurredAt") or "")
    hour = None
    try:
        hour = int(occurred_at[11:13])
    except Exception:
        pass
    is_night = hour is not None and (hour >= 22 or hour < 6)

    # 케이스별 자연어 템플릿
    _CASE_TEMPLATES: dict[str, str] = {
        "HOLIDAY_USAGE": (
            "주말 또는 공휴일 경비 사용 건으로, {merchant}에서 {amount}을 지출하였다. "
            "{hr_hint}"
            "이 지출에 적용되는 휴일 경비 사용 제한 규정과 식대 규정을 찾아야 한다."
        ),
        "LIMIT_EXCEED": (
            "{merchant}에서 {amount}을 지출하였으며, 예산 한도를 초과한 것으로 확인되었다. "
            "금액 구간별 승인 기준과 예산 초과 처리 절차 규정을 찾아야 한다."
        ),
        "PRIVATE_USE_RISK": (
            "{merchant}(MCC: {mcc})에서 {amount}을 사용하였으나 사적 사용 여부가 불명확하다. "
            "업무 관련성 증빙 기준과 사적 사용 금지 규정을 찾아야 한다."
        ),
        "UNUSUAL_PATTERN": (
            "{merchant}에서 {amount}을 지출하였으며 비정상 패턴이 감지되었다. "
            "{night_hint}"
            "관련 경비 지출 규정과 심야 시간대 지출 기준을 찾아야 한다."
        ),
    }

    hr_hint = ""
    if hr_status in {"LEAVE", "OFF", "VACATION"}:
        hr_label = {"LEAVE": "휴가·결근", "OFF": "휴무", "VACATION": "휴가"}.get(hr_status, hr_status)
        hr_hint = f"해당 일자 근태 상태는 {hr_label}({hr_status})이다. "

    night_hint = ""
    if is_night and hour is not None:
        night_hint = f"결제 시각은 {hour:02d}시로 심야 시간대에 해당한다. "

    mcc_display = f"{mcc_name}({mcc_code})" if mcc_name and mcc_code else (mcc_code or "")

    template = _CASE_TEMPLATES.get(case_type, (
        "{merchant}에서 {amount}을 지출한 건에 대해 적용 가능한 사내 경비 지출 규정을 찾아야 한다. "
        "{hr_hint}{night_hint}"
    ))

    query = template.format(
        merchant=merchant,
        amount=amount_str,
        hr_hint=hr_hint,
        night_hint=night_hint,
        mcc=mcc_display,
    ).strip()

    # 공휴일 여부 보강
    if is_holiday and "공휴일" not in query and "휴일" not in query:
        query += " 해당 날은 공휴일 또는 주말이다."

    return query


def _build_dense_query_with_hyde(
    body_evidence: dict[str, Any],
    *,
    llm_client: Any = None,
) -> str:
    """
    HyDE (Hypothetical Document Embedding) 적용 버전.
    LLM으로 가설 규정 문장을 생성하여 임베딩에 사용.
    LLM 미설정 또는 실패 시 _build_dense_query()로 fallback.

    HyDE 원리:
    - 일반 쿼리: "휴일 식대 규정" → 임베딩 → 규정 청크와 코사인 비교
    - HyDE: "휴일에는 식대 사용이 제한되며 제23조에 따라 예외 승인이 필요하다." → 임베딩
    - 가설 문장이 실제 규정 텍스트와 더 유사한 임베딩 공간에 위치
    """
    base_query = _build_dense_query(body_evidence)

    if llm_client is None or not settings.enable_hyde_query:
        return base_query

    try:
        system_prompt = (
            "당신은 한국 기업의 사내 경비 지출 관리 규정 전문가다.\n"
            "아래 전표 상황에 대해 실제 사내 규정집에 나올 법한 조문 형태의 문장을 1~2문장 작성하라.\n"
            "반드시 '제N조' 형식의 조문 번호, '①②③' 형식의 항 번호를 포함하라.\n"
            "실제 규정집 문체와 유사하게 작성할 것. JSON, 코드블록 사용 금지."
        )
        user_prompt = f"전표 상황:\n{base_query}\n\n이 상황에 적용될 가설 규정 문장:"

        response = llm_client.chat.completions.create(
            model=settings.reasoning_llm_model,
            max_tokens=150,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        hyde_text = (response.choices[0].message.content or "").strip()
        # 가설 문장을 원래 쿼리와 결합 (임베딩 시 두 관점 모두 반영)
        return f"{base_query}\n\n[가설 규정 문장] {hyde_text}"
    except Exception:
        return base_query
```

#### `services/policy_service.py` — `_search_dense()` 수정

```python
def _search_dense(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    limit: int = 20,
    effective_date: Any = None,
    embed_column: str = settings.rag_embedding_column,
    use_hyde: bool = False,           # ← 신규: HyDE 활성화 옵션
    llm_client: Any = None,           # ← 신규: HyDE용 LLM 클라이언트
) -> list[dict[str, Any]]:
    """Dense 벡터 검색. 자연어 쿼리 + 선택적 HyDE 적용."""
    ...
    # 기존 단순 concat 쿼리 → 자연어 변환으로 교체
    if use_hyde:
        query_text = _build_dense_query_with_hyde(body_evidence, llm_client=llm_client)
    else:
        query_text = _build_dense_query(body_evidence)  # ← 기존 로직 교체

    if not query_text:
        return []

    vectors = embed_texts([query_text])
    # ... 이하 기존 SQL 로직 유지
```

#### `utils/config.py` — HyDE 활성화 플래그 추가

```python
enable_hyde_query: bool = os.getenv("ENABLE_HYDE_QUERY", "false").lower() == "true"
```

#### `.env.example` 추가

```
# HyDE (Hypothetical Document Embedding) 활성화 (Dense 검색 품질 향상, LLM 호출 비용 발생)
ENABLE_HYDE_QUERY=false
```

---

### 검증 방법

```python
from services.policy_service import _build_dense_query

# 1. HOLIDAY_USAGE 케이스 자연어 쿼리 확인
body = {
    "case_type": "HOLIDAY_USAGE",
    "merchantName": "스타벅스",
    "amount": 15000,
    "isHoliday": True,
    "hrStatus": "LEAVE",
    "mccCode": "5814",
}
query = _build_dense_query(body)
print(query)
# 기대 출력: "주말 또는 공휴일 경비 사용 건으로, 스타벅스에서 15,000원을 지출하였다.
#             해당 일자 근태 상태는 휴가·결근(LEAVE)이다. ..."
assert "HOLIDAY_USAGE" not in query, "내부 코드가 쿼리에 포함되면 안 됨"
assert "스타벅스" in query
assert "15,000원" in query
assert "LEAVE" in query or "휴가" in query
```

---
---

## 🟡 프롬프트 3 — RRF 가중치 동적 조정 + Semantic_group 필터링

### 배경 및 문제

현재 RRF 가중치가 `bm25_weight=0.5, dense_weight=0.5`로 **모든 케이스에 고정**이다.

```python
# 현재: 항상 50:50
fused = _reciprocal_rank_fusion(bm25_results, dense_results, k=60)

def _reciprocal_rank_fusion(..., bm25_weight=0.5, dense_weight=0.5):
    ...
```

**케이스별 최적 가중치는 다르다:**

| 케이스 유형 | 최적 전략 | 이유 |
|------------|----------|------|
| 조문 번호 직접 언급 (`제23조`) | BM25 강화 (70:30) | 정확한 토큰 매칭이 중요 |
| 의미 유사 검색 (`심야 식사 경비`) | Dense 강화 (30:70) | "심야"↔"야간", "식사"↔"식대" 동의어 처리 |
| `HOLIDAY_USAGE` | BM25 강화 (65:35) | "휴일", "주말" 키워드 정확 매칭 우선 |
| `LIMIT_EXCEED` | Dense 강화 (40:60) | 금액 구간 설명이 의미적으로 다양 |

또한 현재 검색이 **모든 doc_id의 청크를 혼합 검색**하기 때문에 규정집이 여러 개 있을 때 관련 없는 문서의 청크가 상위에 올 수 있다.

---

### 구현 목표

1. 케이스 유형별로 BM25/Dense 가중치를 동적 결정
2. `semantic_group` 기반 필터링 — 관련 장(章)/절(節)로 검색 범위 좁힘

---

### 수정 파일 및 상세 명세

#### `services/policy_service.py` — 동적 RRF 가중치 함수 추가

```python
def _get_rrf_weights(body_evidence: dict[str, Any]) -> tuple[float, float]:
    """
    케이스 유형과 컨텍스트에 따라 BM25/Dense 가중치를 동적 결정.
    반환: (bm25_weight, dense_weight)

    설계 원칙:
    - 조문 번호 직접 참조가 있으면 BM25 강화
    - 의미적 유사도가 필요한 케이스(UNUSUAL_PATTERN)는 Dense 강화
    - HOLIDAY_USAGE, LIMIT_EXCEED는 키워드 명확 → BM25 약간 우위
    """
    case_type = str(body_evidence.get("case_type") or body_evidence.get("intended_risk_type") or "")

    # 케이스별 기본 가중치
    _CASE_WEIGHTS: dict[str, tuple[float, float]] = {
        "HOLIDAY_USAGE":    (0.65, 0.35),  # 휴일/주말 정확 키워드 매칭 우선
        "LIMIT_EXCEED":     (0.45, 0.55),  # 금액 구간 설명 다양 → Dense 약간 우위
        "PRIVATE_USE_RISK": (0.50, 0.50),  # 균형
        "UNUSUAL_PATTERN":  (0.35, 0.65),  # 비정상 패턴 → 의미 유사도 중요
        "NORMAL_BASELINE":  (0.50, 0.50),
    }

    bm25_w, dense_w = _CASE_WEIGHTS.get(case_type, (0.50, 0.50))

    # 조문 번호 직접 참조 여부 보정 (regulation_article_hint가 있으면 BM25 강화)
    if body_evidence.get("_regulation_article_hint"):
        bm25_w = min(0.80, bm25_w + 0.15)
        dense_w = 1.0 - bm25_w

    # 심야 시간대 → 특수 조항 검색 → Dense 보강
    occurred_at = str(body_evidence.get("occurredAt") or "")
    try:
        hour = int(occurred_at[11:13])
        if hour >= 22 or hour < 6:
            dense_w = min(0.70, dense_w + 0.10)
            bm25_w = 1.0 - dense_w
    except Exception:
        pass

    return round(bm25_w, 2), round(dense_w, 2)


def _get_semantic_group_filter(body_evidence: dict[str, Any]) -> str | None:
    """
    케이스 유형에 따라 검색할 장(章)/절(節) semantic_group 패턴 반환.
    None이면 전체 검색.

    사내_경비_지출_관리_규정_v2.0_확장판.txt 문서 구조 기준:
    - 제3장: 경비 유형별 기준 (식대, 접대비, 교통비 등)
    - 제4장: 승인 절차 및 한도
    - 제5장: 위반 처리 기준
    """
    case_type = str(body_evidence.get("case_type") or "")

    # 케이스별 우선 검색 장
    _CASE_GROUP_HINTS: dict[str, list[str]] = {
        "HOLIDAY_USAGE":    ["제3장", "제4장"],  # 경비 유형 + 승인 절차
        "LIMIT_EXCEED":     ["제4장", "제2장"],  # 승인 한도 + 일반 기준
        "PRIVATE_USE_RISK": ["제3장", "제5장"],  # 경비 유형 + 위반 처리
        "UNUSUAL_PATTERN":  ["제3장", "제5장"],
    }

    groups = _CASE_GROUP_HINTS.get(case_type)
    if not groups:
        return None

    # SQL LIKE 패턴 (metadata_json의 semantic_group 필드 검색용)
    # 예: "제3장%" OR "제4장%"
    return groups  # 리스트로 반환, SQL에서 처리


def _search_bm25_with_group_filter(
    db: Session,
    body_evidence: dict[str, Any],
    *,
    limit: int = 20,
    effective_date: Any = None,
    group_filter: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    BM25 검색 + semantic_group 필터링.
    group_filter가 있으면 해당 장/절 내에서만 검색.
    """
    keywords = build_policy_keywords(body_evidence)
    if not keywords:
        return []

    # ... (기존 ts_terms 구성 로직 유지)

    # semantic_group 필터 조건 추가
    group_filter_sql = ""
    group_params: dict[str, str] = {}
    if group_filter:
        group_conditions = []
        for idx, grp in enumerate(group_filter):
            key = f"grp{idx}"
            group_conditions.append(
                f"(metadata_json->>'semantic_group' LIKE :{key})"
            )
            group_params[key] = f"{grp}%"
        group_filter_sql = f"AND ({' OR '.join(group_conditions)})"

    sql = text(f"""
        SELECT
            chunk_id, doc_id, regulation_article, regulation_clause,
            parent_title, chunk_text, search_text, node_type, parent_id,
            version, effective_from, effective_to, page_no, chunk_index,
            metadata_json,
            ts_rank_cd(search_tsv, query) AS bm25_score
        FROM dwp_aura.rag_chunk,
             to_tsquery('simple', :ts_query) AS query
        WHERE tenant_id = :tenant_id
          AND is_active = true
          AND search_tsv @@ query
          AND (:effective_date IS NULL OR coalesce(effective_from, :effective_date) <= :effective_date)
          AND (:effective_date IS NULL OR coalesce(effective_to, :effective_date) >= :effective_date)
          {group_filter_sql}
        ORDER BY bm25_score DESC
        LIMIT :limit
    """)

    params = {
        "tenant_id": settings.default_tenant_id,
        "ts_query": ts_query,
        "effective_date": effective_date,
        "limit": limit,
        **group_params,
    }
    rows = db.execute(sql, params).mappings().all()
    return [dict(row) for row in rows]
```

#### `services/policy_service.py` — `search_policy_chunks()` 수정

```python
def search_policy_chunks(db: Session, body_evidence: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    # ... (기존 effective_date 계산 유지)

    candidate_limit = max(limit * 6, 20)

    # ── 동적 가중치 계산 ────────────────────────────────────────────────
    bm25_weight, dense_weight = _get_rrf_weights(body_evidence)
    group_filter = _get_semantic_group_filter(body_evidence)

    # ── BM25 검색 (semantic_group 필터 적용) ────────────────────────────
    bm25_results = _search_bm25_with_group_filter(
        db, body_evidence,
        limit=candidate_limit,
        effective_date=effective_date,
        group_filter=group_filter,
    )
    # group_filter 적용 결과가 부족하면 전체 검색으로 보완
    if group_filter and len(bm25_results) < limit:
        bm25_fallback = _search_bm25(db, body_evidence, limit=candidate_limit, effective_date=effective_date)
        existing_ids = {r["chunk_id"] for r in bm25_results}
        bm25_results.extend(r for r in bm25_fallback if r["chunk_id"] not in existing_ids)

    # ── Dense 검색 ───────────────────────────────────────────────────────
    dense_results = _search_dense(db, body_evidence, limit=candidate_limit, effective_date=effective_date)

    # ── RRF 융합 (동적 가중치 적용) ──────────────────────────────────────
    if bm25_results and dense_results:
        fused = _reciprocal_rank_fusion(
            bm25_results, dense_results,
            k=60,
            bm25_weight=bm25_weight,   # ← 동적 가중치
            dense_weight=dense_weight,
        )
    # ... (이하 기존 fallback 로직 유지)
```

---

### 검증 방법

```python
from services.policy_service import _get_rrf_weights

# HOLIDAY_USAGE → BM25 강화
w = _get_rrf_weights({"case_type": "HOLIDAY_USAGE"})
assert w[0] > 0.60, f"HOLIDAY_USAGE BM25 가중치: {w[0]}"

# UNUSUAL_PATTERN → Dense 강화
w = _get_rrf_weights({"case_type": "UNUSUAL_PATTERN"})
assert w[1] > 0.60, f"UNUSUAL_PATTERN Dense 가중치: {w[1]}"

# 가중치 합 = 1.0
w = _get_rrf_weights({"case_type": "LIMIT_EXCEED"})
assert abs(w[0] + w[1] - 1.0) < 0.01
```

---
---

## 🟢 프롬프트 4 — 한국어 형태소 분석 기반 tsvector 생성 (pg_bigm / 쿼리 확장)

### 배경 및 문제

현재 `to_tsvector('simple', search_text)`는 **공백 기준 토큰 분리**만 수행한다.

```sql
-- 현재: 'simple' config = 공백 분리만
to_tsvector('simple', '업무상 식대는 인당 기준한도를 충족하여야')
-- 결과: '인당' | '기준한도를' | '충족하여야' | '업무상' | '식대는'
-- ← '기준한도를'이 '기준한도'와 매칭 안 됨 (조사 때문에)
```

**문제:**
- `"식대"` 검색 시 `"식대는"`, `"식대를"`, `"식대의"` 모두 매칭 안 됨
- `"한도"` 검색 시 `"기준한도"`, `"한도초과"` 매칭 부재
- tsquery의 prefix 매칭(`식대:*`)으로 부분 보완하지만 조사 처리 불완전

**개선 방향:** `pg_bigm` 확장 또는 `search_text`에 형태소 분리된 토큰을 별도 컬럼에 저장하여 검색 품질을 높인다.

---

### 구현 목표

**두 가지 방법 중 선택 (환경에 따라):**

- **방법 A (pg_bigm 활성화 가능한 경우):** `pg_bigm` GIN 인덱스로 한국어 바이그램 검색
- **방법 B (pg_bigm 없는 경우):** 토큰 확장 컬럼(`search_tokens`) 추가 + 동의어 사전

---

### 수정 파일 및 상세 명세

#### 방법 B: 동의어/형태소 확장 컬럼 방식 (추천 — 추가 DB 확장 불필요)

##### DB 마이그레이션 (새 컬럼 추가)

```sql
-- 형태소/동의어 확장 텍스트 저장용 컬럼
ALTER TABLE dwp_aura.rag_chunk
  ADD COLUMN IF NOT EXISTS search_tokens text;  -- 형태소 분리 + 동의어 확장된 토큰 문자열

-- 기존 GIN 인덱스와 별도로 토큰 기반 GIN 인덱스 추가
CREATE INDEX IF NOT EXISTS ix_rag_chunk_search_tokens_gin
  ON dwp_aura.rag_chunk
  USING gin (to_tsvector('simple', coalesce(search_tokens, '')));
```

##### `services/rag_chunk_lab_service.py` — 동의어 사전 + 토큰 확장

```python
# ── 한국어 경비 규정 동의어 사전 ─────────────────────────────────────────
# 검색어 → 규정 텍스트 내 표현 매핑
_SYNONYM_MAP: dict[str, list[str]] = {
    "식대":      ["식비", "식사비", "음식비", "식음료비"],
    "심야":      ["야간", "야심", "23시", "22시", "자정"],
    "휴일":      ["주말", "공휴일", "토요일", "일요일", "휴무일"],
    "한도":      ["기준한도", "상한", "한도액", "허용한도", "초과"],
    "접대비":    ["접대", "업무추진비", "외부 미팅비"],
    "교통비":    ["출장비", "이동비", "택시비", "대중교통"],
    "승인":      ["결재", "허가", "인가", "사전승인"],
    "증빙":      ["영수증", "청구서", "카드전표"],
    "사적":      ["개인적", "사적 사용", "업무 외"],
    "고위험":    ["제한업종", "주류", "유흥", "도박"],
}


def _expand_tokens(text: str) -> str:
    """
    텍스트에서 동의어 확장 토큰을 생성.
    search_tokens 컬럼 저장용.
    """
    tokens = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", text))
    expanded = set(tokens)

    for canonical, synonyms in _SYNONYM_MAP.items():
        # 원문에 정규어가 있으면 동의어 추가
        if canonical in tokens:
            expanded.update(synonyms)
        # 원문에 동의어가 있으면 정규어 추가
        for syn in synonyms:
            if syn in tokens or any(syn in t for t in tokens):
                expanded.add(canonical)

    # 조사 제거 간단 처리 (완전한 형태소 분석 없이)
    _JOSA_PATTERNS = ["은", "는", "이", "가", "을", "를", "의", "에", "에서", "으로", "로", "와", "과"]
    for token in list(tokens):
        for josa in _JOSA_PATTERNS:
            if token.endswith(josa) and len(token) > len(josa) + 1:
                expanded.add(token[:-len(josa)])
                break

    return " ".join(sorted(expanded))
```

##### `services/chunking_pipeline.py` — `search_tokens` 저장 추가

```python
# save_hierarchical_chunks() 내 INSERT SQL에 search_tokens 추가
insert_sql = text("""
    INSERT INTO dwp_aura.rag_chunk (
        ...,
        search_tokens,   -- ← 신규
        ...
    ) VALUES (
        ...,
        :search_tokens,  -- ← 신규
        ...
    ) RETURNING chunk_id
""")

# 각 노드에 대해 search_tokens 생성
from services.rag_chunk_lab_service import _expand_tokens

for node in article_nodes:
    search_tokens = _expand_tokens(node.search_text)
    row = db.execute(insert_sql, {
        ...,
        "search_tokens": search_tokens,  # ← 신규
        ...
    }).fetchone()
```

##### `services/policy_service.py` — BM25 검색 쿼리에 `search_tokens` 컬럼 추가

```python
sql = text("""
    SELECT ...,
        ts_rank_cd(
            setweight(to_tsvector('simple', coalesce(search_tsv, '')), 'A') ||
            setweight(to_tsvector('simple', coalesce(search_tokens, '')), 'B'),
            query
        ) AS bm25_score
    FROM dwp_aura.rag_chunk,
         to_tsquery('simple', :ts_query) AS query
    WHERE tenant_id = :tenant_id
      AND is_active = true
      AND (
        search_tsv @@ query
        OR to_tsvector('simple', coalesce(search_tokens, '')) @@ query  -- ← 토큰 확장 매칭
      )
    ...
""")
```

#### `build_policy_keywords()` — 동의어 자동 확장 추가

```python
def build_policy_keywords(body_evidence: dict[str, Any]) -> list[str]:
    # ... 기존 로직 유지 ...
    
    # ── 신규: 동의어 확장 ──────────────────────────────────────────────
    from services.rag_chunk_lab_service import _SYNONYM_MAP
    expanded_from_synonyms: list[str] = []
    for kw in list(keywords):
        for canonical, synonyms in _SYNONYM_MAP.items():
            if kw == canonical or kw in synonyms:
                expanded_from_synonyms.append(canonical)
                expanded_from_synonyms.extend(synonyms)
    for kw in expanded_from_synonyms:
        if kw not in keywords:
            keywords.append(kw)
    # ──────────────────────────────────────────────────────────────────

    return keywords
```

---

### 검증 방법

```python
from services.rag_chunk_lab_service import _expand_tokens

# 동의어 확장 테스트
tokens = _expand_tokens("업무상 식대는 인당 기준한도를 충족하여야")
assert "식비" in tokens, "식대 동의어 확장 실패"
assert "식대" in tokens   # 원본 유지 (조사 제거됨)
assert "기준한도" in tokens  # 원본 유지
assert "한도" in tokens   # 상위어 추가

# keyword 확장 테스트
from services.policy_service import build_policy_keywords
kws = build_policy_keywords({"case_type": "HOLIDAY_USAGE", "isHoliday": True})
assert "주말" in kws
assert "공휴일" in kws
```

---
---

## 🔵 프롬프트 5 — Cross-Encoder Rerank 품질 강화: 쿼리 개선 + 배치 크기 최적화 + Fallback LLM Rerank

### 배경 및 문제

현재 `rerank_with_cross_encoder()`의 쿼리 구성:

```python
# 현재: keywords 단순 concat (최대 12개)
keywords = build_policy_keywords(body_evidence)
query_str = " ".join(keywords[:12])
enriched = rerank_with_cross_encoder(enriched, query_str)
```

**3가지 문제:**

1. **쿼리가 키워드 나열** — Cross-Encoder는 (쿼리, 패시지) 쌍의 관련성을 판단하므로 자연어 문장이 훨씬 효과적
2. **Rerank 입력이 너무 많거나 너무 적음** — `candidate_limit = max(limit * 6, 20)`이지만 Cross-Encoder는 상위 20~30개만 처리해도 충분 (과도한 입력 = 성능 저하)
3. **Cross-Encoder 미설치 시 아무 fallback 없음** — `except Exception: pass`로 silently 통과

---

### 구현 목표

1. Cross-Encoder 쿼리를 `_build_dense_query()`와 동일한 자연어 문장으로 통일
2. Rerank 입력 청크 수를 적정 범위로 제한 (15~25개)
3. Cross-Encoder 미설치 시 경량 LLM Rerank fallback 추가

---

### 수정 파일 및 상세 명세

#### `services/policy_service.py` — Cross-Encoder 쿼리 개선 + 입력 제한

```python
def search_policy_chunks(db: Session, body_evidence: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    # ... (기존 검색 + RRF 융합 유지)

    enriched = _enrich_with_parent_context(db, fused[:candidate_limit])

    # ── Cross-Encoder Rerank: 자연어 쿼리 + 입력 수 제한 ────────────────
    # 기존: keywords[:12] 단순 concat → 교체: 자연어 쿼리 문장 사용
    from services.policy_service import _build_dense_query
    rerank_query = _build_dense_query(body_evidence)  # ← 자연어 쿼리 사용

    # Cross-Encoder 최적 입력: 상위 20개 (너무 많으면 오히려 성능 저하)
    RERANK_INPUT_LIMIT = min(len(enriched), 25)
    rerank_input = enriched[:RERANK_INPUT_LIMIT]

    try:
        from services.retrieval_quality import rerank_with_cross_encoder
        reranked = rerank_with_cross_encoder(rerank_input, rerank_query)
        # Rerank 결과 뒤에 누락된 나머지 추가 (limit 만족을 위해)
        reranked_ids = {r.get("chunk_id") for r in reranked}
        remaining = [r for r in enriched[RERANK_INPUT_LIMIT:] if r.get("chunk_id") not in reranked_ids]
        enriched = reranked + remaining
    except Exception as e:
        # Cross-Encoder 실패 시 LLM Rerank fallback
        try:
            from services.retrieval_quality import rerank_with_llm_fallback
            enriched = rerank_with_llm_fallback(rerank_input, rerank_query, body_evidence=body_evidence)
        except Exception:
            pass  # 모든 fallback 실패 시 RRF 순서 유지
```

#### `services/retrieval_quality.py` — LLM Rerank Fallback + 배치 최적화

```python
def rerank_with_cross_encoder(
    groups: list[dict[str, Any]],
    query: str,
    *,
    model_name: str | None = None,
    batch_size: int = 32,             # ← 신규: 배치 크기 파라미터
) -> list[dict[str, Any]]:
    """
    Cross-Encoder rerank. 한국어 ko-reranker 기본.
    batch_size: CrossEncoder.predict() 배치 크기 (GPU 메모리에 따라 조정)
    """
    if not groups or not query or not query.strip():
        return groups
    model = _get_cross_encoder(model_name or _KO_CROSS_ENCODER_MODEL_NAME)
    if model is None:
        return groups
    try:
        passages = [
            g.get("chunk_text") or g.get("search_text") or ""
            for g in groups
        ]
        if not any(passages):
            return groups
        pairs = [(query.strip(), p) for p in passages]

        # ← 신규: batch_size 적용
        scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)

        for i, g in enumerate(groups):
            g["cross_encoder_score"] = float(scores[i]) if i < len(scores) else 0.0
        return sorted(groups, key=lambda x: x.get("cross_encoder_score", 0), reverse=True)
    except Exception:
        return groups


def rerank_with_llm_fallback(
    groups: list[dict[str, Any]],
    query: str,
    *,
    body_evidence: dict[str, Any] | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """
    Cross-Encoder 미설치 시 LLM 기반 경량 Rerank.

    방식:
    - 상위 10개 청크를 LLM에 전달
    - "다음 케이스에 가장 관련 있는 규정 조항 순서를 JSON 배열로 반환"
    - 반환된 순서로 재정렬

    이 방법은 정확도는 Cross-Encoder보다 낮지만
    sentence-transformers 미설치 환경에서 RRF 순서보다 훨씬 나은 결과를 제공한다.
    """
    from utils.config import settings

    if not settings.openai_api_key or not groups:
        return groups

    try:
        from openai import AzureOpenAI, OpenAI

        base_url = (settings.openai_base_url or "").strip()
        is_azure = ".openai.azure.com" in base_url
        if is_azure:
            azure_ep = base_url.rstrip("/")
            if azure_ep.endswith("/openai/v1"):
                azure_ep = azure_ep[:-len("/openai/v1")]
            client = AzureOpenAI(
                api_key=settings.openai_api_key,
                azure_endpoint=azure_ep,
                api_version=settings.openai_api_version,
            )
        else:
            kw: dict = {"api_key": settings.openai_api_key}
            if base_url:
                kw["base_url"] = base_url
            client = OpenAI(**kw)

        # 상위 10개만 LLM에 전달 (비용 절감)
        top_groups = groups[:10]
        passages_for_llm = []
        for idx, g in enumerate(top_groups):
            article = g.get("regulation_article") or ""
            parent_title = g.get("parent_title") or ""
            text_preview = (g.get("chunk_text") or "")[:150]
            passages_for_llm.append(f"{idx}: [{article} {parent_title}] {text_preview}")

        system_prompt = (
            "당신은 한국 기업 경비 규정 전문가다.\n"
            "아래 케이스 상황에 대해 규정 조항들의 관련성 순서를 JSON 배열로만 응답하라.\n"
            "형식: [0, 2, 1, 5, 3, ...] (인덱스 번호, 관련성 높은 순)\n"
            "불필요한 설명, 마크다운 금지."
        )
        user_prompt = (
            f"케이스: {query}\n\n"
            f"규정 조항 목록:\n" + "\n".join(passages_for_llm) +
            "\n\n관련성 높은 순서 (JSON 배열):"
        )

        response = client.chat.completions.create(
            model=settings.reasoning_llm_model,
            max_tokens=100,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        import json
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        # {"order": [0,2,1,...]} 또는 직접 [0,2,1,...] 형태 처리
        order: list[int] = parsed if isinstance(parsed, list) else parsed.get("order") or []
        order = [int(i) for i in order if isinstance(i, int) and 0 <= i < len(top_groups)]

        if not order:
            return groups

        reranked = []
        seen = set()
        for idx in order:
            item = dict(top_groups[idx])
            item["llm_rerank_score"] = len(order) - order.index(idx)
            reranked.append(item)
            seen.add(idx)

        # 순서에 포함되지 않은 나머지 추가
        for idx, item in enumerate(top_groups):
            if idx not in seen:
                reranked.append(item)

        # LLM이 처리 못한 그룹(top_10 이후) 추가
        reranked.extend(groups[10:])
        return reranked

    except Exception:
        return groups
```

#### `utils/config.py` — LLM Rerank Fallback 활성화 플래그

```python
enable_llm_rerank_fallback: bool = os.getenv("ENABLE_LLM_RERANK_FALLBACK", "true").lower() == "true"
```

---

### 검증 방법

```python
# 1. Cross-Encoder 쿼리가 자연어 문장인지 확인
from services.policy_service import _build_dense_query
query = _build_dense_query({"case_type": "HOLIDAY_USAGE", "merchantName": "스타벅스", "amount": 15000})
assert len(query) > 30, "쿼리가 너무 짧음 — 자연어 변환 실패"
assert "HOLIDAY_USAGE" not in query, "내부 코드가 포함됨"
print("Rerank 쿼리:", query[:100])

# 2. LLM Rerank Fallback 동작 확인 (sentence-transformers 없는 환경)
from services.retrieval_quality import rerank_with_llm_fallback
mock_groups = [
    {"chunk_id": i, "chunk_text": f"제{i}조 테스트 조항", "regulation_article": f"제{i}조"}
    for i in range(5)
]
result = rerank_with_llm_fallback(mock_groups, query="휴일 식대 규정")
assert len(result) == 5, "결과 개수 불일치"
print("LLM Rerank 결과 순서:", [r.get("chunk_id") for r in result])

# 3. 단위 테스트
python -m pytest tests/test_rag_chunking.py -v
```

---

## 📊 고도화 전/후 파이프라인 비교

```
[현행]
.txt → hierarchical_chunk() → embed(search_text) → pgvector
  검색: BM25('simple') + Dense(키워드 concat) → RRF(50:50 고정) → parent 보강 → ko-reranker(키워드 쿼리)

[고도화 후]
.txt → hierarchical_chunk() [절(節) 파싱 추가]
     → expand_tokens() [동의어 확장]
     → embed(search_text)
     → pgvector + search_tokens
  검색:
    BM25(search_tsv + search_tokens) [동의어 매칭]
    + Dense(자연어 쿼리 / HyDE 선택)
    → RRF(케이스별 동적 가중치)
    → semantic_group 필터링
    → parent 보강
    → ko-reranker(자연어 쿼리, 25개 제한)
        └─ fallback: LLM Rerank (sentence-transformers 미설치 시)
```

## 📋 구현 우선순위

| 순위 | 프롬프트 | 예상 정답률 향상 | 난이도 | 비용 |
|------|---------|--------------|-------|------|
| 1 | 프롬프트 2: Dense 쿼리 자연어화 | ★★★★★ | 낮음 | 없음 |
| 2 | 프롬프트 4: 동의어 확장 토큰 | ★★★★☆ | 중간 | 없음 |
| 3 | 프롬프트 5: Rerank 쿼리 + Fallback | ★★★★☆ | 중간 | LLM 소량 |
| 4 | 프롬프트 3: RRF 동적 가중치 | ★★★☆☆ | 낮음 | 없음 |
| 5 | 프롬프트 1: 절(節) 파싱 | ★★★☆☆ | 중간 | 없음 |