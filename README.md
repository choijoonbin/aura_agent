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
DWP_AGENT_SERVICE_TOKEN=<local-secret> \
  .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8010 --reload
```

- Health: `GET /health`
- Governed plan contract: `POST /v1/plans/preview`
- OpenAPI: `GET /docs`

Plan Preview는 Gateway가 검증해 전달한 `X-DWP-User-ID`, `X-DWP-Tenant-ID`,
`X-DWP-Roles`, `X-Correlation-ID`와 `X-DWP-Service-Token`을 요구합니다. 로컬 실행은
`DWP_AGENT_SERVICE_TOKEN`을 설정해야 하며 운영 환경에서는 Agent Port를 외부에
공개하지 않고 Gateway·Backend Network와 Workload Identity 또는 mTLS로 보호해야 합니다.

요청의 `agentKey`는 Platform Product Registry의 Active `AGENT` Revision으로 해석되고
해석 결과가 `planHash`와 감사 Event에 포함됩니다. 현재 읽기 전용 Reference Preview는
기본 `DWP_AGENT_REGISTRY_MODE=optional`로 Registry가 비어 있을 때 명시적인
`REFERENCE_FALLBACK`을 사용합니다. 실제 Tool 실행 단계는 아래처럼 Fail Closed로
전환해야 합니다.

```bash
SERVICE_PLATFORM_URL=http://localhost:8002
DWP_PLATFORM_RUNTIME_SERVICE_TOKEN=<managed-runtime-read-secret>
DWP_AGENT_REGISTRY_MODE=enforced
```

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
응답의 `planHash`는 사용자·역할·요청·Agent Registry Revision을 결합한 SHA-256이고,
구조화 감사 Event에는
질문 원문·Source ID·Service Token을 기록하지 않습니다.

관리자 자동화는 요청의 선택적 `adminChange` 계약으로만 표현합니다. 이 계약은
`commandKey`, 대상 종류·ID, `expectedVersion`, 구조화 파라미터와 사유를 요구하며
항상 L3로 분류됩니다. 현재 단계에서는 권한·테넌트 범위·직무분리·버전 충돌을
검사하는 Preview와 사람 승인 단계만 만들고 실제 변경 Endpoint는 제공하지 않습니다.
향후 실행기는 승인된 `planHash`와 Typed Command만 받아 Backend API를 호출하며,
Agent가 데이터베이스를 직접 변경하는 방식은 허용하지 않습니다.

Model Gateway, Retrieval, Tool 실행과 저장 구조는 실제 프로젝트 요구사항과 보안
승인이 정해진 뒤 별도 모듈로 추가합니다.
