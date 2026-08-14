# DWP Agent Starter

새 DWP 프로젝트의 에이전트 기능을 추가하기 위한 최소 FastAPI 런타임입니다.
기존 업무 시스템의 원장을 직접 소유하지 않습니다. 결정적 Plan Preview와 함께,
권한 범위의 워크스페이스 근거만 사용하는 읽기 전용 Ask Runtime을 제공합니다.

## Setup

```bash
uv sync --frozen
```

`pyproject.toml`과 `uv.lock`이 의존성의 단일 원장입니다. 기본 개발 런타임은
Python 3.14이며 전환 기간에는 Python 3.11~3.14를 지원합니다.

## Run

```bash
DWP_AGENT_SERVICE_TOKEN=<local-secret> \
  uv run uvicorn main:app --host 127.0.0.1 --port 8010 --reload
```

- Health: `GET /health`
- Governed plan contract: `POST /v1/plans/preview`
- Grounded Ask Runtime: `POST /v1/ask`
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
uv run pytest
```

Preview는 항상 `mutationAllowed=false`이며 L2 Plan에는 사람 승인을 요구합니다.
응답의 `planHash`는 사용자·역할·요청·Agent Registry Revision을 결합한 SHA-256이고,
구조화 감사 Event에는
질문 원문·Source ID·Service Token을 기록하지 않습니다.

관리자 자동화는 요청의 선택적 `adminChange` 계약으로만 표현합니다. 이 계약은
`commandKey`, 대상 종류·ID, `expectedVersion`, 구조화 파라미터와 사유를 요구하며
항상 L3로 분류됩니다. 허용 명령은 `admin_commands.py`의 버전형 카탈로그에 등록되며,
명령별 대상 유형·파라미터 스키마·필수 권한·담당 서비스·HTTP 계약이 일치하지 않으면
요청 단계에서 Fail Closed 됩니다. 현재 카탈로그는 Access, Navigation, HRIS, SCIM,
Provider Tenant 작업만 허용합니다. 해석한 카탈로그 Revision과 서비스 계약은
`planHash`, 응답, 구조화 감사 Event에 포함됩니다.

Ask Runtime은 서버에서 `APP.ASK:VIEW` 권한과 위험도를 판정하고, 사용자의
`APP.WORK:VIEW`, `APP.MAIL_CALENDAR:VIEW` 범위로 Platform의 읽기 API만 호출합니다.
컨텍스트 안의 명령은 신뢰하지 않으며 모델이 반환한 Citation ID가 실제 조회한 Source
집합에 속하는지 다시 검증합니다. 출처가 없거나 Citation이 잘못되면 답변을 보류합니다.

실행 이력은 전용 `dwp_agent` 데이터베이스에 저장합니다. 질문은 Keyed HMAC만,
Citation은 Hash만 저장하며, 재시도용 응답은 `DWP_AGENT_DATA_KEY`로 AES-256-GCM
암호화합니다. 질문·답변·출처 제목은 평문 이력과 감사 이벤트에 기록하지 않습니다.
운영에서는 이 키를 KMS/Secret Manager로 주입하고 정기 회전해야 합니다.

Model Route는 OpenAI Responses API의 Structured Outputs를 사용하고 `store=false`,
출력 Token 상한, Privacy-preserving Safety Identifier를 적용합니다. 설정 예시는 다음과
같으며 Key가 없으면 성공 응답을 꾸미지 않고 `CONFIGURATION_REQUIRED`로 반환합니다.

```bash
OPENAI_API_KEY=<managed-secret>
DWP_OPENAI_MODEL=<approved-model-snapshot>
DWP_OPENAI_BASE_URL=https://api.openai.com/v1
```

현재 단계에서는 권한·테넌트 범위·직무분리·버전 충돌을 검사하는 Preview와 사람 승인
단계만 만들고 실제 변경 Endpoint를 호출하지 않습니다.
향후 실행기는 승인된 `planHash`와 Typed Command만 받아 Backend API를 호출하며,
Agent가 데이터베이스를 직접 변경하는 방식은 허용하지 않습니다.

관리 작업은 승인된 `planHash`와 Typed Command를 별도 실행기가 검증한 뒤 Backend
API로만 수행해야 하며 Agent가 업무 데이터베이스를 직접 변경하는 방식은 허용하지
않습니다.
