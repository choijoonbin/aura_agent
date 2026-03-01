# MaterTask PoC

기존 `dwp-frontend`, `dwp-backend`, `aura-platform`의 핵심 흐름을 단일 Python 코드베이스로 옮긴 PoC입니다.

## 목표
- React + Java + Aura 구조를 `FastAPI + Streamlit + LangGraph Agent Runtime`으로 단순화
- 기존 PostgreSQL(`dwp_aura`)를 그대로 재사용
- 인증 없이 `tenant_id=1`, `user_id=1` 기준으로 빠르게 시연
- 분석 실행 시 자율형 에이전트 이벤트를 SSE로 실시간 전달
- 필요 시 기존 Aura `analysis_pipeline`을 전문 분석 툴로 재사용
- LangGraph 기반 오케스트레이션을 기본 런타임으로 사용
- Skill registry 기반 도구 확장 구조를 사용
- HITL(Human-in-the-Loop) 요청을 구조화된 payload로 노출
- 사람 검토 응답 제출 후 재분석(resume) 흐름을 제공
- LangGraph 노드 이벤트를 기반으로 생각(thought) / 행동(action) / 관찰(observation) 스트림을 표시

## 현재 아키텍처
1. FastAPI가 전표 조회, 분석 실행, 스트림 API를 제공
2. MaterTask 내부 `LangGraph agent runtime`이 1차 오케스트레이션 수행
3. 자율형 런타임은 `analyze -> plan -> execute -> verify -> finalize` 단계로 동작
4. 실행 중 필요하면 기존 Aura `analysis_pipeline`을 `legacy_aura_deep_audit` 도구처럼 호출
5. Streamlit은 SSE를 수신해 실제 에이전트 이벤트를 표시
6. shadow 비교는 이번 PoC 범위에서 제외하고, 단일 완성형 agentic 경로에 집중

## 현재 구현 범위
- 전표 목록 조회 API
- 소명 대기함 성격의 필터 조회 API
- 전표 기준 분석 실행 API
- SSE 스트림 구독 API
- 최신 분석 결과 조회 API
- 런 이벤트(raw) 조회 API
- 케이스별 분석 이력 조회 API
- Streamlit에서 실시간 사고 흐름 탭 제공
- HITL 요청 제출 및 재분석 API
- 휴일 위반 시연 데이터 생성/삭제 API
- Streamlit 3개 메뉴
  - 통합 워크벤치
  - 에이전트 스튜디오
  - 시연 데이터 제어
- 내부 자율형 에이전트 런타임
  - LangGraph StateGraph 기반 단계 실행
  - 입력 정규화
  - 조사 계획 수립
  - skill/tool 호출 이벤트 발행
  - 기존 Aura 심층 분석 도구 호출
  - 검증 게이트 적용
  - HITL 필요 여부 판단
  - 최종 결과 생성

## 구조
- `main.py`: FastAPI 엔트리
- `app.py`: Streamlit 엔트리
- `agent/langgraph_agent.py`: LangGraph 기반 주 오케스트레이터
- `agent/native_agent.py`: LangGraph 미사용 시 fallback 런타임
- `agent/skills.py`: skill registry 및 specialist tool 구현
- `agent/hitl.py`: HITL 승격 판단 규칙
- `agent/aura_bridge.py`: 기존 Aura `analysis_pipeline` 브리지
- `agent/event_schema.py`: 실제 에이전트 이벤트 스키마
- `db/models.py`: 최소 SQLAlchemy 모델
- `db/session.py`: DB 세션
- `services/case_service.py`: 전표/분석 payload 조립
- `services/demo_data_service.py`: 시연용 데이터 제어
- `services/stream_runtime.py`: 메모리 기반 run/timeline/result 저장
- `services/policy_service.py`: 규정집 계층형 후보 수집/조항 재정렬/문맥 확장
- `utils/config.py`: 환경설정 및 레퍼런스 소스 경로 검증

## API 목록
- `GET /health`
- `GET /api/v1/vouchers?queue=all|pending&limit=50`
- `POST /api/v1/cases/{voucher_key}/analysis-runs`
- `GET /api/v1/analysis-runs/{run_id}/stream`
- `GET /api/v1/cases/{voucher_key}/analysis/latest`
- `GET /api/v1/analysis-runs/{run_id}/events`
- `POST /api/v1/analysis-runs/{run_id}/hitl`
- `POST /api/v1/demo/seed?count=10`
- `DELETE /api/v1/demo/seed`

## 실행 방법
```bash
cd /Users/joonbinchoi/Work/MaterTask
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

FastAPI 실행:
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8010
```

Streamlit 실행:
```bash
streamlit run app.py --server.port 8502
```

접속 주소:
- Streamlit: `http://localhost:8502`
- FastAPI Swagger: `http://localhost:8010/docs`

## 환경 변수
- `DATABASE_URL`: 기존 PostgreSQL 연결 문자열
- `DEFAULT_TENANT_ID`: 기본 1
- `DEFAULT_USER_ID`: 기본 1
- `AURA_PLATFORM_PATH`: Aura import 경로
- `DWP_BACKEND_PATH`: 레퍼런스 BE 경로
- `DWP_FRONTEND_PATH`: 레퍼런스 FE 경로
- `API_BASE_URL`: Streamlit이 호출할 FastAPI 주소
- `AGENT_RUNTIME_MODE`: 현재 기본 `langgraph`
- `ENABLE_MULTI_AGENT`: 역할 분리 오케스트레이션 사용 여부
- `ENABLE_LANGGRAPH_IF_AVAILABLE`: 향후 LangGraph 승격용 예약 플래그

## 현재 상태
- FastAPI import 확인 완료
- Streamlit import 확인 완료
- Python 구문 검증 완료
- MaterTask 내부 자율형 런타임 우선 동작
- 기존 Aura 환경 미충족 시에도 fallback 분석 가능
- 규정 RAG는 단순 페이지 매칭이 아니라 `키워드 후보 수집 -> 조항 단위 그룹화 -> 부모/자식 문맥 확장 -> 재정렬` 전략 사용

## 다음 고도화 후보
1. DB 영속 저장 기반 run history를 더 정교화
2. 규정/RAG 인덱스 직접 조회 툴을 더 확장
3. specialist tool을 더 세분화
4. 사용자/검토자 협업 히스토리 UI를 고도화
5. 필요 시에만 비교 실험용 shadow 기능 재도입

## 주의사항
- 이 PoC는 운영용이 아니라 시연/검증용입니다.
- 인증/권한 검증은 의도적으로 제거되어 있습니다.
- 분석 결과 저장은 현재 DB 영속 저장이 아니라 메모리 런타임 중심입니다.
- 서버 재시작 시 최신 run/timeline/result 메모리는 초기화됩니다.
