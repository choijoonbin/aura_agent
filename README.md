# DWP Agent Starter

새 DWP 프로젝트의 에이전트 기능을 추가하기 위한 최소 FastAPI 런타임입니다.
기존 업무 구현과 데이터베이스 모델은 포함하지 않습니다. R0 Contract Spike는
외부 Model이나 Tool을 호출하지 않는 결정적 Plan Preview만 제공합니다.

## Setup

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8010 --reload
```

- Health: `GET /health`
- Governed plan contract: `POST /v1/plans/preview`
- OpenAPI: `GET /docs`

Plan Preview는 Gateway가 검증해 전달한 `X-DWP-User-ID`, `X-DWP-Tenant-ID`,
`X-DWP-Roles`, `X-Correlation-ID`를 요구합니다. 운영 환경에서는 Agent Port를
외부에 공개하지 않고 Gateway·Backend Network에서만 접근시켜야 합니다.

전체 개발 환경은 `../dwp-backend`에서 실행합니다.

```bash
cd ../dwp-backend
./dev up full
```

## Test

```bash
.venv/bin/python -m pytest
```

현재 Preview는 항상 `mutationAllowed=false`이며 L2 Plan에는 사람 승인을 요구합니다.
Model Gateway, Retrieval, Tool 실행과 저장 구조는 실제 프로젝트 요구사항과 보안
승인이 정해진 뒤 별도 모듈로 추가합니다.
