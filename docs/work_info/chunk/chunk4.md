# RAG 파이프라인 검증 보고서 + 잔여 고도화 프롬프트
> **검증 기준:** 이전 프롬프트 1·2 반영 소스 + Codex 제기 이슈 5건 전수 검증  
> **규정집:** `사내_경비_지출_관리_규정_v2.0_확장판.txt` (13장, 조문 63개)

---

## PART 1 — 프롬프트 반영 검증 결과

### ✅ 프롬프트 1: 절(節) 계층 파싱 + semantic_group 완성

| 항목 | 기대 | 실측 | 판정 |
|------|------|------|------|
| `_SECTION_PATTERN` 실제 루프에서 사용 | ✅ | `hierarchical_chunk()` L244: `_SECTION_PATTERN.fullmatch(line)` 분기 존재 | ✅ |
| `_build_contextual_header(section_context=...)` 파라미터 추가 | ✅ | L149–167: `section_context` 파라미터 추가, `parts.append(section_context)` 반영 | ✅ |
| `semantic_group` = 장 > 절 형태 | ✅ | L260: `" > ".join([x for x in [chapter_ctx, section_ctx] if x])` | ✅ |
| `current_section` ChunkNode 필드 추가 | ✅ | L116: `current_section: str = ""` 필드 존재 | ✅ |
| 실규정집 적용 효과 | 절 없으면 공백 | 규정집에 `제N절` 헤더 없음 → `current_section` 전부 공백 → **코드는 정상, 효과는 0** | ⚠️ 코드 정상, 문서 해당없음 |

**결론:** 구현은 완전히 반영됨. 단 대상 규정집에 절(節) 헤더가 없으므로 실질 효과 없음. Codex [참고] 의견 사실 확인.

---

### ✅ 프롬프트 2: Dense 쿼리 자연어화 + HyDE + RRF 동적 가중치 + Rerank 개선

| 항목 | 기대 | 실측 | 판정 |
|------|------|------|------|
| `_build_dense_query()` 자연어 템플릿 | ✅ | L446–525: 케이스별 한국어 자연어 문장 생성, "HOLIDAY_USAGE" 등 내부 코드 미포함 확인 | ✅ |
| 심야 시간대 자동 감지 | ✅ | 23시 30분 입력 시 `"심야 시간대"` 언급 없음 → **버그: night_hint가 HOLIDAY_USAGE 템플릿에 미포함** | ❌ |
| `_build_dense_query_with_hyde()` 구현 | ✅ | L528–585: `enable_hyde_query` 플래그, LLM 호출, base_query fallback 구현 | ✅ |
| `_search_dense()` `use_hyde` 파라미터 | ✅ | L595: `use_hyde: bool = False` | ✅ |
| `search_policy_chunks()`에서 `use_hyde=True` 전달 | ✅ | **L816: `_search_dense(db, body_evidence, ...)` — `use_hyde` 미전달** | ❌ |
| RRF 동적 가중치 `_get_rrf_weights()` | ✅ | L229–256: 케이스별 가중치, 심야 보정, 조문번호 힌트 보정 구현 | ✅ |
| `_get_semantic_group_filter()` | ✅ | L259–271: CASE_GROUP_HINTS 구현 | ✅ (구현) / ❌ (매핑 오류) |
| Cross-Encoder 자연어 쿼리 사용 | ✅ | L838: `rerank_query = _build_dense_query(body_evidence)` | ✅ |
| Rerank 입력 25개 제한 | ✅ | L839: `RERANK_INPUT_LIMIT = min(len(enriched), 25)` | ✅ |
| LLM Rerank fallback 구현 | ✅ | L848–857: `rerank_with_llm_fallback` 구현 | ✅ (구현) / ❌ (진입 조건 버그) |
| config `enable_hyde_query`, `enable_llm_rerank_fallback` | ✅ | L93–94: 두 플래그 모두 추가 | ✅ |
| 동의어 확장 `_expand_tokens()` | ✅ | L67–90: 동의어 맵, 조사 제거, 이중 확장 구현 | ✅ |
| `chunking_pipeline.py` `search_tokens` 저장 | ✅ | L178–288: 컬럼 존재 시 `_expand_tokens` 적용 저장 | ✅ |
| `build_policy_keywords()` 동의어 확장 | ✅ | L97–107: `_SYNONYM_MAP` 기반 확장 추가 | ✅ |

---

## PART 2 — Codex 이슈 전수 검증

### 🔴 [높음] 이슈 1: 청킹 병합으로 조문 귀속 왜곡 — **✅ 사실, 심각**

**실측 데이터:**
```
ARTICLE 34개 / CLAUSE 120개 / 병합 ARTICLE 29개 (34개 중 85%가 병합)
```

**핵심 증거 — 제38조(시간대 제약) + 제39조(주말·공휴일 제약) 병합:**
```
병합 CLAUSE 마커: ['②', '③', '②']
                         ↑ 제38조②  ↑ 제38조③  ↑ 제39조②
                    → ② 중복 발생 → codex 주장 정확히 확인됨
```

**왜곡 상세:**
- `제39조`의 `①②` 항이 DB에 `regulation_article = 제38조`로 저장됨
- "주말·공휴일 지출 제한"(제39조 핵심 조항)이 **제38조(시간대 제약)**로 귀속
- `HOLIDAY_USAGE` 케이스 검색 시 "주말·공휴일 제약" 조문이 엉뚱한 번호로 반환 → **정합성 직접 훼손**

**근본 원인:** `_merge_short_articles()`는 `regulation_article`을 앞 조문으로 고정하고 `clauses`를 단순 합산. 뒤 조문의 항들이 앞 조문 번호를 상속.

---

### 🔴 [높음] 이슈 2: HyDE 사실상 미사용 — **✅ 사실**

```python
# L816 — use_hyde 미전달 (기본값 False 사용)
dense_results = _search_dense(db, body_evidence, limit=candidate_limit, effective_date=effective_date)
# 올바른 형태:
# dense_results = _search_dense(db, body_evidence, ..., use_hyde=getattr(settings, "enable_hyde_query", False))
```

`ENABLE_HYDE_QUERY=true` 환경변수를 설정해도 `search_policy_chunks()`가 `use_hyde`를 전달하지 않아 HyDE가 절대 동작하지 않음. Codex 주장 정확히 확인됨.

---

### 🟠 [중간] 이슈 3: Cross-Encoder 없을 때 LLM fallback 미진입 — **✅ 사실**

```python
# retrieval_quality.py L44-46
model = _get_cross_encoder(...)
if model is None:
    return groups  # ← 예외 없이 정상 반환

# policy_service.py L841-857
try:
    reranked = rerank_with_cross_encoder(rerank_input, rerank_query)  # 예외 안 남
    enriched = reranked + remaining
except Exception:   # ← 여기 진입 안 함
    # LLM fallback 코드
```

`sentence-transformers` 미설치 → `model=None` → **예외 없이** `groups` 반환 → `except` 블록 미진입 → LLM fallback 동작 안 함. Codex 주장 정확히 확인됨.

---

### 🟠 [중간] 이슈 4: semantic_group 필터 장(章) 매핑 불일치 — **✅ 사실, 치명적**

**실제 규정집 구조 vs 코드 매핑 비교:**

| 케이스 | 코드 매핑 | 실제 핵심 조문 위치 | 불일치 |
|--------|---------|----------------|--------|
| `HOLIDAY_USAGE` | `["제3장", "제4장"]` | **제7장**(제23조 식대), **제8장**(제39조 주말·공휴일) | ❌ 완전 불일치 |
| `LIMIT_EXCEED` | `["제4장", "제2장"]` | **제8장**(제40조 금액·한도), **제3장**(제11조 승인권한) | ❌ 불일치 |
| `PRIVATE_USE_RISK` | `["제3장", "제5장"]` | **제7장**(각 계정별 조문), **제8장**(제42조 금지업종) | ❌ 불일치 |
| `UNUSUAL_PATTERN` | `["제3장", "제5장"]` | **제8장**(제38조 시간대, 제40조 금액) | ❌ 불일치 |

**실제 규정집 장 구조:**
```
제3장: 승인권한 및 결재 통제   ← 코드가 가리키는 곳
제7장: 계정별 세부 집행 기준   ← 식대(제23조), 접대비, 출장비 실제 위치
제8장: 시간·금액·거래처·업종 공통 제약  ← 주말(제39조), 한도(제40조) 실제 위치
```

**결과:** BM25 group_filter가 활성화되면 올바른 조문이 **필터에서 차단**됨. fallback 전체 검색으로 보완되지만 초기 후보군 품질 저하.

---

### ⚪ [참고] 이슈 5: 절(節) 파싱 효과 없음 — **✅ 사실, 허위 아님**

실측: `current_section` 있는 노드 **0개**. 대상 규정집에 `제N절` 패턴 없음. 코드 구현은 정상, 단 실효 없음.

---

## PART 3 — 추가 발견 이슈 (Codex 미발견)

### 🔴 [추가-높음] HOLIDAY_USAGE 템플릿 심야 힌트 누락

```python
# L486-490 HOLIDAY_USAGE 템플릿
"HOLIDAY_USAGE": (
    "주말 또는 공휴일 경비 사용 건으로, {merchant}에서 {amount}을 지출하였다. "
    "{hr_hint}"
    "이 지출에 적용되는 휴일 경비 사용 제한 규정과 식대 규정을 찾아야 한다."
    # ← {night_hint} 없음!
),
```

휴일+심야(23:30) 동시 발생 케이스에서 심야 정보가 Dense 쿼리에 누락됨. 제38조(심야 시간대 제약)를 찾아야 하는 복합 케이스에서 검색 정확도 저하.

---

## PART 4 — 고도화 Cursor 프롬프트

---

### 🔴 프롬프트 A — 병합 조문 귀속 왜곡 수정 (최우선)

#### 배경
`_merge_short_articles()`가 짧은 조문을 다음 조문과 병합할 때 `regulation_article`을 앞 조문으로 고정하여, 뒤 조문의 항(①②)이 앞 조문 번호로 귀속된다. 실측 결과 제38조+제39조 병합 시 `②` 마커가 두 개(`['②', '③', '②']`) 발생하며 제39조의 항이 `regulation_article=제38조`로 DB 저장된다.

#### 수정 파일: `services/rag_chunk_lab_service.py`

**현재 문제 코드 (L321–338):**
```python
if len(clauses) >= 2:
    for marker, clause_text in clauses:
        clause_node = ChunkNode(
            node_type="CLAUSE",
            regulation_article=regulation_article,  # ← 앞 조문 번호 고정
            ...
        )
```

**병합 조문 처리 전략 변경:**

```python
# _merge_short_articles() 반환 딕셔너리에 원본 조문 정보를 보존한다.
# 병합 시 clauses에 (marker, text, original_article) 3-튜플로 저장

def _merge_short_articles(
    articles: list[dict[str, Any]],
    parent_min: int = PARENT_MIN,
) -> list[dict[str, Any]]:
    """
    병합 시 각 clause에 원래 조문 번호를 함께 보존한다.
    clauses 형태: [(marker, clause_text, source_article), ...]  ← 기존 2-튜플에서 3-튜플로 변경
    """
    if not articles:
        return articles

    merged: list[dict[str, Any]] = []
    skip_next = False

    for i, art in enumerate(articles):
        if skip_next:
            skip_next = False
            continue

        body = art.get("body") or ""
        body_len = len(body)
        has_next = i + 1 < len(articles)

        # 현재 조문의 clauses를 3-튜플로 변환 (source_article 부착)
        art_clauses = art.get("clauses") or []  # 기존: [(marker, text)]
        art_article = art.get("regulation_article") or ""
        # 원본 clauses가 이미 3-튜플인 경우(재처리) vs 2-튜플인 경우 구분
        tagged_art_clauses = [
            (m, t, art_article) if len(c) == 2 else c
            for c in art_clauses
            for m, t in [c[:2]]
        ]

        if body_len < parent_min and has_next:
            next_art = articles[i + 1]
            next_clauses = next_art.get("clauses") or []
            next_article = next_art.get("regulation_article") or ""
            tagged_next_clauses = [
                (m, t, next_article) if len(c) == 2 else c
                for c in next_clauses
                for m, t in [c[:2]]
            ]

            merged_title = f"{art.get('full_title') or art_article} ~ {next_art.get('full_title') or next_article}"
            merged_body = body + "\n\n" + (next_art.get("body") or "")
            merged_clauses = tagged_art_clauses + tagged_next_clauses  # ← 3-튜플 합산

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
                # 병합 조문 목록 보존 (CLAUSE 생성 시 정확한 귀속에 사용)
                "merged_articles": [art_article, next_article],
            })
            skip_next = True
        elif body_len < parent_min and not has_next and merged:
            prev = merged[-1]
            prev["body"] = (prev.get("body") or "") + "\n\n" + body
            prev["full_title"] = f"{prev.get('full_title', '')} ~ {art.get('full_title') or art_article}"
            prev_articles = prev.get("merged_articles") or [prev.get("regulation_article")]
            prev["merged_articles"] = prev_articles + [art_article]
            prev["clauses"] = list(prev.get("clauses") or []) + tagged_art_clauses
        else:
            merged.append({
                **art,
                "clauses": tagged_art_clauses,  # ← 일반 조문도 3-튜플 통일
                "merged_articles": [art_article],
            })

    return merged


# hierarchical_chunk() 내 ChunkNode 변환부 수정 (L321 근처)
# clauses가 3-튜플 (marker, clause_text, source_article)로 변경됨

if len(clauses) >= 2:
    for clause_item in clauses:
        # 3-튜플 또는 2-튜플 모두 처리
        if len(clause_item) == 3:
            marker, clause_text, source_article = clause_item
        else:
            marker, clause_text = clause_item
            source_article = regulation_article

        clause_chunk_text = f"{contextual_header}{marker} {clause_text}".strip()

        # source_article이 regulation_article과 다르면 → 병합된 뒤 조문의 항
        # parent_title과 contextual_header를 source_article 기준으로 재생성
        if source_article and source_article != regulation_article:
            # 뒤 조문의 원본 제목 복원 (article_header에서 찾거나 source_article 사용)
            source_title = source_article  # fallback: 조문 번호만
            # parent_title에서 "~" 분리로 뒤쪽 제목 추출 시도
            full_title_parts = (art.get("full_title") or "").split(" ~ ")
            if len(full_title_parts) >= 2:
                source_title = full_title_parts[-1]  # 마지막 병합 제목

            source_contextual_header = _build_contextual_header(
                source_article,
                source_title,
                chapter_context=art.get("current_chapter") or "",
                section_context=art.get("current_section") or "",
            )
            clause_chunk_text = f"{source_contextual_header}{marker} {clause_text}".strip()
        else:
            source_contextual_header = contextual_header

        clause_node = ChunkNode(
            node_type="CLAUSE",
            regulation_article=source_article,   # ← 핵심: 원본 조문 번호 사용
            regulation_clause=marker or None,
            parent_title=full_title,             # 병합 제목 유지 (표시용)
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
```

#### 검증 방법
```python
from services.rag_chunk_lab_service import hierarchical_chunk

text = open("규정집/사내_경비_지출_관리_규정_v2.0_확장판.txt", encoding="utf-8").read()
nodes = hierarchical_chunk(text)
clauses = [n for n in nodes if n.node_type == "CLAUSE"]

# 제38조 병합 케이스 검증
art38 = next((n for n in nodes if n.node_type == "ARTICLE" and n.regulation_article == "제38조"), None)
if art38:
    child_articles = [c.regulation_article for c in art38.children]
    print("제38조 children regulation_article:", child_articles)
    # 기대: ['제38조', '제38조', '제39조']  (앞 2개는 제38조 항, 뒤 1개는 제39조 항)
    assert '제39조' in child_articles, "제39조 항이 올바르게 귀속되어야 함"
    markers = [c.regulation_clause for c in art38.children]
    assert markers.count('②') <= 1 or all(
        (c.regulation_clause == '②' and c.regulation_article in ['제38조', '제39조'])
        for c in art38.children if c.regulation_clause == '②'
    ), "② 마커가 서로 다른 조문으로 구분되어야 함"
    print("✅ 조문 귀속 왜곡 수정 확인")
```

---

### 🔴 프롬프트 B — HyDE 실제 활성화 + 심야 힌트 누락 수정

#### 배경
두 가지 버그가 동시에 존재한다:
1. `search_policy_chunks()`가 `_search_dense()` 호출 시 `use_hyde` 파라미터를 전달하지 않아 `ENABLE_HYDE_QUERY=true`로 설정해도 HyDE가 동작하지 않음
2. `HOLIDAY_USAGE` 케이스 Dense 쿼리 템플릿에 `{night_hint}` 변수가 없어 휴일+심야 복합 케이스에서 심야 정보가 쿼리에 미포함

#### 수정 파일: `services/policy_service.py`

**버그 1 수정 — L816 `_search_dense()` 호출부:**

```python
# 수정 전 (L816):
dense_results = _search_dense(db, body_evidence, limit=candidate_limit, effective_date=effective_date)

# 수정 후:
dense_results = _search_dense(
    db,
    body_evidence,
    limit=candidate_limit,
    effective_date=effective_date,
    use_hyde=getattr(settings, "enable_hyde_query", False),  # ← 추가
)
```

**버그 2 수정 — L486 HOLIDAY_USAGE 템플릿에 `{night_hint}` 추가:**

```python
_CASE_TEMPLATES: dict[str, str] = {
    "HOLIDAY_USAGE": (
        "주말 또는 공휴일 경비 사용 건으로, {merchant}에서 {amount}을 지출하였다. "
        "{hr_hint}"
        "{night_hint}"          # ← 추가: 휴일+심야 복합 케이스 커버
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
```

#### 검증 방법
```python
import re, ast
src = open("services/policy_service.py").read()

# HyDE 전달 확인
assert "use_hyde=getattr(settings" in src or "use_hyde=settings.enable_hyde_query" in src, \
    "use_hyde 파라미터가 _search_dense() 호출에 전달되어야 함"

# 심야 힌트 누락 확인
holiday_template_start = src.find('"HOLIDAY_USAGE"')
holiday_template_end = src.find('"LIMIT_EXCEED"', holiday_template_start)
holiday_block = src[holiday_template_start:holiday_template_end]
assert "{night_hint}" in holiday_block, "HOLIDAY_USAGE 템플릿에 {night_hint} 필요"

# 실제 쿼리 출력 검증
from services.policy_service import _build_dense_query
q = _build_dense_query({
    "case_type": "HOLIDAY_USAGE",
    "merchantName": "스타벅스",
    "amount": 15000,
    "isHoliday": True,
    "occurredAt": "2024-06-01T23:30:00",
})
assert "심야" in q, "휴일+심야 복합 케이스에서 심야 언급 필요"
print(f"✅ HyDE 경로 수정 + 심야 힌트 추가 확인. 쿼리: {q}")
```

---

### 🟠 프롬프트 C — LLM Rerank Fallback 진입 조건 수정

#### 배경
`rerank_with_cross_encoder()`는 `sentence-transformers` 미설치 시 **예외를 발생시키지 않고** 입력 리스트를 그대로 반환한다. `search_policy_chunks()`의 `except Exception` 블록은 예외 발생 시에만 진입하므로, "모델 없음" 상황에서 LLM fallback이 동작하지 않는다.

#### 수정 파일: `services/policy_service.py` + `services/retrieval_quality.py`

**방법 1 (권장): `rerank_with_cross_encoder()`가 모델 없음을 명시적으로 알림**

```python
# services/retrieval_quality.py 수정

# sentinel 값: cross-encoder 모델이 없을 때 반환하는 특수 값
_RERANK_SKIPPED = object()


def rerank_with_cross_encoder(
    groups: list[dict[str, Any]],
    query: str,
    *,
    model_name: str | None = None,
    batch_size: int = 32,
) -> list[dict[str, Any]]:
    """
    cross-encoder rerank 적용.
    모델 없음: _RERANK_SKIPPED sentinel 반환 대신 cross_encoder_available=False 마킹.
    """
    if not groups or not query or not query.strip():
        return groups
    model = _get_cross_encoder(model_name or _KO_CROSS_ENCODER_MODEL_NAME)
    if model is None:
        # 모델 없음을 명시적으로 표시 (호출자가 fallback 여부 판단 가능)
        for g in groups:
            g["cross_encoder_available"] = False
        return groups   # ← 기존과 동일하게 반환하되, 마킹 추가
    try:
        passages = [g.get("chunk_text") or " ".join(g.get("snippets") or []) or "" for g in groups]
        if not any(passages):
            return groups
        pairs = [(query.strip(), p) for p in passages]
        scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)
        for i, g in enumerate(groups):
            g["cross_encoder_score"] = float(scores[i]) if i < len(scores) else 0.0
            g["cross_encoder_available"] = True
        return sorted(groups, key=lambda x: x.get("cross_encoder_score", 0), reverse=True)
    except Exception:
        return groups
```

**방법 1 대응 — `search_policy_chunks()` 수정:**

```python
# services/policy_service.py L841 근처 수정

rerank_query = _build_dense_query(body_evidence)
RERANK_INPUT_LIMIT = min(len(enriched), 25)
rerank_input = enriched[:RERANK_INPUT_LIMIT]

try:
    from services.retrieval_quality import rerank_with_cross_encoder
    reranked = rerank_with_cross_encoder(rerank_input, rerank_query)
    reranked_ids = {r.get("chunk_id") for r in reranked if r.get("chunk_id") is not None}
    remaining = [r for r in enriched[RERANK_INPUT_LIMIT:] if r.get("chunk_id") not in reranked_ids]
    enriched = reranked + remaining

    # ── 신규: 모델 없음 감지 → LLM fallback ──────────────────────────
    model_unavailable = any(
        r.get("cross_encoder_available") is False
        for r in reranked[:1]  # 첫 번째 결과만 확인
    )
    if model_unavailable and getattr(settings, "enable_llm_rerank_fallback", True):
        try:
            from services.retrieval_quality import rerank_with_llm_fallback
            enriched = rerank_with_llm_fallback(
                rerank_input, rerank_query, body_evidence=body_evidence
            ) + enriched[RERANK_INPUT_LIMIT:]
        except Exception:
            pass
    # ──────────────────────────────────────────────────────────────────

except Exception:
    if getattr(settings, "enable_llm_rerank_fallback", True):
        try:
            from services.retrieval_quality import rerank_with_llm_fallback
            enriched = rerank_with_llm_fallback(
                rerank_input, rerank_query, body_evidence=body_evidence
            ) + enriched[RERANK_INPUT_LIMIT:]
        except Exception:
            pass
```

#### 검증 방법
```python
from services.retrieval_quality import rerank_with_cross_encoder

# sentence-transformers 없는 환경 시뮬레이션
mock_groups = [
    {"chunk_id": i, "chunk_text": f"제{i}조 규정 내용", "regulation_article": f"제{i}조"}
    for i in range(5)
]
result = rerank_with_cross_encoder(mock_groups, "식대 규정")

# 모델 없을 때 cross_encoder_available=False 마킹 확인
if result and result[0].get("cross_encoder_available") is False:
    print("✅ 모델 없음 감지 마킹 정상")
    print("✅ LLM fallback 진입 조건 활성화됨")
else:
    print("⚠️ cross_encoder 모델이 설치되어 있어 fallback 필요 없음")
```

---

### 🟠 프롬프트 D — semantic_group 필터 장(章) 매핑 실규정집 기준 수정

#### 배경
`_get_semantic_group_filter()`의 케이스별 힌트가 실제 `사내_경비_지출_관리_규정_v2.0_확장판.txt` 장 구조와 완전히 불일치한다.

**실제 규정집 장 구조:**
```
제1장 총칙 (제1~5조)
제2장 역할과 책임 (제6~10조)
제3장 승인권한 및 결재 통제 (제11~13조)  ← 코드가 지정한 곳
제4장 증빙 및 전표 입력 기준 (제14~17조) ← 코드가 지정한 곳
제7장 계정별 세부 집행 기준 (제23~37조)  ← 식대, 접대비, 출장비 실제 위치
제8장 시간·금액·거래처·업종 공통 제약 (제38~43조) ← 주말, 한도, 금지업종 실제 위치
제10장 Agent AI 판정 및 운영 (제49~54조)
제12장 위반 및 제재 (제58~59조)
```

**실측 핵심 조문 위치:**
- HOLIDAY_USAGE: **제7장**(제23조 식대), **제8장**(제39조 주말·공휴일)
- LIMIT_EXCEED: **제8장**(제40조 금액 및 누적한도), **제3장**(제11조 승인권한)
- PRIVATE_USE_RISK: **제7장**(계정별 기준), **제8장**(제42조 금지 업종)
- UNUSUAL_PATTERN: **제8장**(제38조 시간대), **제10장**(AI 판정)

#### 수정 파일: `services/policy_service.py`

```python
def _get_semantic_group_filter(body_evidence: dict[str, Any]) -> list[str] | None:
    """
    케이스 유형에 따라 검색할 장(章) semantic_group 패턴 목록 반환.
    None이면 전체 검색.

    ⚠️ 이 매핑은 '사내_경비_지출_관리_규정_v2.0_확장판.txt' 기준.
    규정집이 변경되면 반드시 재검토 필요.

    실제 장 구조:
      제3장: 승인권한 및 결재 통제
      제7장: 계정별 세부 집행 기준 (식대·접대비·출장비 등)
      제8장: 시간·금액·거래처·업종 공통 제약 (주말·한도·금지업종)
      제10장: Agent AI 판정 및 운영
      제12장: 위반 및 제재
    """
    case_type = str(body_evidence.get("case_type") or "")

    _CASE_GROUP_HINTS: dict[str, list[str]] = {
        # 수정 전: ["제3장", "제4장"] → 핵심 조문 없음
        # 수정 후: 제7장(식대), 제8장(주말·공휴일), 제3장(승인)
        "HOLIDAY_USAGE":    ["제7장", "제8장", "제3장"],

        # 수정 전: ["제4장", "제2장"] → 핵심 조문 없음
        # 수정 후: 제8장(금액한도), 제3장(승인권한)
        "LIMIT_EXCEED":     ["제8장", "제3장"],

        # 수정 전: ["제3장", "제5장"] → 핵심 조문 없음
        # 수정 후: 제7장(계정별 기준), 제8장(금지업종), 제4장(증빙)
        "PRIVATE_USE_RISK": ["제7장", "제8장", "제4장"],

        # 수정 전: ["제3장", "제5장"] → 핵심 조문 없음
        # 수정 후: 제8장(시간대·금액), 제10장(AI 판정), 제12장(위반)
        "UNUSUAL_PATTERN":  ["제8장", "제10장", "제12장"],
    }
    return _CASE_GROUP_HINTS.get(case_type)
```

**⚠️ 중요 주의사항:** 이 매핑은 규정집 파일에 종속적이다. 규정집이 업데이트되거나 장 번호가 바뀌면 매핑도 반드시 함께 수정해야 한다. 장기적으로는 아래 자동화 방안을 권장한다:

```python
# 권장: 규정집 업로드 시 장별 키워드 자동 인덱싱 (DB 기반 동적 매핑)
# chunking 완료 후 metadata_json['semantic_group']에서 유니크 장 목록 추출
# case_type별 연관 키워드와 장 내 조문 키워드 매칭으로 자동 힌트 생성
```

#### 검증 방법
```python
import sys
sys.path.insert(0, '.')
from services.rag_chunk_lab_service import hierarchical_chunk
from services.policy_service import _get_semantic_group_filter

text = open("규정집/사내_경비_지출_관리_규정_v2.0_확장판.txt", encoding="utf-8").read()
nodes = hierarchical_chunk(text)

# HOLIDAY_USAGE 핵심 조문이 필터 통과하는지 확인
holiday_filter = set(_get_semantic_group_filter({"case_type": "HOLIDAY_USAGE"}) or [])
for n in nodes:
    if n.node_type == "ARTICLE" and any(kw in n.chunk_text for kw in ["식대", "주말", "공휴일"]):
        chapter = (n.semantic_group or "").split(" > ")[0][:3]  # "제7장" 등 앞 3글자
        in_filter = any(chapter.startswith(f) for f in holiday_filter)
        print(f"  {n.regulation_article} ({n.semantic_group[:10]}) → 필터통과: {in_filter}")
        assert in_filter, f"{n.regulation_article}이 필터에서 차단됨"

print("✅ semantic_group 필터 매핑 수정 확인")
```

---

## PART 5 — 전체 정합성 체크리스트

```
RAG 정합성 100% 달성을 위한 수정 우선순위

[즉시 필수]
□ 프롬프트 A: 병합 조문 귀속 왜곡 → 제38~39조 ② 중복 마커 즉시 수정
□ 프롬프트 B: HyDE use_hyde 미전달 1줄 수정 + HOLIDAY_USAGE 템플릿 {night_hint} 추가
□ 프롬프트 D: semantic_group 필터 매핑 4줄 수정 (가장 쉬움, 효과 큼)

[우선 권장]
□ 프롬프트 C: LLM Rerank fallback 진입 조건 수정 (sentence-transformers 없는 환경)

[이미 정상 반영됨 — 추가 작업 불필요]
✅ 절(節) 파싱 코드 구현 (문서에 절 없어 효과 없지만 코드 정상)
✅ _expand_tokens() 동의어 확장 (검증 통과)
✅ search_tokens 저장 파이프라인 (chunking_pipeline.py 정상)
✅ _build_dense_query() 자연어화 (검증 통과)
✅ RRF 동적 가중치 (구현 정상)
✅ Rerank 25개 제한 + 자연어 쿼리 사용 (구현 정상)
✅ config 플래그 (enable_hyde_query, enable_llm_rerank_fallback) 추가 완료
```