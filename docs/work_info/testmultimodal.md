Cursor에게 전달할 [멀티모달 & 시연데이터 고도화] 최종 작업 지시서 (보완본)
제목: [엔드투엔드 구현] 멀티모달 시각 분석 도입 및 지능형 시연 데이터 생성 메뉴 개편

목표
- AuraAgent의 증빙 검증 신뢰도를 높이기 위해 멀티모달(이미지+텍스트) 분석을 도입한다.
- 시연 데이터 생성 UI를 신설해, 업로드 즉시 핵심 엔티티(금액/일자/가맹점)와 좌표를 추출하고 사람이 보정 후 저장할 수 있게 한다.
- 기존 검증 파이프라인(HITL/리뷰 로직)과 질문 체계를 일치시켜 UI/백엔드 간 불일치를 제거한다.

핵심 결정사항(중요)
1) 모델 전략
- 1차 권장: OCR/Layout + LLM 하이브리드
  - OCR 엔진(예: PaddleOCR/docTR)에서 텍스트+bbox를 우선 추출
  - LLM은 정규화(YYYY-MM-DD/숫자 금액), 라벨 매핑(amount/date/merchant), 감사 코멘트 생성 담당
- 2차 대안(OpenAI-only): Vision LLM 단독 추출
  - 우선 모델: gpt-5
  - 비용 최적화 대체: gpt-4.1-mini
  - gpt-4o는 호환용으로만 유지(기본값 아님)

2) 좌표 표준
- 외부 인터페이스(UI/저장/테스트): [ymin, xmin, ymax, xmax], 정수, 0~1000 정규화
- 내부 처리에서는 원본 픽셀 좌표(x0,y0,x1,y1)도 함께 보관해 재계산 가능하게 한다.

3) 질문 생성 로직 재사용
- `agent/hitl.py`에 `generate_hitl_questions` 함수는 현재 없음.
- 질문 재사용은 `agent/langgraph_verification_logic.py`의 규정 기반 결과(`required_inputs`, `review_questions`)를 서비스 레이어에서 호출/재사용하도록 설계한다.

4) 저장 경로 정책
- 기본 영속 경로: `data/evidence_uploads/{case_uuid}/...`
- 필요 시 `규정집/priceimg`는 호환 복사 경로로만 사용(단일 소스는 `data/evidence_uploads` 유지)

5) 시연데이터 생성 정책(신규/중요)
- 기존 `시연데이터 제어`의 seed 로직은 직접 재사용하지 말고, 필요 시 복제 후 `Beta 전용 생성 경로`로 분리한다.
- `NORMAL_BASELINE`만 기존 생성 규칙을 그대로 사용 가능하다.
- 그 외 비정상 케이스(`HOLIDAY_USAGE`, `LIMIT_EXCEED`, `PRIVATE_USE_RISK`, `UNUSUAL_PATTERN`)는 업로드 증빙에서 추출/수정된 아래 3개 필드를 우선 반영해 전표를 생성한다.
  - `amount` <= `amount_total`
  - `merchantName` <= `merchant_name`
  - `occurredAt` <= `date_occurrence`(+ 시간은 정책 규칙에 맞춰 보정)
- 적요/비고(`sgtxt`, `bktxt`)는 분석 단계에서 반드시 LLM 컨텍스트로 함께 전달한다.

----------------------------------------------------------------
1단계: 데이터 모델 및 멀티모달 유틸리티 구축
대상 파일: `agent/output_models.py`, `utils/llm_azure.py`

작업 내용
A. `agent/output_models.py`
- `VisualBox` 모델 추가
  - 필드: `ymin`, `xmin`, `ymax`, `xmax` (int)
  - 제약: 0 <= 값 <= 1000, `ymin <= ymax`, `xmin <= xmax`
- `VisualEntity` 모델 추가
  - 필드: `id`, `label(amount_total|date_occurrence|merchant_name)`, `text`, `bbox: VisualBox`, `confidence(0~1)`
- `MultimodalAuditResult` 모델 추가
  - `image_analysis`: `{condition, has_stamp}`
  - `entities: list[VisualEntity]`
  - `suggested_summary`, `audit_comment`
  - `source`: `"vision_llm" | "ocr_llm"`
  - `fallback_used: bool`

B. `utils/llm_azure.py`
- 함수 추가: `analyze_visual_evidence(image_base64: str, *, model: str | None = None) -> MultimodalAuditResult`
- 동작
  - 1순위(설정 시): OCR 결과를 받아 정규화 + 라벨링
  - 2순위: Vision LLM 호출(기본 `gpt-5`, 환경변수로 오버라이드)
  - 실패 시 OCR 텍스트 기반 fallback 반환 + 경고 로그
- 필수 사항
  - Structured JSON 파싱(스키마 강제)
  - bbox out-of-range 시 clamp + 경고 로그
  - 날짜/금액 정규화 실패 시 `confidence` 하향

환경변수 제안
- `MULTIMODAL_PROVIDER=vision_llm|ocr_llm`
- `MULTIMODAL_MODEL=gpt-5`
- `MULTIMODAL_TIMEOUT_MS=15000`

----------------------------------------------------------------
2단계: 시연 데이터 서비스 레이어 확장
대상 파일: `services/demo_data_service.py`

작업 내용
- `generate_preview_questions(case_type: str, case_data: dict) -> dict`
  - 반환: `{required_inputs: [...], review_questions: [...]}`
  - 구현: `agent/langgraph_verification_logic.py`의 규정 기반 질문 생성 흐름을 재사용
- `save_custom_demo_case(payload: dict, image_bytes: bytes, filename: str) -> dict`
  - `case_uuid` 생성
  - `data/evidence_uploads/{case_uuid}/`에 이미지/메타데이터 저장
  - 메타데이터(json)에 추출 엔티티, 사용자 수정값, 질문/답변, 모델 정보 기록
  - 비정상 케이스 생성 시 전표 필드 매핑 규칙 적용:
    - 업로드/사용자입력 우선: `amount`, `merchantName`, `occurredAt`, `bktxt`, `sgtxt`
    - 기존 시나리오 기본값 재사용: `hrStatus`, `mccCode`, `budgetExceeded`, `expenseType(blart)`, `waers`
    - 단, case_type별 정책 신호를 보장해야 하므로 `isHoliday`, 시간대, `budgetExceeded` 간 정합성은 서버에서 최종 보정

저장 JSON 최소 스키마
- `case_uuid`, `created_at`, `model`, `fallback_used`
- `extracted_entities`, `edited_entities`
- `case_type`, `review_questions`, `review_answers`
- `image_path`
- `memo`: `{bktxt, sgtxt, user_reason}`

----------------------------------------------------------------
3단계: 신규 시연 데이터 UI 개발 (Beta)
대상 파일: `ui/demo_new.py`(신규), `ui/sidebar.py`, `ui/shared.py`

작업 내용
A. `ui/sidebar.py`
- 기존 메뉴명: `시연데이터 제어 (Legacy)`
- 신규 메뉴: `시연데이터 생성 (Beta)` -> `ui/demo_new.py` 연결

B. `ui/shared.py`
- `render_image_with_bboxes(image, boxes, labels=None)` 추가
- bbox 입력은 `[ymin, xmin, ymax, xmax]` 기준으로 렌더링
- bbox 좌표가 비정상일 경우 UI에 경고 표시
- 렌더링 구현 권장안(안정성 우선)
  - Streamlit에서는 Canvas/SVG 오버레이보다 `Pillow(PIL)` 기반 사전 렌더링을 기본으로 사용한다.
  - 구현 방식: PIL로 원본 이미지 복사본에 bbox 사각형+라벨 텍스트를 그린 뒤 `st.image`로 출력.
  - 이유: 의존성/브라우저 호환 이슈가 적고, 좌표 고정형 하이라이트를 가장 빠르게 구현 가능.
  - 품질 기준:
    - 정규화 좌표(0~1000)를 실제 픽셀 좌표로 정확히 변환 후 그리기
    - 좌표 역전/범위 초과 시 clamp 및 보정 로그 남기기
    - 라벨 배경(불투명 박스)+텍스트 대비 색상 적용으로 가독성 확보
    - 원본 이미지는 불변으로 유지하고 렌더링 결과만 표시

C. `ui/demo_new.py`
- 좌측: 파일 업로드 + bbox 오버레이 미리보기
- 우측: 자동 추출 필드 편집
  - `amount_total`, `date_occurrence`, `merchant_name`
  - `적요(bktxt)`, `비고(sgtxt)`, `suggested_summary` 및 `review_questions` 기반 `사유` 입력
- 하단: `테스트 데이터 생성` 버튼

UI 정책
- 비정상 케이스 선택 시: "증빙 첨부 필수" 경고
- 비정상 케이스 + 파일 미첨부: 생성 버튼 disabled
- 정상비교군(NORMAL_BASELINE): 기존처럼 증빙 없이 완료 가능(회귀 유지)
- 비정상 케이스: 업로드 결과가 없으면 `amount/merchant/date` 입력 필수, `적요/비고` 중 최소 1개 필수

----------------------------------------------------------------
4단계: 에이전트 분석 노드 멀티모달 통합
대상 파일: `agent/langgraph_nodes.py`, `agent/langgraph_nodes_review.py`

작업 내용
- `AgentState` 확장
  - `evidence_images: list[str]` (base64)
  - `visual_audit_log: list[dict]`
- critic/review 단계 확장
  - 텍스트 비판과 별도로 이미지-텍스트 모순 점검 함수 호출
  - 모순 신호를 `review_audit`에 누적
  - `bktxt/sgtxt/user_reason`를 LLM 프롬프트 컨텍스트에 포함하여 판단 근거로 사용
- fidelity 계산 통일
  - `rule_fidelity`와 `llm_grounding_score`를 모두 기록
  - 최종 `fidelity = min(rule_fidelity, llm_grounding_score)` 적용

----------------------------------------------------------------
5단계: 테스트, 예외 처리, 완료 기준
대상 파일: `tests/test_multimodal_flow.py`(신규)

필수 테스트
1. 좌표 범위 검증
- 업로드 후 반환 bbox가 모두 0~1000 정수인지

2. 정상비교군 회귀
- NORMAL_BASELINE은 증빙 없이도 완료 가능한지

3. 비정상 케이스 차단
- 비정상 케이스는 증빙 미첨부 시 버튼 disabled인지

4. fallback 검증
- Vision 호출 실패 시 `fallback_used=true`와 사용자 경고가 노출되는지

5. 저장 무결성
- `data/evidence_uploads/{uuid}`에 이미지+json이 함께 저장되는지

완료 기준(Definition of Done)
- UI에서 업로드 즉시 bbox+자동 추출 표시
- 사용자 수정 후 저장하면 UUID 폴더에 결과 영속화
- 규정 기반 질문(`review_questions`)이 UI/검증 파이프라인에서 동일하게 노출
- 멀티모달 실패 시 기능 중단 없이 fallback 동작
- 상기 테스트 5종 통과

----------------------------------------------------------------
구현 순서(권장)
Step 1. `agent/output_models.py`에 모델/검증 로직 추가
Step 2. `utils/llm_azure.py`에 `analyze_visual_evidence` 구현(모델 선택/파싱/폴백)
Step 3. `ui/shared.py` bbox 렌더러 구현 후 `ui/demo_new.py` 연결
Step 4. `services/demo_data_service.py`에 질문 생성/저장 로직 구현
Step 5. `ui/sidebar.py` 메뉴 연결 + 에이전트 노드 통합
Step 6. `tests/test_multimodal_flow.py` 작성 및 회귀 확인

----------------------------------------------------------------
LLM 시스템 프롬프트(최종)
당신은 대한민국 기업 경비 증빙(영수증/전표)에서 핵심 객체를 찾아 위치(bbox)와 데이터를 추출하는 전문 감사 에이전트입니다.
아래 규칙을 엄격히 준수하여 시연 데이터 생성 및 감사 분석에 필요한 정보를 반환하세요.

[좌표 및 스케일 규칙]
- 모든 bbox 좌표는 0~1000 범위의 정수로 환산하여 반환합니다.
- 좌표계: (0,0)=좌상단, (1000,1000)=우하단.
- 형식: [ymin, xmin, ymax, xmax].

[추출 대상 및 정규화]
1. merchant_name: 식당명 또는 가맹점명(상호명만)
2. date_occurrence: 결제 일자, 반드시 YYYY-MM-DD
3. amount_total: 총 결제 금액, 숫자만(통화기호/콤마 제거)

[이미지 품질 진단]
- image_condition: ['clear', 'blurry', 'damaged', 'partial_cut'] 중 하나
- 불확실하면 confidence를 0.5 미만으로 설정

[출력 형식]
- 반드시 순수 JSON만 반환
{
  "image_analysis": {
    "condition": "clear",
    "has_stamp": true
  },
  "entities": [
    {
      "id": "item_1",
      "label": "amount_total",
      "text": "150000",
      "bbox": [120, 540, 170, 760],
      "confidence": 0.98
    }
  ],
  "suggested_summary": "에이전트가 제안하는 1줄 적요",
  "audit_comment": "시각적으로 감지된 특이사항"
}

[사용자 지시]
증빙 이미지에서 금액, 날짜, 식당명을 찾아 좌표와 함께 추출하고, 규정 위반 여부 판단에 필요한 시각 단서를 보고하십시오.
