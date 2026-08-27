# DWP Agent Control Plane

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
  uv run uvicorn --app-dir src dwp_agent.main:app --host 127.0.0.1 --port 8010 --reload
```

- Health: `GET /health`
- Governed plan contract: `POST /v1/plans/preview`
- Grounded Ask Runtime: `POST /v1/ask`
- Grounded Ask progress stream: `POST /v1/ask/stream`
- Conversation history: `GET /v1/conversations`, `GET/PATCH/DELETE /v1/conversations/{id}`
- Answer feedback: `PUT /v1/runs/{runId}/feedback`
- Proactive Agent Inbox: `GET /v1/proposals`,
  `POST /v1/proposals/{proposalId}/decisions`
- Governed proposal producer: `POST /v1/admin/proposals`
- Governed work handoff: `GET /v1/actions`, `POST /v1/actions/{actionKey}/preview`
- Operational delivery gates: `GET /v1/admin/gates`,
  `GET/PATCH /v1/admin/gates/{gateKey}`
- OpenAPI: `GET /docs`

OpenAPI 정본은 `contracts/openapi/agent-public.json`이며 다음 명령으로 런타임 계약과의
일치 여부를 검증합니다.

```bash
.venv/bin/python scripts/export_openapi.py --check
```

고객별 정책 결정과 아직 닫히지 않은 딜리버리 TODO는
`../dwp-backend/docs/delivery/customer-policy-and-release-gate-register.md`에서만 관리합니다.
기능 문서와 테스트는 해당 등록부의 `D-*`, `G-*` ID를 참조하고 별도 활성 목록을 만들지
않습니다.

Plan Preview는 Gateway가 검증해 전달한 `X-DWP-User-ID`, `X-DWP-Tenant-ID`,
`X-DWP-Roles`, `X-DWP-Permissions`, `X-DWP-Resource-Roles`, `X-Correlation-ID`와
`X-DWP-Service-Token`을 요구합니다. 앱 책임 변경 Preview는 Tenant-wide
`ADMIN.APP_GOVERNANCE:MANAGE`를 실행 권한으로 간주하지 않습니다. 명령에 포함된
전송 제외 Context인 `scopeResourceSetKey`와 Gateway의
`RESPONSIBILITY@RESOURCE_SET_KEY`를 일치시켜 `APP_OWNER`,
`APP_ACCESS_APPROVER`, `APP_ACCESS_MANAGER` 후보 자격을 사전 점검합니다. 소유자 변경과
최초 Approver Bootstrap의 `APP_CATALOG_ADMIN` 예외도 Auth 계약과 같은 후보 분기로
표시하지만, 이는 승인 결정이 아니며 응답의 `finalAuthorityService=auth`가 최종 판정자를
명시합니다. Auth가 실행 시 현재 DB 기준 Scope·유효기간·그룹
상속·SoD·Version을 다시 확인하는 최종 권한 소유자입니다.

Local 외 환경은
별도의 `DWP_AGENT_IDENTITY_SIGNING_SECRET`으로 Gateway가 서명한 단기 위임 신원도
필수입니다. 서명은 사용자·Tenant·권한·상관관계 ID·HTTP Method·Downstream Path를 묶으며
앱 책임 Preview에서는 Resource Role까지 묶습니다. Agent가 수명과 요청 일치를
재검증합니다. 로컬 실행은 `DWP_AGENT_SERVICE_TOKEN`을
설정해야 하며 운영 환경에서는 Agent Port를 외부에 공개하지 않고 Gateway·Backend
Network와 Workload Identity 또는 mTLS로 보호해야 합니다.

Workspace에서 독립 배포된 DWAI·ON으로 질문을 넘길 때는 질문 원문을 URL, Browser
History, Navigation State 또는 Web Storage에 저장하지 않습니다. Gateway 서명 신원과
`APP.ASK:VIEW`를 요구하는 `/v1/question-launches`가 Tenant·사용자·인증 Session에 묶인
60초 수명의 불투명 Ticket을 발급하고 `/consume`이 원자적으로 한 번만 소비합니다. 원문은
기존 `dwp2` Envelope로 암호화하며 만료 행 정리는
`DWP_AGENT_QUESTION_LAUNCH_CLEANUP_SECONDS`(기본 60초, 10~300초) 주기의 별도 유지보수
작업이 수행합니다.

Agent Inbox 제안은 Tenant·대상 사용자·원본 Event에 묶이며 제목, 요약, 판단 근거, Action
입력과 변경 사유를 `dwp2` Envelope로 암호화합니다. 목록 조회는 만료와 미루기 종료 상태를
투영할 뿐 DB를 변경하지 않습니다. 생성과 사용자 결정은 UUID 명령 멱등성, Revision
사전조건과 append-only `ai_agent_proposal_events` 증거를 남깁니다. 제안의 `ACCEPT`는 업무
실행이 아니며, 등록된 Action이 있더라도 기존 Action Preview와 담당 도메인 앱의 최종 확인을
다시 거쳐야 합니다.

요청의 `agentKey`는 Platform Product Registry의 Active `AGENT` Revision으로 해석되고
해석 결과가 `planHash`와 감사 Event에 포함됩니다. 현재 읽기 전용 Reference Preview는
기본 `DWP_AGENT_REGISTRY_MODE=optional`로 Registry가 비어 있을 때 명시적인
`REFERENCE_FALLBACK`을 사용합니다. 실제 Tool 실행 단계는 아래처럼 Fail Closed로
전환해야 합니다.

```bash
SERVICE_PLATFORM_URL=http://localhost:8002
DWP_PLATFORM_RUNTIME_SERVICE_TOKEN=<managed-runtime-read-secret>
SERVICE_APPROVAL_URL=http://localhost:8005
DWP_APPROVAL_RUNTIME_SERVICE_TOKEN=<managed-approval-runtime-read-secret>
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
항상 L3로 분류됩니다. 허용 명령은 `src/dwp_agent/admin_commands.py`의 버전형 카탈로그에 등록되며,
명령별 대상 유형·파라미터 스키마·필수 권한·담당 서비스·HTTP 계약이 일치하지 않으면
요청 단계에서 Fail Closed 됩니다. 현재 카탈로그는 Access, Navigation, HRIS, SCIM,
Provider Tenant 작업만 허용합니다. 해석한 카탈로그 Revision과 서비스 계약은
`planHash`, 응답, 구조화 감사 Event에 포함됩니다.

Ask Runtime은 서버에서 `APP.ASK:VIEW` 권한과 위험도를 판정하고, 사용자의
`APP.WORK:VIEW`, `APP.MAIL:VIEW` 범위로 Platform의 읽기 API만 호출합니다.
컨텍스트 안의 명령은 신뢰하지 않으며 모델이 반환한 Citation ID가 실제 조회한 Source
집합에 속하는지 다시 검증합니다. 출처가 없거나 Citation이 잘못되면 답변을 보류합니다.
Source·Action·Safety 정책과 Operational Gate는 일반 조회 과정에서 생성하지 않습니다.
초기화되지 않은 Tenant는 Fail Closed하고, 권한·기대 기존 건수·멱등 키·사유·감사를 갖춘
명시적 Bootstrap Endpoint만 초기 구성을 생성합니다. Local 외 환경에서는 Ask·Action 실행
직전에 Tenant·환경별 필수 Gate가 모두 승인됐는지도 다시 검사합니다.

Ask의 `requestId`는 2분 실행 임대로 보호됩니다. 활성 임대의 중복 요청은 새 실행을 만들지
않고, 만료된 중단 실행만 같은 원장 행에서 원자적으로 회수합니다. Model 재시도는 하나의
합산 Deadline(기본 20초, 상한 24초)을 공유하고 Stream은 전체 40초, 기본 Worker 8개와
대기 32개로 제한됩니다. Gateway의 Agent Runtime 제한은 45초입니다.

메일 기반 AI는 `MailActionProposal` 계약으로만 회신 초안, 회의 일정, 휴가 신청, 업무,
긴급 알림을 제안합니다. 모든 제안은 메시지 근거 Hash, 신뢰도, 위험도, 대상 앱의 리소스와
필수 권한을 포함하며 `humanConfirmationRequired=true`,
`automaticExecutionAllowed=false`가 강제됩니다. 제안을 수락해도 Calendar, HCM, Work 등
대상 앱에서 현재 권한과 최종 입력을 다시 검증하기 전에는 업무 데이터가 변경되지 않습니다.
계약 버전 1과 액션별 필수 Payload는 Agent와 Platform이 독립적으로 검증하므로 미지원 버전,
권한 혼동, 불완전한 제안은 사용자에게 노출되기 전에 거부됩니다.
DWAI·ON Action Shelf에서 담당 앱으로 넘기는 Preview는 서버가 검증한 Run·Request·
Correlation·Conversation 출처를 `planHash`와 감사 Event에 포함합니다. Frontend는 서버가
반환한 Handoff v2만 전달하며 누락되거나 변조된 출처와 이전 v1 Payload를 거부합니다.
질문 원문은 URL·Browser History·Navigation State·Web Storage에 직렬화하지 않습니다.
패널과 전역 검색에서 전체 화면으로 이동할 때는 60초 이내 한 번만 소비되는 메모리
Handoff를 사용하고, 이후 복원 URL에는 서버가 발급한 불투명 Conversation ID만 둡니다.

실행 이력과 사용자 소유 Conversation은 전용 `dwp_agent` 데이터베이스에 저장합니다.
실행 원장의 질문은 Keyed HMAC만, Citation은 Hash만 저장하며 재시도 응답과 대화의 제목,
질문, 답변, 인용, 선택적 피드백 의견은 매 쓰기마다 새로 생성한 Data Key로
AES-256-GCM 암호화하고, Data Key는 `KeyProvider`의 KEK로 Wrap합니다.
대화는 Tenant별 `ai_conversation_retention_policies`에 따라 기본 90일 보존 후 정리됩니다.
Legal Hold가 활성화되면 만료 정리와 사용자 삭제가 모두 차단됩니다. Migration `V13`부터
새 보호 데이터는 매 쓰기마다 생성한 32-byte DEK와 Canonical AAD를 사용하는 `dwp2`
Envelope로 기록합니다. 기존 Non-envelope 열은 명시적인 Legacy Read 경로에서만 읽으며
새 쓰기에는 사용하지 않습니다. `dev/qa/prod`는 관리형 `KeyProvider`와 불변 Key Reference가
필수이고, 현재 관리형 어댑터가 없으므로 시작을 실패 차단합니다. Legacy 암호문에 필요한
이전 Key Version은 재암호화 또는 보존 만료가 증명될 때까지 유지해야 합니다.

Model Route는 OpenAI-compatible Responses API의 Structured Outputs를 사용하고 `store=false`,
출력 Token 상한, Privacy-preserving Safety Identifier를 적용합니다. 설정 예시는 다음과
같으며 Key가 없으면 성공 응답을 꾸미지 않고 `CONFIGURATION_REQUIRED`로 반환합니다.
로컬 개발만 `local-inline` 또는 `local-file` Provider를 사용할 수 있습니다. 평문 Key는
Git에서 제외된 `.env.local`에 두거나 `.dev-runtime/keys` 아래 `chmod 600` 파일로 관리합니다.
파일 Provider는 지정 Root 밖의 경로와 과도한 권한을 거부합니다. Backend `./dev`
Supervisor는 Agent에만 명시적인 로컬 Provider를 주입하며 공유 환경에서는 이 예외를
사용할 수 없습니다.

```bash
DWP_MODEL_PROVIDER=azure_openai
AZURE_OPENAI_API_KEY=<managed-secret>
AZURE_OPENAI_ENDPOINT=https://<resource-name>.openai.azure.com
DWP_OPENAI_MODEL=<azure-deployment-name>
```

Azure 리소스 루트는 런타임에서 GA v1 경로인 `/openai/v1`으로 정규화되며 요청은
`/responses`와 `api-key` Header를 사용합니다. Public OpenAI는
`DWP_MODEL_PROVIDER=openai`, `OPENAI_API_KEY`,
`DWP_OPENAI_BASE_URL=https://api.openai.com/v1` 조합을 사용합니다. 운영에서는 API Key를
소스나 이미지에 포함하지 않고 Secret Store에서 주입해야 합니다.

현재 단계에서는 권한·테넌트 범위·직무분리·버전 충돌을 검사하는 Preview와 사람 승인
단계만 만들고 실제 변경 Endpoint를 호출하지 않습니다.
향후 실행기는 승인된 `planHash`와 Typed Command만 받아 Backend API를 호출하며,
Agent가 데이터베이스를 직접 변경하는 방식은 허용하지 않습니다.

관리 작업은 승인된 `planHash`와 Typed Command를 별도 실행기가 검증한 뒤 Backend
API로만 수행해야 하며 Agent가 업무 데이터베이스를 직접 변경하는 방식은 허용하지
않습니다.
