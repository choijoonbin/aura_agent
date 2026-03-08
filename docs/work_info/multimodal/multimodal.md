# 증빙문서 멀티모달 재분석 — 프로세스 및 로드맵 (최종 작업 실행계획서)

#커서작성

---

## 1. 프로세스 확인

### 1.1 현재 분석 종료 상태

- **REVIEW_REQUIRED**: HITL 없이 분석이 끝난 경우. **증빙 부족**으로 전표–증빙 비교가 필요한 케이스에 사용. 이때만 UI에서 "증빙 업로드 후 재분석"을 노출한다.
- **HITL_REQUIRED**: verify 노드에서 `needs_hitl=True`이면 `hitl_pause`로 interrupt → 담당자 응답 후 **같은 run_id로 resume** → `body_evidence["hitlResponse"]` 주입 후 **reporter → finalizer**로 이어서 결과 도출. **규정 위반·심각도 높은 케이스**는 `build_hitl_request`에서 blocking_reasons에 포함되어 HITL로 분류되며, 담당자가 통과/미통과를 판단한다.
- **COMPLETED_AFTER_HITL**: HITL 응답에서 `approved=True`일 때만 "완료" 계열 상태로 갈 수 있음.

**프로세스 구분**: "규정 위반이 심각해 담당자 검토 필요" → **HITL** (증빙 업로드 아님). "증빙이 부족해 전표와 비교할 문서 제출 필요" → **REVIEW_REQUIRED** 후 증빙 업로드.

### 1.2 공통 증빙 의무 (규정 제14조)

규정집에 따르면 **모든 경비 지출은 증빙을 구비하여야 한다**(제14조 공통 증빙 의무). 따라서 에이전트는 **case_type(휴일/한도/업종 등) 위주만 보지 않고**, 매 전표에 대해 다음을 수행한다.

- **document_evidence_probe**: 전표 라인/증거 수집 — 계획에서 case_type 유무와 관계없이 **항상** 포함되며, LLM 플래너가 생략해도 병합 단계에서 다시 넣는다.
- **policy_rulebook_probe**: 규정 조항 조회 — **항상** 포함하며, 키워드/쿼리에 "증빙", "공통", "제14조"를 넣어 공통 증빙 의무 조항이 검색되도록 한다.

이에 따라 case_type 위배만 찾는 것이 아니라, **공통 사항(증빙 구비)** 도 항상 검토 대상에 포함된다.

### 1.3 재분석 요구사항 정리

- **진입 조건**: 케이스 상태가 **REVIEW_REQUIRED**일 때만 재분석 허용.
- **동작**:  
  1. 사용자가 **증빙문서를 업로드**한다.  
  2. **멀티모달 기능**으로 증빙문서에서 **금액·승인일자(전표 발생일자)·업종** 등을 텍스트/구조 데이터로 추출한다.  
  3. **전표 데이터**(`body_evidence`의 amount, occurredAt, mccCode, merchantName 등)와 **비교**한다.  
  4. **증빙이 통과**하면 해당 케이스를 **완료**로 종료한다.
- **재개 방식**: HITL과 동일하게 **same run(thread_id) resume**가 가장 안전하다.  
  - **동일 run_id**로 기존 run 맥락을 이어받아, 증빙 추출·비교 결과만 `body_evidence`에 넣고 **reporter → finalizer**만 수행해 결과 도출.  
  - `intake/planner/execute/critic/verify`를 전부 다시 돌리면 비용/지연이 커지므로 **evidence 검증 전용 분기**를 두는 것이 적합하다.

### 1.4 기존 HITL 재개 흐름(참고)

- `langgraph_agent.py`: `resume_value`가 있으면 `Command(resume=resume_value)`로 그래프 재개.
- 재개 시 `body_evidence`에 `hitlResponse`만 추가하고, 나머지 state(체크포인트)는 그대로 사용 → **reporter_node**에서 `hasHitlResponse`/`hitlApproved`에 따라 verdict를 COMPLETED_AFTER_HITL 등으로 설정.
- 재분석 시 **증빙 통과**를 "HITL 승인과 유사한 입력"으로 취급하여, `body_evidence["evidenceDocumentResult"]` 및 재개 입력 스키마(아래 2.2)를 표준화해 reporter/finalizer에서 **COMPLETED_AFTER_EVIDENCE** 등으로 처리한다.

### 1.5 완료로 가는 프로세스 정리 (세 가지 경로)

설계상 **완료**까지 가는 프로세스는 아래 세 가지가 있다. **처음 분석할 때부터 모든 조건이 만족되면** (경로 1) **완료**까지 가는 프로세스도 포함된다.

| 경로 | 조건 | 최종 상태 | 비고 |
|------|------|-----------|------|
| **1. 처음 분석에서 모든 조건 만족** | 에이전트가 **첫 분석**에서 이미 증거·규정 근거 등이 충분하고, verify 노드가 **추가 검토 불필요**로 판단한 경우. | **COMPLETED**(완료) | 처음부터 검토 없이 완료. 현재 코드는 HITL이 없으면 항상 REVIEW_REQUIRED로 끝나므로, 이 경로를 쓰려면 reporter/finalizer에서 verdict=READY일 때 **COMPLETED**로 두는 정책을 도입해야 함. |
| **2. 증빙 업로드 후 재분석** | 처음 분석에서 증빙 부족 → **REVIEW_REQUIRED** → 사용자가 증빙 업로드 → 재분석에서 전표–증빙 비교 **모든 조건 충족**. | **COMPLETED_AFTER_EVIDENCE**(증빙 검증 완료) | 본 문서의 증빙 재분석 프로세스. |
| **3. HITL 승인** | verify에서 **담당자 검토 필요** → HITL 요청 → 사용자 승인. | **COMPLETED_AFTER_HITL**(검토 후 완료) | 기존 구현됨. |

정리하면, **처음 분석할 때부터 모든 조건이 만족되면** (경로 1) **완료**까지 가는 프로세스도 설계상 있으며, 구현 시에는 "증거 충분·HITL 불필요"인 경우 **REVIEW_REQUIRED가 아닌 COMPLETED**로 종료되도록 reporter/finalizer를 확장하면 된다.

---

## 2. 재분석 프로세스 및 데이터 계약

### 2.1 재분석 프로세스(안)

| 단계 | 내용 |
|------|------|
| 1 | REVIEW_REQUIRED 케이스에 대해 "증빙 제출 / 재분석" 액션 노출 |
| 2 | 증빙문서 업로드(파일) → 서버 저장·해시(SHA-256)·run/case 연결 |
| 3 | **멀티모달 추출**: 업로드된 문서에서 **금액, 승인일자(발생일자), 업종(MCC/업종명)** 등 구조화 필드 추출 |
| 4 | **전표–증빙 비교**: 정량 규칙(2.3)으로 통과 여부 판정, `confidence` 및 `reasons[]` 저장 |
| 5 | **통과 시**: 기존 run state를 이어서 **evidenceDocumentResult** 주입 후 reporter/finalizer에서 **완료** 처리 |
| 6 | **불통과 시**: 사유(금액/날짜/업종)와 함께 **EVIDENCE_REJECTED** 등 보류 상태로 반환 |

**기존 분석 결과를 이어서** 사용하므로, intake → planner → execute → verify는 재실행하지 않고 **증빙 비교 결과만 반영한 뒤 reporter → finalizer**만 수행한다(HITL과 동일한 resume 스타일).

### 2.2 상태 모델 정리 (필수)

- 신규 상태 코드 추가 및 UI/집계 매핑 갱신:
  - **EVIDENCE_PENDING**: 증빙 제출 대기
  - **EVIDENCE_REJECTED**: 증빙 불일치(필드별 사유 반환)
  - **COMPLETED_AFTER_EVIDENCE**: 증빙 통과 완료
- UI 표시명("완료", "검토 필요", "보류") 및 KPI 집계에 위 상태를 반영한다.

### 2.3 재개 입력 스키마 (필수)

- 현재 `resume_value`는 HITL 전용이므로, evidence 재분석용으로 확장:
  - **resume_type**: `"hitl" | "evidence"`
  - **evidence_result**: `{ passed, score, fields, mismatches, extractor_meta }`
- 그래프 내부 `body_evidence` 주입 키 표준화:
  - **body_evidence["evidenceDocumentResult"]**: `{ passed, extracted_fields, comparison_detail, confidence, reasons }`
  - **body_evidence["evidenceDocuments"]**: 원본 파일 메타(해시, 페이지, 저장 경로 등)

### 2.4 비교 규칙의 정량화 (필수)

- **금액**: 절대 오차(예: ±100원) + 상대 오차(예: ±0.5%) 둘 다 적용. 둘 중 하나라도 초과 시 불일치.
- **날짜**: `occurredAt`(전표 발생일자) 기준 ±N일 허용(정책값으로 설정).
- **업종**: `mccCode` 직접 일치 우선; 없으면 업종명 정규화 사전으로 매칭.
- **판정 산출**: `pass/fail`만 두지 말고 **confidence**와 **reasons[]**를 함께 저장해 UI/감사에 활용.

### 2.5 감사/재현성 (필수)

- 추출 **원문 스니펫**, bbox, 페이지 번호, **모델 버전**, 처리시간을 `metadata_json`(또는 run aux)에 저장.
- 분쟁 대응용으로 **원본 파일 해시(SHA-256)** 저장.

---

## 3. 멀티모달 정확도 — 라이브러리/서비스 선정

증빙문서에서 **금액·날짜·업종** 등을 안정적으로 추출하려면, 문서 종류(PDF/이미지, 네이티브 vs 스캔)와 운영 환경(온프레미스 vs 클라우드)을 고려한 선택이 필요하다.

### 3.1 로컬/오픈소스

| 라이브러리 | 용도 | 장점 | 비고 |
|------------|------|------|------|
| **Camelot** | PDF 테이블 추출 | 텍스트 PDF에서 테이블 정확도 높음, pandas 연동 | 스캔/이미지 PDF 비지원 |
| **pdf2table** | PDF/이미지 테이블 | PDF·이미지 모두 지원, 테이블 위주 | 한글/레이아웃에 따라 튜닝 필요 |
| **unifex** | 통합 추출(OCR+테이블+LLM) | EasyOCR/Tesseract/PaddleOCR 및 Azure/Google Document AI 백엔드, LLM 기반 구조화 추출 | 멀티백엔드로 정확도·비용 조합 가능 |
| **Tesseract + layoutparser / EasyOCR** | OCR 기반 텍스트 | 무료, 로컬 실행 | 표/복잡 레이아웃은 후처리 필요 |
| **PaddleOCR** | OCR | 한글 인식 좋음, 테이블 인식 모델 별도 | 도커/환경 의존성 있음 |

- **정확도 우선**이면: **텍스트 PDF**는 Camelot, **스캔/이미지**는 PaddleOCR 또는 **unifex + 클라우드 백엔드** 조합을 권장.

### 3.2 클라우드 Document Understanding API

| 서비스 | 특징 | 정확도·일관성 |
|--------|------|----------------|
| **Azure AI Document Intelligence** | 레이아웃·키-값·테이블, 영수증/인보이스 등 prebuilt, 커스텀 모델 | 높음, 비용 발생 |
| **Google Document AI** | 다양한 프로세서 타입, 다국어 | 높음, 비용 발생 |

- **증빙문서 품질이 제각각**이거나 **폼/인보이스 형태**가 정해져 있으면 Azure/Google prebuilt 모델이 정확도와 유지보수 측에서 유리하다.
- PoC 단계에서는 **한 가지 백엔드**(예: Azure Document Intelligence 또는 unifex 내 Azure 백엔드)로 통일해, 입력 포맷(이미지/PDF)과 출력 스키마(금액, 날짜, 업종)를 고정한 뒤 전표 필드와의 매핑 규칙을 만드는 것이 좋다.

### 3.3 멀티모달 LLM(Vision)

- **GPT-4V / Claude Vision** 등: 이미지/PDF 페이지를 넣고 "금액, 승인일자, 업종을 JSON으로 추출해라"처럼 프롬프트로 구조화 가능.
- **장점**: 레이아웃이 복잡해도 유연하게 대응 가능.  
- **단점**: 비용·지연, 숫자/날짜 오타 가능성.  
- **역할**: 1차 추출 후 **검증/보정** 또는 **로컬 OCR 결과와 교차 검증**용으로 두는 구성을 권장.

### 3.4 선정 요약 및 운영 전략

- **1차 권장(정확도 우선)**: **Azure AI Document Intelligence**  
  - 사유: 영수증/인보이스 구조화 안정성, 운영 유지보수 용이. 증빙문서용 prebuilt 또는 layout 프로세서로 금액·날짜·항목명 추출.
- **2차 백업(벤더 종속 완화)**: 로컬 OCR 파이프라인(**PaddleOCR**) + LLM 구조화, 또는 **unifex** + PaddleOCR/EasyOCR 조합.
- **3차(한글/테이블)**: 한글 비중이 크면 **PaddleOCR** 또는 **pdf2table**을 파이프라인에 포함.
- **운영 전략**:
  - 1차 추출: Document Intelligence(또는 선정한 클라우드 1종).
  - 2차 교차검증: Vision LLM(필요 시) 또는 규칙 엔진.
  - 불일치 시 **EVIDENCE_REJECTED** + 원인코드 반환.

---

## 4. 로드맵 (상세 작업 실행계획)

### Phase 0 — 데이터 계약/상태 계약

- **목표**: API·DB·이벤트 스키마에 evidence 재분석 계약을 먼저 정의하고, 상태 코드와 UI 매핑을 확정한다.
- **작업**:
  - 상태 코드 확정: EVIDENCE_PENDING, EVIDENCE_REJECTED, COMPLETED_AFTER_EVIDENCE 및 기존 완료/검토필요/보류와의 관계.
  - 재개 입력 스키마(2.2, 2.3) 및 `body_evidence` 주입 키를 API/이벤트 스펙에 반영.
  - `update_agent_case_status_from_run` 및 UI 집계 매핑 테이블 갱신 설계.

### Phase 1 — 업로드/추출 서비스

- **목표**: 증빙문서 업로드와 멀티모달 추출을 하나의 서비스 흐름으로 제공.
- **작업**:
  - **POST /api/v1/analysis-runs/{run_id}/evidence-upload** (또는 cases/{voucher_key}/evidence-upload 후 run_id 연결):  
    multipart 파일 업로드 → 서버 저장 → **원본 파일 SHA-256 해시 저장** → extractor 호출 → 구조화 결과 저장.
  - 케이스 상태가 **REVIEW_REQUIRED**일 때만 호출 허용(그 외 4xx 또는 안내 메시지).
  - 추출 스키마: `{ amount, approval_date_or_occurred_at, industry_or_mcc, merchant_name? }` + 신뢰도/원문 스니펫.
  - 서비스 레이어: `services/evidence_extraction.py` (입력=파일/bytes, 출력=구조화 dict + 메타). 필요 시 **EvidenceExtractor** 인터페이스로 백엔드 교체 가능하게 추상화.
  - 단위 테스트: 샘플 PDF/이미지로 추출 결과 및 정확도 확인.

### Phase 2 — 비교/판정 엔진

- **목표**: 추출된 증빙 필드와 전표(`body_evidence`)를 정량 규칙으로 비교하고, 통과 여부·confidence·reasons를 산출.
- **작업**:
  - **services/evidence_compare_service.py** 신설: 금액(절대/상대 오차), 날짜(±N일), 업종(mccCode/업종명 매핑) 규칙 기반 비교 + **confidence** 및 **reasons[]** 계산.
  - 비교 결과를 `evidenceDocumentResult` 형태로 정리해 Phase 3 재개 입력에 사용.
  - 추출 메타(원문 스니펫, bbox, 페이지, 모델 버전, 처리시간)를 run metadata에 저장(감사/재현성).

### Phase 3 — same-run 재개 (reporter/finalizer)

- **목표**: 기존 run을 이어받아 evidence 결과만 반영한 뒤 reporter → finalizer로 최종 상태 확정.
- **작업**:
  - **POST /api/v1/analysis-runs/{run_id}/evidence-resume**:  
    `Command(resume=...)`에 **resume_type="evidence"**, **evidence_result: { passed, score, fields, mismatches, extractor_meta }** 전달.
  - 그래프 진입 시 `body_evidence["evidenceDocumentResult"]`, `body_evidence["evidenceDocuments"]` 주입.
  - **reporter/finalizer**에서 evidence 결과를 읽어 verdict를 **COMPLETED_AFTER_EVIDENCE** 또는 **EVIDENCE_REJECTED**로 설정.
  - `update_agent_case_status_from_run`에 COMPLETED_AFTER_EVIDENCE → 완료 계열, EVIDENCE_REJECTED → 보류 계열 매핑 추가.
  - UI 집계: "완료" KPI에 COMPLETED_AFTER_EVIDENCE 포함.

### Phase 4 — UI/운영

- **목표**: 워크스페이스에서 REVIEW_REQUIRED 케이스에 대해 증빙 업로드와 재분석 결과를 사용할 수 있게 함.
- **증빙 업로드 UI 배치 위치**:
  - **페이지**: **워크스페이스** (Streamlit `ui/workspace.py`, 워크스페이스 탭).
  - **영역**: 케이스를 선택했을 때 **우측 패널(에이전트 대화/판단 요약 패널)**. 즉, 좌측 케이스 목록에서 전표를 선택한 뒤 보이는 **오른쪽 영역**.
  - **구체적 위치**: 현재 "strip(스크리닝·상태 요약)" + **"HITL 확인" 체크박스** + **"분석 시작" 버튼**이 있는 **같은 행(columns)** 근처, 또는 그 바로 아래. HITL 대기 시 "이 분석은 담당자 검토가 필요합니다" 배너와 "HITL 검토 입력 열기" 버튼이 나오는 것과 동일한 패널 안.
  - **노출 조건**: 선택한 케이스의 **상태가 "검토 필요"(REVIEW_REQUIRED)**일 때만 **"증빙 업로드 후 재분석"** 버튼(및 파일 업로드) 노출. HITL_REQUIRED일 때는 기존처럼 HITL 검토 입력만 노출.
  - **참고**: `render_workspace_chat_panel()` 내부, strip/cta_hitl_col/cta_btn_col 다음 블록 또는 `_has_pending_hitl()` 블록과 나란히 **REVIEW_REQUIRED 전용 블록**을 추가하는 형태로 구현 예정.
- **작업**:
  - 위 위치에 **"증빙 업로드 후 재분석"** 버튼 및 파일 업로드(st.file_uploader 또는 유사) 노출.
  - 파일 업로드 → evidence-upload API 호출 → (선택) evidence-resume 호출 또는 자동 연동 → 스트림/폴링으로 진행 상태 표시.
  - 재분석 완료 시 "증빙 검증 완료" 메시지 및 케이스 상태 "완료" 갱신.
  - **증빙 불일치 시**: 사유(금액/날짜/업종) 및 필드별 비교표 표시.
  - (권장) 대시보드에 **evidence_resume_success_rate** 등 지표 추가.

### 이번 범위에서 제외(불필요 작업)

- 첫 릴리즈에서 **완전한 문서 분류기**(영수증/계약서/품의서 자동 분류) 구축.
- **다중 OCR 엔진 동시 운영(A/B)** 즉시 도입.
- **end-to-end 자율 재계획(planner 재진입)**까지 한 번에 구현.

> **권장**: 먼저 **REVIEW_REQUIRED → 증빙 통과 시 완료**의 단일 경로를 안정화한 뒤, 문서 분류·멀티엔진·자동 재계획은 단계적으로 확장.

---

## 5. 체크리스트(구현 시 참고)

- [ ] REVIEW_REQUIRED만 재분석/evidence-upload API 허용(다른 상태는 4xx 또는 안내 메시지).
- [ ] 증빙 추출 스키마와 전표 필드 매핑을 한 곳(설정 또는 상수)에서 관리.
- [ ] 재분석 시 **기존 run state 이어받기**(resume_type=evidence, reporter-only 경로)로 HITL과 동일한 패턴 유지.
- [ ] 멀티모달 라이브러리/API 선택 시 **한글·숫자·날짜** 정확도와 비용을 문서화하고, **EvidenceExtractor** 등으로 백엔드 교체 가능하게 추상화.
- [ ] 증빙 불일치 시 **사유(금액/날짜/업종)** 및 **reasons[]**를 사용자에게 노출해 재제출 시 참고 가능하게 함.
- [ ] 원본 파일 **SHA-256 해시** 및 추출 메타(원문 스니펫, 페이지, 모델 버전, 처리시간) 저장으로 감사/재현성 확보.

---

## 6. 검토 의견 반영 — 명확한 정의

아래는 merge 및 검토 과정에서 제기된 이슈에 대한 **명확한 정의**와 **구현 전 선결 조치**이다.

### 6.1 Run 종료 정책과 evidence 재개 방식 (검토 의견: 높음)

- **현행 동작**: `main.py`에서 run 종료 시 **HITL_REQUIRED**가 아니면 `runtime.close(run_id)`를 호출한다. 따라서 **REVIEW_REQUIRED**로 끝난 run은 이미 close되어, 같은 run에 대한 `Command(resume=...)` 재개가 불가능하다.
- **명확한 정의**:
  - **선택지 A (same-run 재개)**  
    evidence 재분석도 same run으로 재개하려면, **REVIEW_REQUIRED**인 run은 close 대상에서 제외해야 한다.  
    즉, `last_payload.get("result", {}).get("status") in ("HITL_REQUIRED", "REVIEW_REQUIRED")`일 때는 `runtime.close(run_id)`를 호출하지 않도록 정책을 확장한다.
  - **선택지 B (새 run + lineage)**  
    REVIEW_REQUIRED run은 기존대로 close하고, evidence 재분석은 **새 run**을 생성하되 **parent_run_id** 등 lineage로 기존 run과 연결한다. 새 run 입력에 기존 run의 state(체크포인트)를 복원한 뒤 reporter/finalizer만 실행하는 방식.
- **조치**: 구현 Phase 0에서 **A 또는 B 중 하나를 확정**하고, main.py의 close 조건 및(또는) evidence-resume API의 run 생성/복원 로직에 반영한다.

### 6.2 신규 상태 코드와 DB/매핑 정책 (검토 의견: 중간)

- **현행 동작**: `update_agent_case_status_from_run`은 일부 상태만 `status_map`으로 변환하고(HITL_REQUIRED 등 → IN_REVIEW), 나머지는 **그대로** DB에 쓴다. `dwp_aura.agent_case.status` 컬럼이 **DB enum**이면, 문서에 정의한 EVIDENCE_PENDING, EVIDENCE_REJECTED, COMPLETED_AFTER_EVIDENCE 등 신규 값 저장 시 제약/실패 가능성이 있다.
- **명확한 정의**:
  - **구현 전 필수**: 상태 코드 추가 전에 **DB 스키마 확인** — `status` 컬럼이 enum이면 허용 값 목록에 신규 상태를 추가하거나, varchar 등으로 변경해야 한다.
  - **매핑 정책**:  
    - UI/집계용 “완료”에 포함할 상태: `COMPLETED`, `COMPLETED_AFTER_HITL`, **COMPLETED_AFTER_EVIDENCE**, RESOLVED, OK.  
    - “보류/검토”에 포함할 상태: **EVIDENCE_REJECTED**, EVIDENCE_PENDING(필요 시), HOLD_AFTER_HITL 등.  
    - `update_agent_case_status_from_run`의 `status_map`에 COMPLETED_AFTER_EVIDENCE는 **그대로 저장**(완료 계열), EVIDENCE_REJECTED는 **IN_REVIEW 또는 별도 보류 코드** 중 정책에 맞게 매핑 확정.
- **조치**: Phase 0에서 DB enum 여부 및 허용 값 목록을 확인하고, 위 매핑 규칙을 코드와 문서에 동일하게 반영한다.

### 6.3 문서 구조 (검토 의견: 중간)

- **명확한 정의**:  
  - **본문(섹션 1~6)** = **최종 작업 실행계획서(단일 최종본)**. 구현·유지보수 시 이 본문만 따르면 된다.  
  - **#codex 작성 이하** = 참고용 원문 및 검토 의견. 역할 분리 후 **삭제 예정**으로 두고, 필요 시 과거 검토 이력만 참고용으로 보관한다.
- **조치**: 본문과 중복된 계획 블록은 본문 기준으로 통일하고, #codex 블록은 검토 완료 후 제거한다.

### 6.4 증빙 일자 필드 용어 (검토 의견: 낮음)

- **명확한 정의**:
  - **occurred_at**: **전표 발생일자**(전표 데이터의 거래 발생 시점). `body_evidence.occurredAt`에 해당하며, 비교 규칙의 **기준일**로 사용한다.
  - **approval_date**: **증빙문서 상 승인일자**(증빙문서에서 멀티모달로 추출한 “승인일” 또는 “발생일” 필드). 전표와 비교할 **대상일**이다.
- **비교 규칙**: 증빙의 **approval_date**와 전표의 **occurred_at**을 비교하여, occurred_at 기준 **±N일** 이내면 일치로 판정한다. 문서·코드에서 “승인일자(전표 발생일자)”처럼 한꺼번에 쓰지 말고, **approval_date** vs **occurred_at**로 구분해 기재한다.
- **조치**: 추출 스키마(Phase 1·2)와 비교 서비스(Phase 2)에서 필드명을 `approval_date`(증빙), `occurred_at`(전표)로 통일한다.

### 6.5 코드·문서 대조 결과 (현행 상태)

구현 착수 전 아래 현행 상태를 확인했다. 문서상 계획은 정리되었으나 **구현은 미착수**이며, **A/B 중 하나 확정** 후 진행하면 된다.

| 항목 | 재확인 결과 | 근거 |
|------|-------------|------|
| **1) A/B 의사결정 미확정** | 유효. 본문 6.1에 선택지 A(same-run 재개)와 B(새 run + lineage)만 나열되어 있고 "Phase 0에서 A 또는 B 중 하나를 확정"으로만 되어 있음. 단일안 확정 문구 없음. | 구현 전 **A/B 중 하나 확정** 필요. |
| **2) 문서 구조** | 본문(섹션 1~6)이 **단일 최종본**이다. 재확인·보강 내용은 6.5·6.6에 통합되었고, 중복 블록은 제거하여 본 문서만 유지한다. | 6.3 정리. |
| **3) 코드 반영 미적용** | 유효. evidence-upload/evidence-resume 엔드포인트 없음. EVIDENCE_* 상태 매핑 없음. | main.py: 420라인 부근 submit_hitl만 존재. case_service.py: status_map에 HITL_REQUIRED 등 → IN_REVIEW만 있고 EVIDENCE_PENDING / EVIDENCE_REJECTED / COMPLETED_AFTER_EVIDENCE 반영 없음. 프로젝트 전체에 증빙 재분석용 라우트·상태 매핑 없음. **문서상 계획만 있고 구현 미착수**로 확인됨. |

**결론**: 문서는 진행 가능한 수준으로 정리되어 있으며, **A/B 확정**만 하면 바로 구현 단계로 넘어가도 된다.

### 6.6 보강 사항 (구현 시 반영)

아래는 구현·QA 시 반영할 보강 사항이다.

- **A/B 선택 기준을 수치·조건으로 명시**
  - **A(same-run 재개) 선택 조건**: 같은 run 재개 대기 지연 < X초(정책값), run state 메모리 유지 가능한 런타임.
  - **B(새 run + lineage) 선택 조건**: 서버 재시작/스케일아웃 환경이 필수이거나, lineage 추적·감사가 우선인 경우.
  - Phase 0에서 A 또는 B 확정 시 **선택 사유(의사결정 로그)** 를 문서 또는 코드 주석에 남긴다.

- **완료 판정 기준(acceptance criteria) 추가**
  - **COMPLETED_AFTER_EVIDENCE** 부여 조건(예): amount 일치(정량 규칙 2.4) **AND** approval_date와 occurred_at ±N일 **AND** mcc 또는 업종명 매칭 → 전부 충족 시 통과.
  - **EVIDENCE_REJECTED** 부여 조건(예): 위 항목 중 불일치 N개 이상(또는 1개라도 불일치 시 보류 정책에 따라)이면 EVIDENCE_REJECTED.
  - 위 기준을 2.4 비교 규칙 또는 evidence_compare_service 스펙에 명시하여 QA/테스트 기준으로 사용한다.

- **문서 정리 타이밍**
  - 본 문서가 **단일 최종본**이므로, 과거 참고 블록(#codex 작성 등)은 **Phase 0 승인 또는 최종 확정 시점에 제거**하여 중복·혼선을 막는다.

---

## 7. 작업 착수 준비 체크

문서 기준으로 **작업 시작 준비는 되어 있다**. 아래만 확정하면 Phase 0부터 구현을 진행할 수 있다.

| 구분 | 상태 | 비고 |
|------|------|------|
| **프로세스·로드맵** | ✅ 정리됨 | 1~4절: 완료 경로 3가지, 재분석 단계, 상태·스키마·비교 규칙, Phase 0~4 상세 작업 |
| **UI 배치** | ✅ 정리됨 | Phase 4: 워크스페이스 우측 패널, strip/HITL/분석 시작 행 근처, REVIEW_REQUIRED 시만 노출 |
| **착수 전 확정 1** | ⬜ **A/B 의사결정** | **same-run 재개(A)** vs **새 run + lineage(B)** 중 하나 확정. 확정 후 main.py close 조건 또는 evidence-resume 설계 반영. |
| **착수 전 확인 2** | ⬜ **DB 스키마** | Phase 0에서 `dwp_aura.agent_case.status` 컬럼이 enum인지 확인. enum이면 신규 상태 추가 또는 varchar 등으로 변경. |
| **Phase 1에서 결정** | ⬜ **멀티모달 백엔드** | 1차 권장: Azure Document Intelligence. PoC에서는 한 가지 백엔드로 통일 후 추출 스키마 고정. |

**결론**: **A/B 중 하나만 확정**하면 문서 기준으로 **Phase 0 → 1 → 2 → 3 → 4** 순서로 작업을 시작할 수 있다. DB 확인은 Phase 0과 동시에 진행하면 된다.

---
