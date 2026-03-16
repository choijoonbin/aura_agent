모든 정보를 취합했습니다. 종합 감사 보고서를 작성합니다.

---

## 감사 보고서: 시연데이터 생성 (Beta) — 비정상 케이스 데이터 완비성 점검

**테스트 실행 결과:** 6 passed / 44 total — 감사 대상 6개 테스트 모두 통과 (단, 아래에서 확인하듯 이는 "충분히 좁은 커버리지"이기 때문임)

---

## A. Findings (심각도 순)

---

### [Severity: Critical] Beta 경로는 FiDocHeader/FiDocItem/AgentCase를 생성하지 않으며 스크리닝 파이프라인에 진입하지 않음

**문제 요약:** `save_custom_demo_case()`는 로컬 JSON 파일만 저장하고, DB 전표 생성·스크리닝 호출이 전혀 없어 Beta로 생성된 비정상 케이스는 에이전트 분석 대상이 될 수 없음

**근거 코드:**
- `services/demo_data_service.py:268–344` — 파일 I/O만 존재, DB 코드 0줄
- `services/case_service.py:108–198` — `run_case_screening()`이 FiDocHeader 조회를 전제 (`select(FiDocHeader)...` line 125)
- `services/demo_data_service.py:541–556` — Legacy `seed_demo_scenarios()`만 `run_case_screening()` 호출

**실제 영향:** Beta로 생성한 HOLIDAY_USAGE 케이스는 AgentCase 테이블에 없으므로 "분석 케이스 목록"에 노출되지 않음. 업로드·보정·저장 후 증빙 파일만 디스크에 존재하고 심사 불가.

**재현 방법:**
```python
# services/demo_data_service.py:268–344 전체를 보면 DB session 파라미터 자체 없음
result = save_custom_demo_case(payload={...}, image_bytes=b"", filename="")
# → data/evidence_uploads/{uuid}/meta.json 만 생성
# → FiDocHeader, FiDocItem, AgentCase 모두 0건
```

**권장 수정:** `save_custom_demo_case()`에 `db: Session` 파라미터 추가 후 FiDocHeader/FiDocItem 생성 + `run_case_screening()` 호출 블록 삽입. 또는 문서(testmultimodal.md)에 "Beta 경로는 의도적으로 DB 비연결"임을 명시하고, 별도 "Beta→분석 진입" 엔드포인트 설계.

---

### [Severity: Critical] 스크리닝 7개 필수 필드 중 4개가 Beta 저장 JSON에 누락

**문제 요약:** `case_service.py`의 `_SCREENING_REQUIRED_FIELDS`(7개)와 Beta `meta.json` 저장 필드를 대조하면 `hrStatus`, `hrStatusRaw`, `budgetExceeded`, `isHoliday`가 항상 누락

**근거 코드:**
```
# services/case_service.py:85–93
_SCREENING_REQUIRED_FIELDS = (
    "occurredAt",      ← Beta: datetime_occurrence로 부분 저장 ⚠
    "isHoliday",       ← Beta: 저장 없음 ✗
    "hrStatus",        ← Beta: 저장 없음 ✗
    "hrStatusRaw",     ← Beta: 저장 없음 ✗
    "mccCode",         ← Beta: 저장됨 ✓
    "budgetExceeded",  ← Beta: 저장 없음 ✗
    "amount",          ← Beta: amount_total로 저장 ✓
)
```

**SCENARIO_PROFILES의 정의된 케이스별 기본값이 Beta에서 완전 무시됨:**

| Case Type | hr_status | budget_flag | mcc_code |
|-----------|-----------|-------------|----------|
| HOLIDAY_USAGE | LEAVE | N | 5813 |
| LIMIT_EXCEED | WORK | **Y** | 5812 |
| PRIVATE_USE_RISK | LEAVE | N | 7992 |
| UNUSUAL_PATTERN | WORK | N | 5813 |

`services/demo_data_service.py:65–143` 에 위 값이 정의되어 있지만 `save_custom_demo_case()`에서 `SCENARIO_PROFILES`를 단 한 번도 참조하지 않음.

**실제 영향:**
- LIMIT_EXCEED 케이스 저장 시 `budgetExceeded` 없음 → 스크리닝 투입 시 "한도 초과" 신호 부재
- HOLIDAY_USAGE 저장 시 `isHoliday=false`(혹은 없음)로 저장 가능 → 정책 위반 감지 불가

**재현 방법:**
```python
result = save_custom_demo_case(
    payload={"case_type": "LIMIT_EXCEED", "amount_total": "500000",
             "date_occurrence": "2026-03-17", "merchant_name": "고액식당", ...},
    image_bytes=b"", filename=""
)
meta = json.loads(Path(result["meta_path"]).read_text())
assert "hrStatus" in meta["edited_entities"]      # ← FAILS
assert "budgetExceeded" in meta["edited_entities"] # ← FAILS
```

**권장 수정:**
```python
# save_custom_demo_case() 내부에서 case_type에 맞는 profile defaults 병합
profile = SCENARIO_PROFILES.get(case_type, {})
"edited_entities": {
    ...existing fields...,
    "hrStatus": payload.get("hr_status") or profile.get("hr_status", "WORK"),
    "budgetExceeded": payload.get("budget_exceeded") or (profile.get("budget_flag","N") == "Y"),
    "isHoliday": _is_weekend_date(date_occurrence) or profile.get("day_mode") == "weekend",
    "blart": profile.get("blart", "SA"),
    "waers": "KRW",
}
```

---

### [Severity: High] 서버단 검증이 전무 — UI 우회 시 필수값 누락 데이터 저장 가능

**문제 요약:** `save_custom_demo_case()`는 인자를 받아 그대로 저장하며 어떠한 비즈니스 룰 검증도 없음. 모든 검증(`validate_demo_required_fields`)은 UI 레이어에서만 수행됨

**근거 코드:**
```python
# services/demo_data_service.py:268 — 함수 시그니처에 validation 없음
def save_custom_demo_case(payload: dict[str, Any], image_bytes: bytes, filename: str):
    # line 268~344: 파일 쓰기만 있고 payload 검증 코드 전혀 없음
    case_type = payload.get("case_type", "UNKNOWN")   # UNKNOWN도 허용
    meta = {...}
    meta_path.write_text(json.dumps(meta, ...))  # 그대로 저장
```

**UI 검증 위치:** `services/demo_data_service.py:211–244` (validate_demo_required_fields), `demo_new.py:294–307` (버튼 disabled 제어) — 모두 UI 호출 전용

**실제 영향:** HTTP API 직접 호출(또는 Python 직접 호출) 시:
```python
# 비정상 케이스 + 빈 amount + 이미지 없어도 저장됨
save_custom_demo_case(
    payload={"case_type": "HOLIDAY_USAGE", "amount_total": "", "date_occurrence": ""},
    image_bytes=b"", filename=""
)  # → 오류 없이 meta.json 생성됨
```

**권장 수정:** `save_custom_demo_case()` 함수 진입 시점에 `validate_demo_required_fields()` 재호출 또는 서버전용 필드검증 로직 삽입. 비정상 케이스이면서 `image_bytes=b""`인 경우 `ValueError` 발생.

---

### [Severity: High] `review_answers`가 항상 단일 원소 리스트 — 질문 수와 구조적 불일치

**문제 요약:** 모든 비정상 케이스의 `review_questions`는 2개인데 `review_answers`는 항상 `[user_reason]` (1개). 저장 후 질문↔답변 인덱스 매핑 불가.

**근거 코드:**
```python
# ui/demo_new.py:378–379
"review_questions": review_questions,        # 2개 질문 (HOLIDAY_USAGE 등)
"review_answers": [user_reason.strip()],     # 항상 1개
```

```python
# services/demo_data_service.py:156–165 (HOLIDAY_USAGE 질문 정의)
"review_questions": [
    "휴일 사용에 대한 사전 승인을 받았습니까?",
    "해당 지출이 업무 목적임을 증명할 수 있습니까?",
]
```

**케이스별 불일치:**

| case_type | questions 수 | answers 수 | 상태 |
|-----------|-------------|-----------|------|
| HOLIDAY_USAGE | 2 | 1 | ❌ |
| LIMIT_EXCEED | 2 | 1 | ❌ |
| PRIVATE_USE_RISK | 2 | 1 | ❌ |
| UNUSUAL_PATTERN | 2 | 1 | ❌ |
| NORMAL_BASELINE | 0 | 1 | ❌ (답이 더 많음) |

**실제 영향:** 저장된 meta.json을 에이전트가 읽을 때 "Q1에 대한 답이 뭔가?" 역참조 불가. HITL 리뷰 노드에서 질문별 충족도 평가 불가.

**권장 수정:** UI에 질문별 개별 text_area 렌더링 또는 단일 텍스트를 N개 질문에 대한 통합 답변임을 명시하는 필드(`answer_type: "combined"`) 추가. 아니면 `review_answers`를 `[user_reason] * len(review_questions)`로 복제하는 임시 처리.

---

### [Severity: Medium] `occurredAt` 필드가 `datetime_occurrence` 키로 저장 — 스크리닝 API 필드명과 불일치

**문제 요약:** `_build_screening_body()`는 `occurredAt`을 키로 사용하는데, Beta meta.json은 `datetime_occurrence`로 저장함. 향후 Beta→스크리닝 연결 시 필드명 매핑 누락으로 묵시적 None 처리

**근거 코드:**
```python
# services/demo_data_service.py:279–283
"edited_entities": {
    "date_occurrence": payload.get("date_occurrence", ""),
    "datetime_occurrence": _combine_date_time(...),  # ← 이 키
    ...
}
# services/case_service.py:73 — 스크리닝 body 키
{"occurredAt": occurred_at, ...}  # ← 다른 키
```

**권장 수정:** meta.json 저장 시 `"occurredAt"` 키를 병행 저장하거나, Beta→스크리닝 변환 레이어에서 명시적 매핑.

---

### [Severity: Medium] `mcc_code` 중복 저장 (top-level + edited_entities 두 곳)

**근거 코드:**
```python
# services/demo_data_service.py:284, 322–323
meta = {
    ...
    "mcc_code": payload.get("mcc_code", ""),      # line 284: top-level
    "edited_entities": {
        "mcc_code": payload.get("mcc_code", ""),  # line 322: 중복
    }
}
```

**실제 영향:** 두 값이 서로 다르게 업데이트될 경우(예: 향후 수정 로직 추가 시) 어느 것이 정본인지 불명확. 현재는 동일 source라 동기화 오류는 없음.

---

## B. Spec Gap (문서 요구사항 vs 실제 구현 비교)

| 항목 | testmultimodal.md 요구 | 실제 구현 | 상태 |
|------|----------------------|---------|------|
| 비정상 케이스 전표 필드 매핑 | `hrStatus`, `mccCode`, `budgetExceeded`, `isHoliday` — "기존 시나리오 기본값 재사용" (line 88) | `SCENARIO_PROFILES` 전혀 참조 안 함 | ❌ 미구현 |
| `occurredAt` 보정 | "시간은 정책 규칙에 맞춰 보정" (line 37) | `_combine_date_time()`으로 단순 합산만, 정책 보정 없음 | ❌ 미구현 |
| `isHoliday` 계산 | 스크리닝 필수 필드 (case_service.py:87) | Beta 경로에서 계산·저장 없음 | ❌ 미구현 |
| `blart`, `waers` | "기존 시나리오 기본값 재사용" (testmultimodal.md:88) | Beta 경로에서 저장 없음 | ❌ 미구현 |
| 서버단 비정상 케이스 필수값 보정 | "서버에서 최종 보정" (testmultimodal.md:89) | `save_custom_demo_case()`에 보정 로직 없음 | ❌ 미구현 |
| `review_questions` UI/파이프라인 일치 | "규정 기반 질문이 UI/검증 파이프라인에서 동일하게 노출" (testmultimodal.md:190) | 질문 정의는 일치, 답변 매핑 불일치 | ⚠ 부분 |
| `sgtxt` 저장 | "적요/비고(`sgtxt`, `bktxt`) 전달 필수" (testmultimodal.md:38) | `memo.sgtxt`로 저장됨, but UI에 sgtxt 입력 필드 없음 | ⚠ 부분 |
| VisualEntity `time_occurrence` | 스펙 외 (추가 구현됨) | 구현됨 | ✅ 추가됨 |
| 저장 JSON 최소 스키마 | 10개 최소 필드 (testmultimodal.md:92–96) | 모두 저장됨 | ✅ 충족 |
| 비정상 케이스+파일 없으면 버튼 disabled | testmultimodal.md:142 | 구현됨 | ✅ 충족 |

---

## C. Test Gap

### 현재 테스트가 보장하는 것
- `save_custom_demo_case()`: UUID 폴더 + 이미지 + meta.json 파일 생성 여부 (file system)
- `validate_demo_required_fields()`: 5개 필드 형식 검증 (금액>0, 날짜 형식, 빈값)
- `is_generate_disabled()`: 비정상케이스+파일없음 → disabled
- `generate_preview_questions()`: 케이스별 질문 목록 반환
- bbox 좌표 유효성(0~1000, ymin≤ymax)
- `analyze_visual_evidence()` fallback (API키 없음, 예외)
- `_combine_date_time()`: 날짜+시간 조합 변환
- `time_occurrence` 엔티티 생성, 3분할 bbox fix

### 보장되지 않는 것 + 추가 테스트 케이스 목록

| 번호 | 테스트 케이스 | 우선순위 |
|------|------------|---------|
| T1 | `save_custom_demo_case(HOLIDAY_USAGE)` → `meta.json`에 `hrStatus="LEAVE"` 존재 확인 | Critical |
| T2 | `save_custom_demo_case(LIMIT_EXCEED)` → `meta.json`에 `budgetExceeded=True` 존재 확인 | Critical |
| T3 | `save_custom_demo_case(HOLIDAY_USAGE, date="2026-03-14")` → `isHoliday=True` 계산·저장 확인 | Critical |
| T4 | `save_custom_demo_case()` 직접 호출 시 amount="" → ValueError 발생 (서버단 검증) | High |
| T5 | `review_answers` 길이 == `review_questions` 길이 또는 단일답변임을 명시하는 구조 확인 | High |
| T6 | `meta.json`의 `occurredAt` or `datetime_occurrence` → `case_service._build_screening_body()` 투입 시 오류 없음 | High |
| T7 | `save_custom_demo_case()` mcc_code 중복값 두 위치가 동일한지 확인 | Medium |
| T8 | Beta 저장 후 `run_case_screening()` 호출 가능 여부 (DB 레코드 없어서 실패해야 하는지 설계 명확화) | High |
| T9 | 비정상 케이스에서 `blart="SA"`, `waers="KRW"` 저장 확인 | Medium |
| T10 | NORMAL_BASELINE: review_questions=[], review_answers 처리 일관성 확인 | Low |

---

## D. Patch Plan (코드 수정 전 제안)

### 변경할 함수 목록

| 함수 | 파일 | 변경 방향 |
|------|------|---------|
| `save_custom_demo_case()` | `services/demo_data_service.py` | ① `SCENARIO_PROFILES` 기본값 병합 ② 서버단 필수값 검증 ③ `isHoliday` 자동 계산 ④ `occurredAt` 키 병행 저장 |
| `validate_demo_required_fields()` | `services/demo_data_service.py` | `save_custom_demo_case()` 내에서 재호출 (서버 게이트 역할) |
| `_handle_generate()` | `ui/demo_new.py` | `review_answers` 매핑 구조 개선 (질문당 1답변 또는 `answer_type` 명시) |

### 데이터 계약 변경안 (meta.json 필수 필드 확장)

```json
{
  "edited_entities": {
    "amount_total": "97042",
    "merchant_name": "가온 식당",
    "date_occurrence": "2026-03-14",
    "time_occurrence": "19:45",
    "datetime_occurrence": "2026-03-14T19:45",
    "occurredAt": "2026-03-14T19:45:00",    ← 신규 (스크리닝 필드명 일치)
    "mcc_code": "5813",
    "hrStatus": "LEAVE",                    ← 신규 (SCENARIO_PROFILES 기본값)
    "hrStatusRaw": "LEAVE",                 ← 신규
    "budgetExceeded": false,                ← 신규
    "isHoliday": true,                      ← 신규 (날짜 기반 자동 계산)
    "blart": "SA",                          ← 신규
    "waers": "KRW"                          ← 신규
  },
  "review_questions": ["질문1", "질문2"],
  "review_answers": ["통합 답변"],          ← answer_type: "combined" 추가 권장
  "answer_type": "combined"                 ← 신규
}
```

### 하위 호환성 영향

- **기존 meta.json 파일**: 신규 필드 없어도 `meta.get("hrStatus")` 패턴은 `None` 반환으로 안전. 읽기 하위 호환 유지.
- **`validate_demo_required_fields()` 재호출**: 기존 UI 경로에서는 이미 통과한 데이터이므로 이중 검증이지만 오류 없음.
- **`review_answers` 구조 변경**: 저장된 기존 케이스 재처리 시 `answer_type` 없으면 `"combined"`로 간주하는 fallback 필요.
- **`case_service.run_case_screening()` 연결**: DB 세션 주입 필요 → `save_custom_demo_case(db: Session, ...)` 시그니처 변경 → 기존 호출부(`ui/demo_new.py`, 테스트) 파라미터 업데이트 필요.




첫째, 이미지 참고하고 아래 수정사항 진행

왜 고쳐지지 않을까요 합계금액 라벨과 동일 라인의 금액을 못 찾는 이유가 궁금합니다.

그리고 결제일시는 여전히 문제입니다. 신중하게 검토해서 진행하세요. 계속 오류가 반복되니 힘드네요

claude 는 멀티 모달 vllm 은 실력이 없는건가요?



둘째, 시연데이터제어(베타) 메뉴는 기존 시연데이터제어(legacy)  로직을 동일하게 가져오데 vllm 으로 읽어들이는 값만 

기존거에서 대체하는것으로 시작했습니다.

그런데 정상비교군 케이스 선택을 해도 로컬 파일시스템에 저장을 하고,  Legacy는 FiDocHeader/FiDocItem에 전표를 넣고 스크리닝까지 수행하는데 그 로직도 다 빠진듯 합니다. 

정상 비교군 외 케이스도 이미지에서 읽어들이는 값들을 사용하는것 외에는 기존 프로세스를 동일하게 수행해야되는데 얼마나 바뀌고 적용이 안된건지 모르겠네요 . 위 내용 확인해서 이해가 안되면 작업 하지 말고 이해되면 진행하세요

---

## 추가 정리: 현재 파일 기준으로 실제 진행해야 할 작업(확정본)

아래 항목이 완료되어야 "Beta는 Legacy와 동일 프로세스 + 이미지 추출값 대체" 요구를 충족한다.

### 1) 저장 경로 정렬 (필수)
- `save_custom_demo_case()`가 파일 저장만 하지 말고 `FiDocHeader/FiDocItem`도 생성해야 함.
- 생성 직후 `run_case_screening(..., strict_required_fields=True)`을 호출해 Legacy와 동일하게 스크리닝까지 완료해야 함.
- 즉, Beta 결과가 분석 목록/케이스 흐름에 즉시 나타나야 함.

### 2) 비정상 케이스 필수 필드 보강 (필수)
- 이미지+수정값 우선 반영: `amount`, `merchantName`, `occurredAt`, `bktxt`, `sgtxt`.
- 시나리오 기본값 병합: `hrStatus`, `hrStatusRaw`, `mccCode`, `budgetExceeded`, `blart`, `waers`.
- 서버 최종 보정: `isHoliday` 및 시간대/예산 신호 정합성 보정.

### 3) 서버단 검증 추가 (필수)
- 현재 UI 검증만으로는 우회 저장 가능하므로, 저장 함수 내부에서 필수값 검증을 다시 수행해야 함.
- 비정상 케이스 + 증빙 없음, 금액/일자/가맹점/적요/사유 누락 시 저장 실패 처리 필요.

### 4) 질문/답변 구조 정합성 수정 (필수)
- `review_questions`(다건)과 `review_answers`(현재 1건) 불일치 해결 필요.
- 해결안:
- 질문별 답변 입력으로 변경하거나,
- 단일 통합답변이면 `answer_type: "combined"`를 명시하고 후속 처리에서 이를 기준으로 해석.

### 5) 정상 비교군 동작 보장 (필수)
- 정상 비교군은 Legacy와 동일 정책으로 DB 전표 생성/스크리닝 완료되어야 함.
- "파일 시스템만 저장"으로 끝나면 요구사항 미충족으로 판단.

### 6) 구현 완료 판정 기준 (체크포인트)
- Beta에서 케이스 생성 후 DB에 `FiDocHeader/FiDocItem/AgentCase`가 실제 생성되는지.
- 비정상 케이스별 `hrStatus/budgetExceeded/isHoliday/mccCode`가 기대값으로 채워지는지.
- 생성 직후 분석 목록에서 케이스 조회 및 후속 분석 실행이 가능한지.
- 관련 회귀 테스트가 추가되어 CI에서 재검증되는지.
