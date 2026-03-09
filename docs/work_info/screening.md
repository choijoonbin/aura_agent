# Screening 고도화 정의서 (Hybrid: LLM + 결정론 보정)

작성일: 2026-03-09
대상 프로젝트: AuraAgent

## 1) 배경
- 현재 AuraAgent 스크리닝은 `agent/screener.py`의 결정론(rule) 기반으로 즉시 분류된다.
- 기존 레퍼런스 조합(`dwp-backend + aura-platform`)은 `screen-batch`에서 LLM을 호출하고, 이후 결정론 신호로 점수/정합성을 보정하는 하이브리드 구조다.
- 목표는 AuraAgent에서도 동일한 운영 철학(LLM 유연성 + 결정론 재현성)을 확보하는 것이다.

## 2) 목표
- 목표 1: `SCREENING_MODE=hybrid`에서 LLM 초기 분류를 수행한다.
- 목표 2: 최종 `score/severity`는 결정론 규칙으로 계산해 재현성을 보장한다.
- 목표 3: LLM 오류/지연 시 자동으로 rule-only fallback 하여 서비스 연속성을 보장한다.
- 목표 4: 기본 동작을 `hybrid`로 전환하되, LLM 실패 시 rule fallback으로 회귀 리스크를 최소화한다.

## 3) 최종 결정사항 (고정 정의)
### 3.1 case_type 표준
- AuraAgent 운영 표준 case_type은 아래 5종으로 고정한다.
- `HOLIDAY_USAGE`, `LIMIT_EXCEED`, `PRIVATE_USE_RISK`, `UNUSUAL_PATTERN`, `NORMAL_BASELINE`
- 내부 소스(시나리오 생성/스크리닝/저장)는 위 5종만 사용한다.
- 레거시 값(`DEFAULT`, `NORMAL`, `DUPLICATE_SUSPECT`, `SPLIT_PAYMENT`)은 신규 생성 경로에서 사용하지 않는다.
- 런타임에서 5종 외 값은 `pre-screened`로 간주하지 않고 스크리닝을 재실행한다.

### 3.2 권한 분리
- LLM 권한: `초기 case_type 제안`, `사유 문장 생성`
- 결정론 권한: `최종 score`, `최종 severity`, `신호-분류 정합성 보정`
- 즉, "판단 유연성은 LLM", "재현성/안정성은 규칙"으로 분리한다.

### 3.3 입력 정규화 기준
- 입력 핵심 필드: `occurredAt`, `isHoliday`, `hrStatus`, `hrStatusRaw`, `mccCode`, `budgetExceeded`, `amount`
- 운영 기본 경로에서는 필드 누락 시에도 스크리닝은 실패하지 않고 보수적으로 분류한다.
- 단, 테스트 데이터 생성 경로(`seed_demo_scenarios`)에서는 `strict_required_fields=true`로 핵심 필드 누락 시 즉시 실패시켜 데이터 품질을 강제한다.

### 3.4 fallback 정책
- LLM 미설정/실패/파싱오류/타임아웃 시 `rule` 결과로 즉시 대체한다.
- fallback 시에도 API 성공 응답을 유지한다(사용자 체감 장애 방지).

## 4) 하이브리드 결정 로직
1. 결정론 신호 추출 및 점수 계산
2. LLM이 case_type과 reason 제안
3. 정합성 보정 규칙 적용
- 예: `HOLIDAY_USAGE`를 LLM이 제안했지만 휴일/휴무 신호가 없으면 rule 결과를 우선
- 예: `LIMIT_EXCEED`를 LLM이 제안했지만 예산초과 신호가 없으면 rule 결과를 우선
- 예: LLM이 `NORMAL_BASELINE` 제안해도 rule score가 충분히 높으면 rule 결과 우선
4. 최종 저장
- `case_type`: 보정 후 최종값
- `score/severity`: 결정론 계산값
- `reason_text`: LLM 사유(정합성 충족 시) 또는 결정론 사유

## 5) 운영 설정값
- `SCREENING_MODE=rule|hybrid` (기본 `hybrid`)
- `SCREENING_LLM_MODEL` (기본 `gpt-5`)
- `SCREENING_LLM_FALLBACK_MODEL` (기본 `gpt-5`)
- `SCREENING_LLM_TEMPERATURE` (기본 `0`)
- `SCREENING_LLM_MAX_TOKENS` (기본 `220`)
- `SCREENING_LLM_TIMEOUT_SECONDS` (기본 `8`)
- `SCREENING_LLM_OVERRIDE_MIN_CONFIDENCE` (기본 `0.75`)

## 6) 성능/안정성 기준 (진행 전 합의)
- SLO(권장): 단건 스크리닝 p95 2초 이내 (LLM 실패 fallback 포함)
- LLM 실패 허용: 실패 시 즉시 rule fallback, 전체 요청 실패로 전파 금지
- 비용 제어: `max_tokens` 제한 + 필요 시 특정 테넌트만 hybrid 활성화

## 7) 관측성/로그 기준
- 아래 항목을 로그에 남긴다.
- `screening_mode`, `screening_source`, `llm_case_type`, `final_case_type`, `hybrid_case_align_reason`
- 운영 대시보드에서는 fallback 비율과 정합성 보정 비율을 추적한다.

## 8) 롤아웃 계획
### Phase 1 (완료 목표: 코드 반영)
- 플래그 기반 하이브리드 엔진 추가
- 기본값 hybrid 전환
- LangGraph screener 노드에서 스크리닝 호출을 thread 오프로딩

### Phase 2 (운영 검증)
- 운영 시나리오에서 fallback 비율/분류 안정성 모니터링
- rule vs hybrid 결과 비교(분류 차이율, fallback 비율, 응답시간)

### Phase 3 (점진 배포)
- 시연 테넌트/시나리오별 임계값(`SCREENING_LLM_OVERRIDE_MIN_CONFIDENCE`) 튜닝
- KPI 안정 시 rule 모드 사용 범위 축소 여부 판단

## 9) 진행 가능 여부 판단
결론: **진행 가능(Go)**

근거:
- 기존 코드 구조에서 플래그 기반 분기 적용이 용이함
- 실패 시 rule fallback으로 안정성 확보 가능
- LLM 실패 시 rule fallback으로 기존 시연/운영 영향 최소화 가능

진행 조건:
- OpenAI/Azure 키 미설정 환경에서도 정상 fallback이 동작해야 함
- 기존 API 응답 스키마(`case_type`, `severity`, `score`, `reason_text`)는 유지해야 함
- `AgentCase` 저장 필드 호환성(문자열 case_type, 수치 score)은 기존과 동일해야 함

## 10) 리스크 및 대응
- 리스크: LLM 응답 형식 이탈(JSON 불일치)
- 대응: 파서 실패 시 rule fallback

- 리스크: LLM 지연으로 워크플로우 체감 지연
- 대응: timeout + fallback + thread 오프로딩

- 리스크: 유형 체계 불일치(DEFAULT/기타)
- 대응: 레거시 값은 pre-screened로 인정하지 않고 재스크리닝하며, LLM 출력은 5종 외 값을 `UNUSUAL_PATTERN`으로 정규화
