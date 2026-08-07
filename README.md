# DWP Agent Starter

새 DWP 프로젝트의 에이전트 기능을 추가하기 위한 최소 FastAPI 런타임입니다.
기존 업무 구현과 데이터베이스 모델은 포함하지 않습니다.

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
- OpenAPI: `GET /docs`

전체 개발 환경은 `../dwp-backend`에서 실행합니다.

```bash
cd ../dwp-backend
./dev up full
```

## Test

```bash
.venv/bin/python -m pytest
```

새 에이전트 API와 저장 구조는 실제 프로젝트 요구사항이 정해진 뒤 별도
모듈로 추가합니다.
