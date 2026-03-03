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

## 🎯 프로젝트 개요

이 프로젝트는 기존 `dwp-frontend`, `dwp-backend`, `aura-platform`의 핵심 흐름을 **FastAPI + Streamlit + LangGraph** 단일 코드베이스로 통합한 PoC입니다.

### 핵심 목표

1. **아키텍처 단순화**: React + Java + Aura 구조를 Python 단일 스택으로 전환
2. **DB 재사용**: 기존 PostgreSQL(`dwp_aura`) 스키마를 그대로 사용
3. **에이전트형 오케스트레이션**: LangGraph 기반 자율형 에이전트 런타임
4. **실시간 이벤트 스트리밍**: SSE로 에이전트 사고/행동/관찰 이벤트 전달
5. **HITL 통합**: 구조화된 HITL 요청 및 사람 검토 응답 후 재분석(resume) 흐름
6. **증거 기반 판단**: 규정·전표 기반 근거 수집, 전문 분석 도구(skill) 호출

## ✨ 주요 기능

### 1. LangGraph 기반 에이전트 런타임
- **intake**: 전표 입력 정규화, 휴일/시간대/근태/예산/업종 신호 추출
- **planner**: 위험 신호별 조사 계획 수립 및 도구 선택
- **execute**: skill/tool 순차 실행 (policy_rulebook_probe, document_evidence_probe 등)
- **verify**: 점수 산정, HITL 필요 여부 판단
- **finalize**: 최종 결과 생성 및 근거 기반 설명

> 참고: [`agent/langgraph_agent.py`](agent/langgraph_agent.py#L176-L343) — intake/planner/execute/verify/finalizer 노드 구현

### 2. Skill 기반 도구 확장
- **policy_rulebook_probe**: 내부 규정집 조항 조회, 키워드 후보 수집 → 조항 단위 그룹화 → 문맥 확장
- **document_evidence_probe**: 전표 증거 수집
- **holiday_compliance_probe**: 휴일/휴무 리스크 확인
- **budget_risk_probe**: 예산 초과 확인
- **merchant_risk_probe**: 업종/MCC 위험 확인
- **legacy_aura_deep_audit**: 기존 Aura 심층 분석 파이프라인 호출 (선택적)

> 참고: [`agent/skills.py`](agent/skills.py#L130) — SKILL_REGISTRY 및 각 skill 구현

### 3. HITL (Human-in-the-Loop)
- **구조화된 HITL 요청**: `hitl_request`, `reasons`, `questions`, `handoff` 필드
- **HITL 필수 조건**: 핵심 필드 누락, 증거 부족, 규정 해석 모호, specialist 결과 충돌 시
- **재분석 흐름**: 사람 검토 응답 제출 후 재평가 및 최종 확정

> 참고: [`agent/hitl.py`](agent/hitl.py#L6) — HITL 승격 판단, [`main.py`](main.py#L223-L290) — HITL 응답 제출 API

### 4. SSE 실시간 이벤트 스트리밍
- **NODE_START / NODE_END**: 노드 시작·종료
- **PLAN_READY**: 조사 계획 확정
- **TOOL_CALL / TOOL_RESULT**: 도구 호출 및 결과
- **GATE_APPLIED**: 검증 게이트 적용
- **HITL_REQUESTED**: HITL 승격

> 참고: [`agent/event_schema.py`](agent/event_schema.py#L9) — AgentEvent 스키마, [`main.py`](main.py#L380-L405) — SSE 스트림 API

### 5. Streamlit 통합 UI
- **AI 워크스페이스**: 전표 선택, 분석 실행, 실시간 사고 흐름 탭, HITL 응답
- **에이전트 스튜디오**: 에이전트 모델/프롬프트/도구/지식 설정
- **규정문서 라이브러리**: RAG 문서 인덱싱, 품질 리포트, 청크 목록
- **시연 데이터 제어**: 대표 시나리오 생성/삭제, 시연 전표 관리

> 참고: [`app.py`](app.py#L1192) — AI 워크스페이스, [`app.py`](app.py#L1332) — 에이전트 스튜디오, [`app.py`](app.py#L1479) — 규정문서 라이브러리, [`app.py`](app.py#L1607) — 시연 데이터 제어

### 6. RAG 규정집 통합
- 규정집 계층형 후보 수집 및 조항 재정렬
- 청킹 실험실: TXT 업로드, 하이브리드/조항 우선/슬라이딩 윈도우 전략
- 문서메타, 품질 리포트, 청크 목록 조회

> 참고: [`services/policy_service.py`](services/policy_service.py) — 규정집 후보 수집/조항 재정렬, [`services/rag_library_service.py`](services/rag_library_service.py) — RAG 문서 라이브러리, [`services/rag_chunk_lab_service.py`](services/rag_chunk_lab_service.py) — 청킹 실험실

## 📁 프로젝트 구조

```
AruaAgent/
├── main.py                      # FastAPI 엔트리
├── app.py                       # Streamlit 엔트리 (UI)
├── requirements.txt             # Python 의존성
├── .env.example                 # 환경 변수 예시
│
├── agent/                       # LangGraph 에이전트 런타임
│   ├── __init__.py
│   ├── langgraph_agent.py       # LangGraph 기반 주 오케스트레이터
│   ├── native_agent.py          # LangGraph 미사용 fallback 런타임
│   ├── skills.py                # skill registry 및 specialist tool 구현
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
│   ├── policy_service.py        # 규정집 계층형 후보 수집/조항 재정렬/문맥 확장
│   ├── rag_library_service.py   # RAG 문서 라이브러리
│   ├── rag_chunk_lab_service.py # 청킹 실험실
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

> 참고: [`main.py`](main.py) — FastAPI 엔트리, [`app.py`](app.py) — Streamlit 엔트리

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
- **Graphviz**: 그래프 시각화
- **httpx**: HTTP 클라이언트
- **python-dotenv**: 환경 변수 로드

### 데이터베이스
- **PostgreSQL**: 기존 `dwp_aura` 스키마 재사용

> 참고: [`requirements.txt`](requirements.txt) — Python 의존성 목록

## 🔄 동작 원리

### 1. LangGraph 에이전트 워크플로우

```mermaid
graph TD
    Start([시작]) --> Intake[intake: 입력 정규화]
    Intake --> Planner[planner: 조사 계획 수립]
    Planner --> Execute[execute: skill/tool 실행]
    Execute --> Verify[verify: 점수·HITL 판단]
    Verify --> Finalize[finalize: 최종 결과]
    Finalize --> End([종료])
    
    Verify -->|HITL 필요| HITL[HITL 요청]
    HITL -->|응답 제출| Resume[재분석]
    Resume --> Finalize
    
    style Intake fill:#e1f5ff
    style Planner fill:#e1f5ff
    style Execute fill:#fff4e1
    style Verify fill:#e8f5e9
    style Finalize fill:#f3e5f5
    style HITL fill:#ffebee
```

> 참고: [`agent/langgraph_agent.py`](agent/langgraph_agent.py#L346-L363) — `build_agent_graph` 그래프 정의

### 2. Agent 노드 상세

| 노드 | 역할 | 출력 |
|------|------|------|
| **intake** | 전표 입력 정규화, flags 추출 | `flags`, `pending_events` |
| **planner** | 위험 신호별 조사 계획 수립 | `plan`, `pending_events` |
| **execute** | skill 순차 호출, 점수 산정 | `tool_results`, `score_breakdown`, `pending_events` |
| **verify** | HITL 필요 여부 판단 | `hitl_request` 또는 null, `verification` |
| **finalize** | 근거 기반 최종 설명 생성 | `final_result` |

### 3. Skill 선택 흐름

- **휴일/휴무**: `holiday_compliance_probe`
- **예산 초과**: `budget_risk_probe`
- **MCC/업종**: `merchant_risk_probe`
- **공통**: `document_evidence_probe` → `policy_rulebook_probe` → `legacy_aura_deep_audit` (조건부 생략 가능)

> 참고: [`agent/langgraph_agent.py`](agent/langgraph_agent.py#L126-L137) — `_plan_from_flags` 도구 선택 로직

### 4. SSE 이벤트 구조

```json
{"event_type": "NODE_START", "node": "intake", "phase": "analyze", "message": "...", "thought": "...", "action": "...", "metadata": {...}}
{"event_type": "TOOL_CALL", "node": "executor", "phase": "execute", "tool": "policy_rulebook_probe", "message": "...", "metadata": {...}}
{"event_type": "TOOL_RESULT", "node": "executor", "phase": "execute", "tool": "policy_rulebook_probe", "observation": "...", "metadata": {...}}
{"event_type": "HITL_REQUESTED", "node": "verify", "phase": "verify", "metadata": {"hitl_request": {...}}}
```

> 참고: [`agent/event_schema.py`](agent/event_schema.py) — 이벤트 페이로드 구조

## 🚀 설치 및 실행

### 1. 저장소 클론 및 의존성 설치

```bash
cd /Users/joonbinchoi/Work/AruaAgent
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

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

> 참고: [`main.py`](main.py) — FastAPI 앱, [`app.py`](app.py) — Streamlit 앱

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
# native: AruaAgent 내부 자율형 에이전트 우선
# aura: legacy aura 전용 모드
# hybrid: native + aura 병행
AGENT_RUNTIME_MODE=langgraph

# 멀티 에이전트 역할 분리
ENABLE_MULTI_AGENT=true
ENABLE_LANGGRAPH_IF_AVAILABLE=true
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

> 참고: [`.env.example`](.env.example) — 환경 변수 예시, [`utils/config.py`](utils/config.py) — 설정 로드

## 🔍 Langfuse 통합

연결 시 **테스트·분석 실행 시** LangGraph 실행과 LLM 호출을 [Langfuse 대시보드](https://cloud.langfuse.com)에서 추적·모니터링할 수 있습니다.

1. **Langfuse 계정**: https://cloud.langfuse.com 에서 계정 생성 후 프로젝트 선택
2. **키 발급**: 프로젝트 설정에서 Public Key / Secret Key 확인
3. **환경 변수**: `.env`에 `LANGFUSE_ENABLED=true`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` 설정
4. **분석 실행**: AI 워크스페이스에서 분석 실행 시 `run_id`가 세션 ID로 전달되어 대시보드에서 run별로 조회 가능
5. **비활성화**: `LANGFUSE_ENABLED=false`(기본값)이면 전송하지 않음

> 참고: [`utils/config.py`](utils/config.py) — `get_langfuse_handler`, [`agent/langgraph_agent.py`](agent/langgraph_agent.py) — astream config에 callbacks 전달

## 📡 API 엔드포인트

### 헬스 체크
- **GET** `/health` — 서버 상태 확인

### 전표 및 분석
- **GET** `/api/v1/vouchers?queue=all|pending&limit=50` — 전표 목록 조회
- **POST** `/api/v1/cases/{voucher_key}/analysis-runs` — 분석 실행 시작
- **GET** `/api/v1/analysis-runs/{run_id}/stream` — SSE 스트림 구독
- **GET** `/api/v1/cases/{voucher_key}/analysis/latest` — 최신 분석 결과
- **GET** `/api/v1/cases/{voucher_key}/analysis/history` — 케이스별 분석 이력
- **GET** `/api/v1/analysis-runs/{run_id}/events` — 런 이벤트(raw) 조회
- **POST** `/api/v1/analysis-runs/{run_id}/hitl` — HITL 응답 제출

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

> 참고: [`main.py`](main.py#L40) — 헬스 체크, [`main.py`](main.py#L54-L181) — 전표/분석 API, [`main.py`](main.py#L64-L90) — RAG/에이전트 API, [`main.py`](main.py#L407-L428) — 시연 데이터 API

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
- **Aura 연동**: `AURA_PLATFORM_PATH`가 설정된 경우에만 legacy Aura 심층 분석 도구 사용 가능

> 참고: [`db/models.py`](db/models.py) — DB 스키마, [`services/stream_runtime.py`](services/stream_runtime.py) — 메모리 기반 런타임, [`agent/aura_bridge.py`](agent/aura_bridge.py) — Aura 연동

## 🧪 다음 고도화 후보

1. DB 영속 저장 기반 run history 정교화
2. 규정/RAG 인덱스 직접 조회 툴 확장
3. specialist tool 세분화
4. 사용자/검토자 협업 히스토리 UI 고도화
5. shadow 비교 실험용 기능 재도입 (선택)

> 참고: [`services/persistence_service.py`](services/persistence_service.py) — 분석 결과 영속화, [`services/case_service.py`](services/case_service.py) — 전표/케이스 처리

## 🤝 기여

이슈 리포트 및 풀 리퀘스트를 환영합니다.

## 📄 라이선스

[라이선스 정보를 여기에 추가하세요]

---

**프로젝트**: Aura Agent AI PoC (AruaAgent)  
**목적**: 엔터프라이즈급 에이전트형 금융 감사 어시스턴트 PoC
