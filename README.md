# Aura Agent AI PoC

엔터프라이즈급 에이전트형 AI 금융 감사 어시스턴트 PoC로, LangGraph 기반 자율형 에이전트가 전표를 분석하고 규정 위반 여부를 판단하며, HITL(Human-in-the-Loop) 체크포인트를 통해 검증 가능한 감사 결과를 제공합니다.

## 📋 목차

- [프로젝트 개요](#-프로젝트-개요)
- [주요 기능](#-주요-기능)
- [프로젝트 구조](#-프로젝트-구조)
- [기술 스택](#-기술-스택)
- [동작 원리](#-동작-원리)
- [설치 및 실행](#-설치-및-실행)
- [환경 변수 설정](#-환경-변수-설정)
- [API 엔드포인트](#-api-엔드포인트)
- [Langfuse 통합](#-langfuse-통합)
- [사용 예시](#-사용-예시)
- [주의사항](#-주의사항)
- [기초 문서 (docs/Edu)](#-기초-문서-docsedu)

## 🎯 프로젝트 개요

이 프로젝트는 기존 `dwp-frontend`, `dwp-backend`, `aura-platform`의 핵심 흐름을 **FastAPI + Streamlit + LangGraph** 단일 코드베이스로 통합한 PoC입니다.

### 핵심 목표

1. **아키텍처 단순화**: React + Java + Aura 구조를 Python 단일 스택으로 전환
2. **DB 재사용**: 기존 PostgreSQL(`dwp_aura`) 스키마를 그대로 사용
3. **에이전트형 오케스트레이션**: LangGraph 기반 자율형 에이전트 런타임
4. **실시간 이벤트 스트리밍**: SSE로 에이전트 사고/행동/관찰 이벤트 전달
5. **HITL 통합**: 구조화된 HITL 요청 및 사람 검토 응답 후 재분석(resume) 흐름
6. **증거 기반 판단**: 규정·전표 기반 근거 수집, 전문 분석 도구(tool) 호출

## ✨ 주요 기능

### 1. LangGraph 기반 에이전트 런타임

2단계 프로세스 (원본 aura-platform 흐름 충실히 구현):

**Phase 0 — 스크리닝 (Screening)**
- **screener**: 전표 원시 데이터에서 케이스 유형을 자율 분류 (BE 힌트 없음)
  - `hr_status`, `mcc_code`, `budat`, `cputm`, `budget_exceeded_flag` 등 전표 필드 기반 결정론적 스코어링
  - `HOLIDAY_USAGE` / `LIMIT_EXCEED` / `PRIVATE_USE_RISK` / `UNUSUAL_PATTERN` / `NORMAL_BASELINE` 분류
  - 결과를 `AgentCase` 테이블에 저장 후 분석 Phase에 전달
  - 독립 호출 가능: `POST /api/v1/cases/{voucher_key}/screen`

**Phase 1 — 심층 분석 (Analysis)**
- **intake**: 전표 입력 정규화, 스크리닝 결과 기반 데이터 확정
- **planner**: 케이스 유형별 조사 계획 수립 및 도구 선택
- **execute**: LangChain tool 순차 실행 (policy_rulebook_probe, document_evidence_probe 등)
- **critic**: tool 결과 기반 과잉 주장·반례 검토
- **verify**: 점수 산정, HITL 필요 여부 판단
- **hitl_pause** (HITL 시): 사람 검토 대기(interrupt), 응답 후 같은 run으로 resume
- **reporter**: 최종 설명 문장·요약 생성
- **finalizer**: 최종 결과 생성 및 근거 기반 설명

| 관련 기초 문서 | [Langgraph_Logic.md](docs/Edu/Langgraph_Logic.md) |
|----------------|---------------------------------------------------|
| 코드 참고 | [`agent/screener.py`](agent/screener.py), [`agent/langgraph_agent.py`](agent/langgraph_agent.py) |

### 2. Tool 기반 도구 확장 (LangChain StructuredTool)
- **policy_rulebook_probe**: 내부 규정집 조항 조회, 키워드 후보 수집 → 조항 단위 그룹화 → 문맥 확장
- **document_evidence_probe**: 전표 증거 수집
- **holiday_compliance_probe**: 휴일/휴무 리스크 확인
- **budget_risk_probe**: 예산 초과 확인
- **merchant_risk_probe**: 업종/가맹점 업종 코드(MCC) 위험 확인
- **legacy_aura_deep_audit**: 기존 Aura 심층 분석 파이프라인 호출 (선택적)

| 관련 기초 문서 | [Langgraph_Logic.md](docs/Edu/Langgraph_Logic.md) §4 |
|----------------|------------------------------------------------------|
| 코드 참고 | [`agent/agent_tools.py`](agent/agent_tools.py) |

### 3. HITL (Human-in-the-Loop)
- **구조화된 HITL 요청**: `hitl_request`, `reasons`, `questions`, `handoff` 필드
- **HITL 필수 조건**: 핵심 필드 누락, 증거 부족, 규정 해석 모호, specialist 결과 충돌 시
- **재분석 흐름**: 사람 검토 응답 제출 후 **같은 run**에서 재평가 및 최종 확정. `resumed_run_id`는 동일 `run_id`를 반환한다.
- **「분석 이어하기」 재개 시점**: 설계상 **hitl_pause 노드 직후**부터 이어서 **hitl_validate → reporter → finalizer**만 실행한다.  
  단, 체크포인트를 찾지 못하면(single-worker가 아니거나 `CHECKPOINTER_BACKEND=memory`인 경우) **screener부터 전부 재실행**된다.  
  **HITL 직후 노드부터만 재개**하려면 `CHECKPOINTER_BACKEND=postgres`로 두고, `langgraph-checkpoint-postgres` 설치 후 DB에 체크포인트를 저장해 두어야 한다.

| 관련 기초 문서 | [HITL Logic.md](docs/Edu/HITL%20Logic.md) |
|----------------|-------------------------------------------|
| 코드 참고 | [`agent/hitl.py`](agent/hitl.py), [`main.py`](main.py) — HITL 응답 API |

### 4. SSE 실시간 이벤트 스트리밍
- **NODE_START / NODE_END**, **PLAN_READY**, **TOOL_CALL / TOOL_RESULT**, **GATE_APPLIED**, **HITL_REQUESTED** 등 구조화 이벤트를 스트림으로 전달.

| 코드 참고 | [`agent/event_schema.py`](agent/event_schema.py), [`main.py`](main.py) — SSE 스트림 |
|-----------|-------------------------------------------------------------------------------------|

### 5. Streamlit 통합 UI
| 화면 | 설명 |
|------|------|
| **AI 워크스페이스** | 전표 선택, 분석 실행, 실시간 사고 흐름, HITL 응답, 결과·근거 확인 |
| **에이전트 스튜디오** | 오케스트레이션·실행 도구 그래프, 모델/도구 설정 |
| **규정문서 라이브러리** | RAG 문서 인덱싱, 청킹 실험실, 품질 리포트 |
| **시연 데이터 제어** | 시나리오별 전표 생성/삭제 |

스트림 UI는 이벤트 타입별 아이콘, 노드별 reasoning 표시, 도구 툴팁, KST 날짜 형식을 사용하며, 완료 후 타임라인 재조회로 접기/펼치기가 가능하다.

| 코드 참고 | [`app.py`](app.py), [`ui/workspace.py`](ui/workspace.py), [`ui/shared.py`](ui/shared.py) |
|-----------|---------------------------------------------------------------------------------------|

### 6. RAG 규정집 통합
- 규정집 계층형 후보 수집·조항 재정렬·문맥 확장
- 청킹 실험실: TXT 업로드, 전략별 미리보기(계층/조항/슬라이딩)
- 문서 메타, 품질 리포트, 청크 목록 조회

| 관련 기초 문서 | [Chunk Logic.md](docs/Edu/Chunk%20Logic.md) |
|----------------|---------------------------------------------|
| 코드 참고 | [`services/policy_service.py`](services/policy_service.py), [`services/rag_chunk_lab_service.py`](services/rag_chunk_lab_service.py), [`services/chunking_pipeline.py`](services/chunking_pipeline.py) |

### 7. BE(dwp-backend)·Aura 정합성
- **케이스 목록 데이터**: 전표(fi_doc_header, fi_doc_item)를 기준으로 조회하고, 배지용 값(상태·심각도·유형)은 agent_case와 LEFT OUTER JOIN으로 가져옵니다. 스크리닝 전에는 case_type/severity가 없어 "미분류/낮음"으로 표시됩니다.
- **테스트 데이터 생성**: `services/demo_data_service.py`의 시나리오별 필드(hr_status, mcc_code, budget_flag, day_mode, hour)는 BE `DemoViolationService`의 `setContextForScenario`·`preferredMccCodes`·`resolveBudgetExceededFlag` 규칙과 동일하게 적용됨. (HOLIDAY_USAGE는 budget N, LIMIT_EXCEED는 budget Y 등.)
- **스크리닝 입력**: `case_service._build_screening_body()`는 BE `DetectBatchService.buildFlattenedBatchItem()`와 동일한 핵심 필드(occurredAt, hrStatus, hrStatusRaw, mccCode, budgetExceeded, isHoliday)로 구성하며, **intended_risk_type은 포함하지 않음** — Aura가 전표 원시 데이터만으로 케이스 유형 분류.
- **스크리닝 → 분석 흐름**: (1) 에이전트가 스크리닝으로 유형을 판단한 뒤 결과를 `agent_case`에 저장(업데이트). (2) 이후 **분석 버튼**을 누르면 BE와 동일하게, 스크리닝 결과(`case_type`, `screening_reason_text`)와 전표/evidence 필드를 담은 `body_evidence`를 에이전트에 넘기고, 에이전트는 그 값을 기준으로 심층 분석을 수행.

## 📁 프로젝트 구조

```
AuraAgent/
├── main.py                      # FastAPI 엔트리
├── app.py                       # Streamlit 엔트리 (UI)
├── requirements.txt             # Python 의존성
├── .env.example                 # 환경 변수 예시
│
├── agent/                       # LangGraph 에이전트 런타임
│   ├── __init__.py
│   ├── langgraph_agent.py       # LangGraph 기반 주 오케스트레이터
│   ├── native_agent.py          # LangGraph 미사용 fallback 런타임
│   ├── agent_tools.py           # LangChain Tool 등록 및 probe 구현
│   ├── hitl.py                  # HITL 승격 판단 규칙
│   ├── aura_bridge.py           # 기존 Aura analysis_pipeline 브리지
│   └── event_schema.py          # 에이전트 이벤트 스키마
│
├── api/                         # API 모듈
│   └── __init__.py
│
├── db/                          # 데이터베이스
│   ├── __init__.py
│   ├── models.py                # SQLAlchemy 모델
│   └── session.py               # DB 세션
│
├── services/                    # 비즈니스 로직
│   ├── __init__.py
│   ├── case_service.py          # 전표/분석 payload 조립
│   ├── demo_data_service.py     # 시연용 데이터 제어
│   ├── stream_runtime.py        # 메모리 기반 run/timeline/result 저장
│   ├── policy_service.py        # 규정집 검색(BM25/Dense/RRF/Rerank)
│   ├── rag_library_service.py   # RAG 문서 라이브러리
│   ├── rag_chunk_lab_service.py # 청킹 전략·계층 파싱
│   ├── chunking_pipeline.py     # 청킹→저장→임베딩 파이프라인
│   ├── agent_studio_service.py  # 에이전트 스튜디오 API
│   ├── persistence_service.py   # 분석 결과 영속화
│   ├── runtime_persistence_service.py
│   └── schemas.py               # Pydantic 스키마
│
├── utils/                       # 유틸리티
│   ├── __init__.py
│   └── config.py                # 환경설정 및 레퍼런스 경로 검증
│
├── 규정집/                      # 규정 텍스트 소스
│   └── 사내_경비_지출_관리_규정_v2.0_확장판.txt
│
└── README.md
```

상세 동작·아키텍처는 [기초 문서 (docs/Edu)](#-기초-문서-docsedu)를 참고한다.

## 🛠 기술 스택

### 백엔드
- **FastAPI** (0.115.0+): 고성능 비동기 웹 프레임워크
- **LangGraph** (0.2.0+): 에이전트 오케스트레이션
- **LangChain Core** (0.3.0+): LLM 연동 기반
- **SQLAlchemy** (2.0.30+): ORM 및 DB 관리
- **Pydantic** (2.8.0+): 데이터 검증 및 설정
- **Psycopg2**: PostgreSQL 연결

### 프론트엔드
- **Streamlit** (1.42.0+): 웹 UI 프레임워크

### 기타
- **matplotlib/networkx**: 그래프 시각화(서버 내 PNG 렌더)
- **httpx**: HTTP 클라이언트
- **python-dotenv**: 환경 변수 로드

### 데이터베이스
- **PostgreSQL**: 기존 `dwp_aura` 스키마 재사용

| 코드 참고 | [`requirements.txt`](requirements.txt) |
|-----------|---------------------------------------|

## 🔄 동작 원리

### 1. LangGraph 에이전트 워크플로우

```mermaid
graph TD
    Start([시작]) --> Screener[screener: 유형 스크리닝]
    Screener --> Intake[intake: 입력 정규화]
    Intake --> Planner[planner: 조사 계획 수립]
    Planner --> Execute[execute: LangChain tool 실행]
    Execute --> Critic[critic: 반례/과잉 주장 점검]
    Critic --> Verify[verify: 게이트 + HITL 판단]
    Verify -->|HITL 필요| HitlPause[hitl_pause: interrupt]
    HitlPause -->|응답 제출| Resume[Command(resume)]
    Resume --> Reporter[reporter: 설명/요약]
    Verify -->|자동 진행| Reporter
    Reporter --> Finalizer[finalizer: 최종 확정]
    Finalizer --> End([종료])
    
    style Intake fill:#e1f5ff
    style Planner fill:#e1f5ff
    style Execute fill:#fff4e1
    style Critic fill:#e8f5e9
    style Verify fill:#e8f5e9
    style Reporter fill:#f3e5f5
    style HitlPause fill:#ffebee
```

| 관련 기초 문서 | [Langgraph_Logic.md](docs/Edu/Langgraph_Logic.md) §2–3 |
|----------------|--------------------------------------------------------|

### 2. Agent 노드 상세

| 노드 | 역할 | 출력 |
|------|------|------|
| **screener** | 전표 원시 데이터에서 케이스 유형 분류 | `screening_result`, `intended_risk_type` |
| **intake** | 전표 입력 정규화, flags 추출 | `flags`, `pending_events` |
| **planner** | 위험 유형별 조사 계획 수립 | `plan`, `pending_events` |
| **execute** | LangChain tool 순차 호출, 점수 산정 | `tool_results`, `score_breakdown`, `pending_events` |
| **critic** | 과잉 주장·반례 검토, 재계획 여부 | `critic_output`, `replan_context` |
| **verify** | 게이트 적용 + HITL 승격 판단 | `hitl_request` 또는 null, `verification` |
| **hitl_pause** | HITL 시 interrupt, 재개 시 `hitlResponse` 반영 | — |
| **reporter** | 근거 기반 최종 설명/요약 생성 | `final_result` |
| **finalizer** | 상태·점수·이력 최종 확정 | `final_result` |

### 3. Tool 선택 흐름 (plan 기반)

- **휴일/휴무**: `holiday_compliance_probe`
- **예산 초과**: `budget_risk_probe`
- **가맹점 업종 코드(MCC)/업종**: `merchant_risk_probe`
- **공통**: `document_evidence_probe` → `policy_rulebook_probe` → `legacy_aura_deep_audit` (조건부 생략 가능)

| 관련 기초 문서 | [Langgraph_Logic.md](docs/Edu/Langgraph_Logic.md) §5 |
|----------------|--------------------------------------------------------|

### 4. SSE 이벤트 구조

```json
{"event_type": "NODE_START", "node": "intake", "phase": "analyze", "message": "...", "thought": "...", "action": "...", "metadata": {...}}
{"event_type": "TOOL_CALL", "node": "executor", "phase": "execute", "tool": "policy_rulebook_probe", "message": "...", "metadata": {...}}
{"event_type": "TOOL_RESULT", "node": "executor", "phase": "execute", "tool": "policy_rulebook_probe", "observation": "...", "metadata": {...}}
{"event_type": "HITL_REQUESTED", "node": "verify", "phase": "verify", "metadata": {"hitl_request": {...}}}
```

| 코드 참고 | [`agent/event_schema.py`](agent/event_schema.py) |
|-----------|--------------------------------------------------|

## 🚀 설치 및 실행

### 1. 저장소 클론 및 의존성 설치

```bash
cd /Users/joonbinchoi/Work/AuraAgent
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

#### (선택) Postgres 체크포인터 (HITL 재개 안정화)

`CHECKPOINTER_BACKEND=postgres`(기본값) 사용 시 체크포인트가 DB에 저장됩니다. 해당 패키지가 없으면 자동으로 MemorySaver로 fallback됩니다.

```bash
pip install -r requirements-checkpoint-postgres.txt
```

#### (선택) Phase F cross-encoder rerank

규정 검색 결과를 cross-encoder로 재정렬하려면 `sentence-transformers`(및 `torch`)를 설치합니다. **미설치 시에도 동작하며**, 이때는 기존 lexical 순위가 그대로 사용됩니다.

```bash
# Python/torch 호환 환경에서만 (예: Python 3.10~3.12 권장)
pip install -r requirements-optional.txt
```

| 코드 참고 | [`services/retrieval_quality.py`](services/retrieval_quality.py), [`services/policy_service.py`](services/policy_service.py) |
|-----------|------------------------------------------------------------------------------------------------------------------------|

### 2. 환경 변수 설정

`.env.example`을 복사하여 `.env` 생성 후 필요한 값을 설정합니다. (자세한 내용은 [환경 변수 설정](#-환경-변수-설정) 참조)

```bash
cp .env.example .env
```

### 3. FastAPI 백엔드 실행

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8010
```

백엔드 API는 `http://localhost:8010`에서 실행됩니다.

### 4. Streamlit 프론트엔드 실행

새 터미널에서:

```bash
streamlit run app.py --server.port 8502
```

Streamlit 앱은 `http://localhost:8502`에서 실행됩니다.

### 접속 주소

| 서비스 | URL |
|--------|-----|
| Streamlit UI | http://localhost:8502 |
| FastAPI Swagger | http://localhost:8010/docs |

| 코드 참고 | [`main.py`](main.py), [`app.py`](app.py) |
|-----------|----------------------------------------|

### 로그 확인 (HITL / 분석 이어가기 추적)

- **FastAPI(uvicorn) 터미널**: `[RESUME_TRACE]`, `[analysis]` — review-submit 경로(checkpoint 재개 vs 스크리닝부터 재실행), 에이전트 재개 시도/실패
- **Streamlit 터미널**: `[HITL_CLOSE]`, `[RESUME_TRACE]` — 팝업 닫기 단계, UI에서 review-submit 호출
- 브라우저 개발자 도구 콘솔에는 위 로그가 나오지 않으며, `Unrecognized feature` 등은 Streamlit/브라우저 경고로 무시해도 됨
- **스트림이 처음부터 다시 시작하는 경우**: FastAPI 터미널에서 `[RESUME_TRACE] review_submit → 경로: 처음부터 재실행 (base_status=XXX != HITL_REQUIRED)` 가 나오면, 해당 run의 마지막 결과 상태가 HITL_REQUIRED(실제 interrupt)가 아니라서 checkpoint 재개가 아닌 전체 재실행으로 간 것입니다. 실제로 interrupt된 run(HITL_REQUIRED)에서 "분석 이어하기"를 눌렀을 때만 checkpoint 재개가 시도됩니다.

### 5. 테스트 실행

그래프·도구 스키마·HITL·citation 관련 단위 테스트:

```bash
pytest tests/ -v
```

| 코드 참고 | [`tests/`](tests/) — test_graph, test_tool_schema, test_interrupt_resume, test_citation_binding |
|-----------|------------------------------------------------------------------------------------------------|

## ⚙️ 환경 변수 설정

### `.env` 파일 예시

```env
# 앱 설정
APP_ENV=local
APP_HOST=0.0.0.0
APP_PORT=8010
STREAMLIT_PORT=8502

# 데이터베이스
DATABASE_URL=postgresql://dwp_user:dwp_password@localhost:5432/dwp_aura
DEFAULT_TENANT_ID=1
DEFAULT_USER_ID=1

# API
API_BASE_URL=http://localhost:8010

# 레퍼런스 경로 (선택)
AURA_PLATFORM_PATH=/path/to/aura-platform
DWP_BACKEND_PATH=/path/to/dwp-backend
DWP_FRONTEND_PATH=/path/to/dwp-frontend

# 에이전트 런타임
# native: AuraAgent 내부 자율형 에이전트 우선
# aura: legacy aura 전용 모드
# hybrid: native + aura 병행
AGENT_RUNTIME_MODE=langgraph

# 멀티 에이전트 역할 분리
ENABLE_MULTI_AGENT=true
ENABLE_LANGGRAPH_IF_AVAILABLE=true

# Reasoning LLM 스트림
ENABLE_REASONING_LIVE_LLM=true
REASONING_LLM_MODEL=gpt-4o-mini
REASONING_LLM_LABEL=Azure OpenAI gpt-4o-mini
SCREENING_MODE=hybrid
SCREENING_LLM_MODEL=gpt-4o-mini
SCREENING_LLM_FALLBACK_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://<your-resource>.openai.azure.com/
OPENAI_API_VERSION=2024-12-01-preview
OPENAI_API_KEY=<your-key>

# RAG 임베딩 (Azure/OpenAI)
OPENAI_EMBEDDING_MODEL=text-embedding-3-large
OPENAI_EMBEDDING_DIM=3072
RAG_EMBEDDING_COLUMN=embedding_az
RAG_EMBEDDING_CAST_TYPE=halfvec
OPENAI_EMBEDDING_BATCH_SIZE=64
OPENAI_EMBEDDING_MAX_RETRIES=3
```

### 주요 환경 변수 설명

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `DATABASE_URL` | PostgreSQL 연결 문자열 | - |
| `DEFAULT_TENANT_ID` | 기본 tenant ID | 1 |
| `DEFAULT_USER_ID` | 기본 user ID | 1 |
| `API_BASE_URL` | Streamlit이 호출할 FastAPI 주소 | http://localhost:8010 |
| `AGENT_RUNTIME_MODE` | 에이전트 런타임 모드 | langgraph |
| `ENABLE_MULTI_AGENT` | 멀티 에이전트 역할 분리 사용 | true |
| `LANGFUSE_ENABLED` | Langfuse 추적 활성화 | false |
| `LANGFUSE_PUBLIC_KEY` | Langfuse Public Key | - |
| `LANGFUSE_SECRET_KEY` | Langfuse Secret Key | - |
| `LANGFUSE_HOST` | Langfuse 서버 URL | https://cloud.langfuse.com |
| `OPENAI_BASE_URL` | OpenAI/Azure 엔드포인트 | - |
| `OPENAI_API_KEY` | OpenAI/Azure API 키 | - |
| `REASONING_LLM_MODEL` | Reasoning 생성 모델 | gpt-4o-mini |
| `SCREENING_MODE` | 스크리닝 실행 모드(`rule`/`hybrid`) | hybrid |
| `SCREENING_LLM_MODEL` | 스크리닝 분류 모델 | gpt-4o-mini |
| `SCREENING_LLM_FALLBACK_MODEL` | 스크리닝 fallback 모델 | gpt-4o-mini |
| `OPENAI_EMBEDDING_MODEL` | 임베딩 모델(배포명) | text-embedding-3-large |
| `OPENAI_EMBEDDING_DIM` | 임베딩 차원 | 3072 |
| `RAG_EMBEDDING_COLUMN` | rag_chunk 임베딩 컬럼 | embedding_az |
| `RAG_EMBEDDING_CAST_TYPE` | 쿼리/저장 캐스팅 타입(`vector`/`halfvec`) | halfvec |
| `CHECKPOINTER_BACKEND` | LangGraph 체크포인트 저장소. `memory`(기본)=프로세스 메모리(동일 프로세스에서만 HITL 재개). `postgres`=DB 저장(안정적 PoC 시 권장, 재시작 후에도 hitl_pause 직후부터 재개) | memory |

| 코드 참고 | [`.env.example`](.env.example), [`utils/config.py`](utils/config.py) |
|-----------|---------------------------------------------------------------------|

## 🔍 Langfuse 통합

연결 시 **테스트·분석 실행 시** LangGraph 실행과 LLM 호출을 [Langfuse 대시보드](https://cloud.langfuse.com)에서 추적·모니터링할 수 있습니다.

1. **Langfuse 계정**: https://cloud.langfuse.com 에서 계정 생성 후 프로젝트 선택
2. **키 발급**: 프로젝트 설정에서 Public Key / Secret Key 확인
3. **환경 변수**: `.env`에 `LANGFUSE_ENABLED=true`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` 설정
4. **분석 실행**: AI 워크스페이스에서 분석 실행 시 `run_id`가 세션 ID로 전달되어 대시보드에서 run별로 조회 가능
5. **비활성화**: `LANGFUSE_ENABLED=false`(기본값)이면 전송하지 않음

| 코드 참고 | [`utils/config.py`](utils/config.py), [`agent/langgraph_agent.py`](agent/langgraph_agent.py) |
|-----------|--------------------------------------------------------------------------------------------|

## 📡 API 엔드포인트

### 헬스 체크
- **GET** `/health` — 서버 상태 확인

### 전표 및 분석
- **GET** `/api/v1/vouchers?queue=all|pending&limit=50` — 전표 목록 조회
- **POST** `/api/v1/cases/{voucher_key}/screen` — Phase 0 스크리닝 (케이스 유형 분류, AgentCase 생성/갱신)
- **POST** `/api/v1/cases/{voucher_key}/analysis-runs` — Phase 1 분석 시작 (screener_node 자동 포함)
- **GET** `/api/v1/analysis-runs/{run_id}/stream` — SSE 스트림 구독
- **GET** `/api/v1/cases/{voucher_key}/analysis/latest` — 최신 분석 결과
- **GET** `/api/v1/cases/{voucher_key}/analysis/history` — 케이스별 분석 이력
- **GET** `/api/v1/analysis-runs/{run_id}/events` — 런 이벤트(raw) 조회
- **POST** `/api/v1/analysis-runs/{run_id}/hitl` — HITL 응답 제출 (재개 시 새 run 생성 없이 동일 `run_id`로 이어짐. 응답 `resumed_run_id` = 동일 run_id)

### RAG / 에이전트
- **GET** `/api/v1/rag/documents` — RAG 문서 목록
- **GET** `/api/v1/rag/documents/{doc_id}` — RAG 문서 상세
- **GET** `/api/v1/agents` — 에이전트 목록
- **GET** `/api/v1/agents/{agent_id}` — 에이전트 상세

### 시연 데이터
- **GET** `/api/v1/demo/scenarios` — 시연 시나리오 목록
- **GET** `/api/v1/demo/seeded` — 저장된 시연 전표 목록
- **POST** `/api/v1/demo/seed?scenario=...&count=10` — 시연 데이터 생성
- **DELETE** `/api/v1/demo/seed` — 시연 데이터 전체 삭제

| 코드 참고 | [`main.py`](main.py) — 전표/분석/RAG/시연 API |
|-----------|---------------------------------------------|

## 📝 사용 예시

### Streamlit UI 사용

1. `http://localhost:8502` 접속
2. **AI 워크스페이스**에서 전표 선택 또는 시연 데이터 생성
3. "분석 실행" 버튼 클릭
4. 실시간 사고 흐름 탭에서 에이전트 진행 상황 확인
5. HITL 요청 시 질문에 응답 후 재분석 진행
6. 최종 결과 및 근거 확인

### API 직접 호출

```python
import requests

# 분석 실행
resp = requests.post(
    "http://localhost:8010/api/v1/cases/DEMO-HOLIDAY-001/analysis-runs"
)
run_id = resp.json()["run_id"]

# SSE 스트림 구독
with requests.get(
    f"http://localhost:8010/api/v1/analysis-runs/{run_id}/stream",
    stream=True
) as r:
    for line in r.iter_lines():
        if line:
            print(line.decode())
```

## ⚠️ 주의사항

- **PoC 목적**: 이 프로젝트는 운영용이 아니라 시연/검증용입니다.
- **인증 미적용**: 인증/권한 검증은 의도적으로 제거되어 있으며, `tenant_id=1`, `user_id=1` 고정 사용
- **메모리 기반 런타임**: 분석 결과 일부는 메모리 기반으로 저장되며, 서버 재시작 시 초기화됩니다.
- **HITL resume**: 기본값 `memory`는 같은 프로세스 내에서만 재개됩니다. **재시작 후에도** hitl_pause 직후부터 재개하려면 `CHECKPOINTER_BACKEND=postgres`로 두고 `pip install -r requirements-checkpoint-postgres.txt` 후 사용하세요.
- **Aura 연동**: `AURA_PLATFORM_PATH`가 설정된 경우에만 legacy Aura 심층 분석 도구 사용 가능

| 코드 참고 | [`db/models.py`](db/models.py), [`services/stream_runtime.py`](services/stream_runtime.py), [`agent/aura_bridge.py`](agent/aura_bridge.py) |
|-----------|--------------------------------------------------------------------------------------------------------------------------------------|

## 🧪 다음 고도화 후보

1. DB 영속 저장 기반 run history 정교화
2. 규정/RAG 인덱스 직접 조회 도구 확장
3. 도구(tool) 세분화 및 planner 기반 동적 선택
4. 사용자/검토자 협업 히스토리 UI 고도화
5. shadow 비교 실험용 기능 재도입 (선택)

## 📚 기초 문서 (docs/Edu)

동작·프로세스·아키텍처의 **기준 설명 자료**는 `docs/Edu/`에 있으며, 시연/설명회 및 온보딩 시 README와 함께 참고한다.

| 문서 | 내용 |
|------|------|
| [HITL Logic.md](docs/Edu/HITL%20Logic.md) | HITL 라이프사이클, 중단 시 저장 데이터, 재개 시 로드·이어하기, API·UI 흐름 |
| [Chunk Logic.md](docs/Edu/Chunk%20Logic.md) | 규정집 RAG 청킹 전략, 계층 구조, 파이프라인(저장·임베딩), 검색 연계 |
| [Langgraph_Logic.md](docs/Edu/Langgraph_Logic.md) | LangGraph 오케스트레이션, 노드·도구·상태, HITL 연계, 자율성 검증 |

## 🤝 기여

이슈 리포트 및 풀 리퀘스트를 환영합니다.

## 📄 라이선스

[라이선스 정보를 여기에 추가하세요]

---

**프로젝트**: Aura Agent AI PoC (AuraAgent)  
**목적**: 엔터프라이즈급 에이전트형 금융 감사 어시스턴트 PoC
