# DWAI·ON Production Gate

DWAI·ON은 읽기 전용 근거 탐색과 담당 앱 초안 전달까지만 수행합니다. 이 문서는 운영
승인 전에 반복 실행할 최소 보안·품질 게이트와 데이터 운영 절차를 정의합니다.

## 1. 필수 실행 설정

- Agent와 Gateway의 `DWP_AGENT_SERVICE_TOKEN`은 동일한 관리형 Secret을 사용합니다.
- `DWP_AGENT_IDENTITY_SIGNING_SECRET`은 Service Token·KMS Key와 분리된 관리형 Secret으로
  주입합니다. Gateway는 검증된 사용자·Tenant·권한·상관관계 ID와 HTTP Method·Agent
  Downstream Path를 최대 15초 수명의 서명 Assertion으로 묶고, Agent는 서명·수명·요청
  일치를 모두 검증합니다. 이 검증이 활성화되면 개별 `X-DWP-*` Header만 신뢰하지 않습니다.
- Platform 읽기에는 `DWP_PLATFORM_RUNTIME_SERVICE_TOKEN`, Approval 읽기에는 별도의
  `DWP_APPROVAL_RUNTIME_SERVICE_TOKEN`을 사용합니다. Gateway Token을 재사용하지 않습니다.
- Calendar 근거는 `SERVICE_GATEWAY_URL`의 public route를 통해 현재 사용자 Session 권위를
  다시 평가합니다. Agent는 해당 자격 증명을 저장·로그하지 않고 Platform service token으로
  우회하지 않으며, support mode 또는 401/403/409/503에서는 Calendar Source를 실패 차단합니다.
- `DWP_AGENT_REGISTRY_MODE=enforced`로 실행하고 승인된 Agent Revision이 없으면
  실패 차단합니다.
- `dev/qa/prod`는 관리형 `KeyProvider`와 불변 `DWP_AGENT_KEY_REFERENCE`를 사용해야 합니다.
  Azure Key Vault·AWS KMS·Google Cloud KMS용 실제 어댑터와 Workload Identity는 중앙
  Delivery Register의 `G-11` 외부 Gate이며, 어댑터가 없는 현재 상태에서는 시작을 실패
  차단합니다.
- 새 보호 데이터는 매 쓰기마다 32-byte DEK를 생성하고 `dwp2` Envelope로 저장합니다.
  `DWP_AGENT_DATA_KEY_VERSION`과 이전 Key Map은 KEK 회전 및 Legacy Read 용도입니다.
  평문 `DWP_AGENT_DATA_KEY`와 `DWP_AGENT_PREVIOUS_DATA_KEYS`는 로컬 개발 전용입니다.
- 로컬은 `local-inline` 또는 Root 경계·`0600` 권한을 검사하는 `local-file` Provider만
  허용합니다. 로컬 설정 파일은 Git과 배포 Artifact에서 제외합니다.
- 승인된 모델 Snapshot과 관리형 API Key를 사용합니다. 모델 설정이 없으면
  `CONFIGURATION_REQUIRED`가 정상 결과이며 대체 답변을 생성하지 않습니다.
- `DWP_ENVIRONMENT=production`에서는 위 설정과 전용 DB, 분리된 Service Token,
  감사/API 이력 수집 설정이 누락되거나 서로 재사용되면 프로세스가 시작되지 않습니다.
- Gateway의 `agentRuntime` 제한 시간은 Agent의 전체 실행 예산보다 길어야 합니다. 기본
  계약은 모델 재시도 합산 20초(설정 상한 24초), Slow-call 30초, Stream 전체 40초,
  Gateway 45초입니다. Stream Worker는 기본 동시 8개·대기 32개로 제한하고 포화 시
  `429`와 `Retry-After`를 반환합니다. 일반 업무 API의 10초 제한과 분리합니다.

## 2. 릴리스 차단 테스트

```bash
uv sync --frozen
uv run pytest
uv run python -m compileall -q src
uv run python scripts/export_openapi.py --check
```

다음 항목 중 하나라도 실패하면 배포하지 않습니다.

1. 권한·Tenant·요청 Scope가 Context 조회 전에 거부되는지 확인합니다.
2. 프롬프트 인젝션과 제한 분류 Source가 모델 Evidence에 포함되지 않는지 확인합니다.
3. 모델이 조회되지 않은 Citation을 반환하거나 근거가 없을 때 답변을 보류하는지
   확인합니다.
4. 모든 변경 제안이 `automaticExecutionAllowed=false`이고 담당 앱에서 최종 저장을
   다시 요구하는지 확인합니다.
5. Model 호출이 Structured Output, `store=false`, 출력 Token 상한을 유지하는지
   확인합니다.
6. 공유 환경에서 관리형 Key Provider·참조·과거 Key가 누락되면 평문이나 빈 값으로
   대체하지 않고 시작 또는 복호화를 실패 차단하는지 확인합니다.
7. 동일 평가 세트의 최신 실행과 직전 실행을 비교하고, 통과율 또는 사례별 회귀가 있으면
   릴리스 승인자가 결과를 검토하는지 확인합니다.
8. Source·Action·Safety·Operational Gate의 조회가 기본값을 쓰지 않고, 초기화되지 않은
   Tenant를 실패 차단하는지 확인합니다. 초기화는 별도 Bootstrap 명령의 권한, 기대 기존
   건수, 멱등 키, 사유와 감사 Event를 모두 요구합니다.
9. Local 외 환경의 Ask·Action 요청이 Tenant·환경별 필수 Gate가 모두 `APPROVED`일 때만
   실행되고, 선택 옵션도 해당 환경에서 배포 가능한지 다시 확인하며 Gate Schema가 없으면
   Startup이 실패하는지 확인합니다. `BLOCKED`, `LOCAL_DEVELOPMENT_KEY`와 다른 개발 전용
   옵션은 승인 상태만으로 Shared·운영 실행을 허용하지 않습니다.
10. 동일 Ask `requestId`가 실행 중이면 중복 실행되지 않고, 2분 임대가 만료된 경우에만
    같은 원장 행을 원자적으로 회수하는지 확인합니다. 회수할 때마다 증가하는 Lease
    Generation을 완료·실패·대화 메시지에 함께 검증해, 임대를 잃은 실행이 새 실행의 상태나
    대화 내용을 기록·노출하지 못하는지도 확인합니다.
11. Action Preview가 서버가 검증한 DWAI·ON Run·Request·Correlation·Conversation 출처를
    `planHash`와 감사 Event에 묶고, Browser Handoff v2가 누락·변조된 출처를 거부하는지
    확인합니다.

운영 모델 또는 Prompt가 바뀌면 고정 회귀 세트 외에 승인된 평가 환경에서 간접
인젝션, 데이터 유출, Citation 정확도, 무응답 적합성, 한국어·영어 동등성을 다시
측정하고 결과를 릴리스 증적으로 보관합니다.

## 3. 운영 Control Plane

- `/v1/admin/gates`는 고객·환경별 모델, 연결, ACL, 평가, 승인, KMS, 보존과 감사 준비
  상태를 관리한다. 조회·증빙·변경·승인을 `ADMIN.DWAION_GATES`의 동작별 권한으로
  분리하고, 구성자 또는 검증자는 같은 Gate를 승인할 수 없다.
- Gate 상세는 현재 구성 리비전의 전체 증빙, 필수 증빙 누락, 요청자별 독립 승인 자격,
  성공한 변경·검증·승인 감사 타임라인을 함께 반환한다. 승인자는 이 근거를 확인한 동일
  화면에서만 결정을 기록하며, 새 증빙을 추가하면 이전 검증이 무효화된다.
- Gate 오류는 RFC 9457 Problem Details 형태의 안정된 코드, 교정 가능한 설명,
  상관관계 ID를 반환한다. 비밀 원문이나 내부 예외 Stack은 반환하지 않는다.
- 활성 TODO, 고객 결정과 종료 증거의 정본은 Backend
  `docs/delivery/customer-policy-and-release-gate-register.md`다. 이 문서에는 중복 TODO
  목록을 두지 않는다.

- `/v1/admin/overview`는 `ADMIN.DWAION_OPERATIONS:VIEW`가 있는 Gateway 호출만
  허용합니다. 실행 수, 정책 판정, 답변 상태, 지연, Token, 활성 사용자 수, 대화 수와
  피드백의 Tenant 집계만 반환합니다.
- `/v1/admin/sources`, `/actions`, `/safety`는 각각
  `ADMIN.DWAION_SOURCES`, `ADMIN.DWAION_ACTIONS`, `ADMIN.DWAION_SAFETY` 권한을
  독립적으로 요구합니다. 조회와 변경 권한을 합치지 않으며 모든 변경은 버전·사유·상관관계
  ID를 검증합니다.
- `/v1/admin/sources/bootstrap`, `/actions/bootstrap`, `/safety/bootstrap`,
  `/v1/admin/gates/bootstrap` 및 `/v1/admin/retention/bootstrap`만 초기 구성을 생성할 수
  있습니다. 일반 조회는 데이터가 없다는 사실을 그대로 반환하거나 `NOT_INITIALIZED`로
  실패하며, Write-on-read를 허용하지 않습니다. Bootstrap은 Tenant Advisory Lock,
  기대 기존 건수, 멱등 키, 변경 사유와 감사 Event를 요구합니다.
- 평가셋 작성·상태 전이·실행은 `ADMIN.DWAION_EVALUATION`의
  `CREATE|UPDATE|EXECUTE|MANAGE`를 동작별로 요구합니다. 평가 질문과 기대 결과는 대화와
  동일한 Data Key 계약으로 암호화합니다. 실행 이력은 모델 참조, 사례별 판정, 근거 여부,
  기대 용어 충족 수와 지연 시간만 기록하며 질문이나 응답 원문을 결과 증적에 복제하지 않습니다.
- 실행 이력·상세 조회는 `VIEW`, 메트릭 전용 CSV 내보내기는 `EXPORT`를 요구합니다.
  동일 세트에는 하나의 실행만 허용하며 30분 실행 임대가 만료된 중단 실행은 `FAILED`로
  회수한 뒤 새 실행을 허용합니다. 임대를 잃은 이전 실행의 지연 결과는 저장하지 않습니다.
- 거버넌스 감사 조회·내보내기는 `ADMIN.DWAION_AUDIT:VIEW|EXPORT`로 분리하며,
  변경 권한을 가진 운영자에게 감사 삭제나 수정 경로를 제공하지 않습니다.
- 운영 조회는 질문, 답변, 대화 제목, 인용 원문이나 사용자 식별자를 선택하거나
  복호화하지 않습니다. 집계 쿼리에 개인 콘텐츠 컬럼을 추가하지 않습니다.
- `/v1/admin/retention` 조회는 `ADMIN.DWAION_RETENTION:VIEW`, 변경은 `UPDATE` 또는
  `MANAGE`가 필요합니다. 최초 정책 생성은 `MANAGE`와 명시적 Bootstrap 계약을 요구하며
  일반 GET·Overview·PATCH는 누락된 정책을 생성하지 않습니다.
  Legal Hold 필드를 포함한 변경은 `MANAGE`만 허용합니다.
- 변경 요청은 현재 `policyVersion`, 10~500자의 사유와 `X-Correlation-ID`를 요구합니다.
  버전 충돌은 `409`로 실패하며 묵시적으로 덮어쓰지 않습니다.
- `ai_retention_policy_events`는 이전 값, 새 값, 수행자, 상관관계 ID, 사유를 append-only로
  기록합니다. 운영 API로 이 이력을 수정하거나 삭제하는 경로를 제공하지 않습니다.

## 4. 실행 경계와 복구

- Local 외 환경의 Ask와 Action은 화면의 `deliveryReady` 표시와 별개로 서버 실행 경계에서
  필수 Gate를 다시 검사합니다. UI 상태나 캐시는 권한 또는 승인 근거가 아닙니다.
- Ask의 `requestId`는 Tenant·사용자·질문 Fingerprint에 결합됩니다. 같은 식별자의 다른
  질문은 충돌로 거부하고, 실행 중 재요청은 `in progress`, 완료된 재요청은 기존 결과를
  반환합니다. 2분 임대가 만료된 중단 실행만 같은 행에서 원자적으로 회수하며, 회수 세대보다
  오래된 Worker의 완료·실패는 거부합니다. 대화 메시지는 현재 Lease Generation과 함께
  Pending 상태로 저장하고 동일 트랜잭션에서 Run 완료가 확정된 뒤에만 조회·집계에 노출합니다.
- Frontend는 질문 원문을 URL, Browser History, Navigation State나 Web Storage에 넣지
  않습니다. 독립 배포 앱 간 전달은 Tenant·사용자·인증 Session에 결합되고 암호화 저장되는
  60초 수명의 서버측 불투명 Ticket을 원자적으로 한 번만 소비합니다. 서버가 대화를 생성한
  뒤에는 불투명 Conversation ID만 URL에 둡니다. Legacy `q`는 자동 제출하지 않으며 신규
  링크 생성을 금지하고 Edge 접근 로그에는 Query String을 기록하지 않습니다.
- 질문 Ticket 생성·소비는 `TENANT` Identity Plane, `APP.ASK:VIEW`, Gateway Service Token과
  서명된 `X-DWP-Auth-Session-ID`를 모두 요구합니다. 사용자·Session별 동시 8개 및 분당
  20회 한도를 적용하고, 불일치·만료·재사용은 동일한 비노출 응답으로 처리합니다. 만료
  정리는 조회 요청에 쓰기를 섞지 않고 전용 유지보수 주기로 수행합니다.
- Stream은 제한된 Worker Pool과 Queue만 사용합니다. 전체 40초 예산을 넘거나 Worker가
  포화되면 연결을 무기한 유지하지 않고 안정된 오류 코드로 종료합니다.
- Action Handoff는 `APP.ASK`의 실제 `action-shelf`에서 발생한 Run·Request·Correlation과
  허용 Route를 요구합니다. 서버가 검증하고 반환한 출처만 Handoff v2에 저장하며 Client가
  임의 생성한 출처나 v1 Payload는 거부합니다.
- Agent는 업무 원장을 직접 변경하지 않습니다. Preview의 권한·Version·`planHash`를 담당
  Backend가 최종 확인하고 사용자가 저장을 승인한 뒤에만 업무 API가 변경을 수행합니다.
- Agent Inbox는 TENANT 관리 생산자가 명시적으로 생성하거나, 사용자가 본인 권한 범위에서
  `POST /v1/proposals/analyze`를 명시 호출한 제안만 대상 사용자에게 노출합니다. 백그라운드
  분석과 자동 실행은 없습니다. 제안 본문과 감사 사유는 Envelope 암호화하고, Source Event
  중복·명령 멱등성·Revision 사전조건·append-only 결정 증거를 강제합니다. GET은 만료와
  미루기 종료를 투영할 뿐 어떤 행도 생성·갱신하지 않으며 `ACCEPT`는 자동 실행 권한으로
  해석하지 않습니다.
- 생성·결정 명령의 재시도 지문은 평문 Hash가 아니라 Data Key에서 Tenant와
  `agent-proposal` Purpose를 분리해 파생한 HMAC-SHA-256을 사용합니다. 동일 Command와
  Source Event는 PostgreSQL Transaction Advisory Lock으로 직렬화하고, 사유·Revision·Note·
  Snooze 시각을 포함한 전체 Payload가 달라지면 기존 성공을 재사용하지 않습니다.
- 사용자 분석 명령은 인증 Session과 Locale Payload를 HMAC으로 결속하고, 세대별 Lease와
  암호화된 Canonical Receipt로 동시 요청·응답 유실·재시도를 직렬화합니다. Source Event
  식별자는 Locale과 분리하며, Inbox Clear는 완료 Receipt와 활성 Lease를 함께 Fencing합니다.
- 개인 선호의 암호화 저장 동의와 답변 적용 동의는 서로 다른 명시적 명령입니다. 기존 사용자는
  답변 적용이 `UNSET`으로 유지되며 저장을 껐다가 다시 켜도 답변 적용은 자동 복원되지 않습니다.
  `DWP_ASSISTANT`는 현재 사용자에게 `APP.DWAION_MEMORY:VIEW`가 있고 두 동의가 모두 켜진 경우에만
  활성·미만료 선호를 유형별 최신 한 건씩 읽습니다. 선호는 비신뢰 표현 데이터로만 모델에 전달하며
  사실·근거·권한·정책·안전 경계를 변경할 수 없습니다. 개인화 저장소 장애는 본 답변을 실패시키지
  않고 `UNAVAILABLE`, 근거 기반 대체 답변은 `BYPASSED`로 공개하되 선호 원문은 응답하지 않습니다.
- 선택 업무 도움은 Frontend가 전달한 업무 본문을 신뢰하지 않고 담당 Owner API에서 Tenant·사용자·
  인증 Session·Source ID·Source Version·권한 Obligation을 다시 조회합니다. 모델 호출 전후에 동일한
  Authority Snapshot을 확인하며, 처리 중 권한 회수·사용자 전환·Version 변경·원천 `403/404/409/503`이
  발생하면 늦게 도착한 답변을 게시하지 않습니다. Stream의 `requestId`·`correlationId`·
  `conversationId`와 선택 업무 Context가 정확히 결속된 경우에만 같은 대화를 이어가며, Agent는
  업무 Form을 자동 적용·저장·제출하거나 Owner 원장을 변경하지 않습니다.
- Artifact 내보내기는 `DWP_GOVERNED_WORKERS_ENABLED=true`가 명시된 Agent 인스턴스의
  fresh worker heartbeat가 있을 때만 실행 capability를 공개합니다. 요청은 outbox와 export job의
  독립 세대·임대에 결속되고, Worker는 게시된 불변 Version·DLP Preflight·Source 증거를 다시
  검증한 뒤 `MARKDOWN`, `DOCX`, `PDF` 실제 바이트와 Manifest를 PostgreSQL에 Envelope 암호화해
  저장합니다. Download는 Tenant·사용자·Artifact·Job 소유권과 keyed content fingerprint를 다시
  검증합니다. `sourceVerificationAvailable`의 범위는 서버가 직접 대화·Assistant message·Citation을
  결속한 `SERVER_BOUND_CONVERSATION_CITATIONS_ONLY`이며, 사용자가 입력한 Source Reference를
  `VERIFIED`로 승격하지 않습니다. 조직 Enterprise DLP Connector는 별도 외부 Gate입니다.
- 개인 데이터 삭제 Worker도 같은 명시적 설정과 fresh heartbeat가 있을 때만 실행 capability를
  공개합니다. Request 시점과 각 Domain 처리 직전에 Legal Hold를 다시 확인하고, Outbox·Job의
  Fenced Lease를 모두 소유한 경우에만 Agent 활성 PostgreSQL의 `ROUTINE`, `MEMORY`, `ARTIFACT`,
  `ARTIFACT_EXPORT` 행과 암호화 Envelope를 제거합니다. 완료 증거는 Domain별 행 수, keyed receipt
  fingerprint와 `EXTERNAL_RETENTION_BOUNDARY`를 기록합니다. 이 영수증은 Agent 활성 저장소의
  물리 행 제거 증거이며 Owner 원천 시스템 또는 Database Backup의 물리 삭제 증거가 아닙니다.
  따라서 heartbeat가 살아 있을 때 `activeStorePhysicalPurgeAvailable`만 `true`이고, 사용자별
  별도 암호화 키를 폐기하는 방식이 아니므로 `activeStoreCryptoShredAvailable`은 계속 `false`입니다.
- Routine의 일정 계산과 Dry Run 검증은 실제 Background 실행 증거가 아닙니다. Background에서
  사용자 권위를 재평가할 위임 자격, Source Connector, 제안 전달 및 알림 Delivery가 운영 승인되기
  전에는 `activationAvailable`, `schedulingAvailable`, `backgroundExecutionAvailable`,
  `proposalDeliveryAvailable`, `notificationDeliveryAvailable`을 계속 `false`로 유지합니다.

## 5. 보존 및 Legal Hold

### 공통 활동의 DWAI·ON 원천 조회 경계

- `/v1/activity/events`, `/v1/activity/events/{event_id}` 및
  `/v1/activity/executions/summary`는 기존 Gateway 서명 Identity 경계를 사용하는
  읽기 전용 원천 API입니다. `TENANT` Plane에서 `APP.ACTIVITY:VIEW`와 `APP.ASK:VIEW`를
  모두 요구하고 Provider Role·Support Session·Support Access Mode를 거부합니다.
  Platform이 Agent 원장을 직접 조회하거나 별도 Service Token으로 사용자 권한을 대체하지 않습니다.
- 이 API는 Product Authorization v5의 정확 Gateway/Service Route Contract로 등록되어
  있습니다. Activity 읽기 Route는 DWAI·ON 제품 권한 `APP.ASK:VIEW`와 별도
  `APP.ACTIVITY:VIEW` capability를 모두 요구합니다. `110`/`111`에서는 Gateway가 발급한
  정확 Route·Context·SELF Scope·현재 Decision 증거가 하나라도 없거나 다른 Route의
  증거가 재사용되면 `503`/`403`으로 실패 차단합니다. `000`/`100`에서도 기존
  Tenant·Owner·Source 권한 검증은 유지됩니다.
- 기존 v4 projection은 최초 3개 DWAI.ON Route만 포함한 채 byte-immutable하게 유지됩니다.
  신규 5개 읽기 Route는 `DWP_AGENT_PRODUCT_AUTHORIZATION_V5_ENABLED`가 명시적으로
  준비된 경우에만 `110`/`111` enforcement를 통과하며, v4 readiness만으로는 열리지 않습니다.
- v6에는 실제 사용자 변경 경로인 `POST /v1/ask/stream`,
  `PATCH /v1/conversations/{conversation_id}`, `DELETE /v1/conversations/{conversation_id}`가
  각각 독립 ACTION Route로 등록됩니다. `110`/`111`에서는 Gateway가 평가한 정확 Route·Context·
  SELF Scope와 브라우저가 보낸 `X-DWP-Expected-Decision-Revision`이 현재 Decision Revision과
  일치해야 하며, Agent owner PEP가 동일 증거를 다시 검사한 뒤에만 runtime/store를 호출합니다.
  세 경로는 `DWP_AGENT_PRODUCT_AUTHORIZATION_V6_ENABLED`가 명시적으로 준비되어야 열리고,
  v4/v5 readiness만으로 신규 v6 ACTION을 열 수 없습니다.
- `/v1/runs`와 `/v1/runs/{run_id}`도 동일한 SELF Route Contract 아래 등록됩니다.
  단건 조회는 최신 목록 한도와 독립적이며, Tenant와 사용자 소유권이 맞지 않거나 삭제된
  실행은 동일한 `404`로 처리하고 질문·답변·인용 원문을 반환하지 않습니다.
- 자료는 불변 이벤트 이력이 아니라 `ai_agent_runs`의 **현재 실행 Snapshot**입니다.
  `coverage.semantics=CURRENT_EXECUTION_SNAPSHOTS`, `sourceScope=DWAI_ON`을 명시하며
  과거 Attempt별 전이 이력·Outbox·외부 업무 처리·자동 Agent 실행은 제공하지 않습니다.
  `occurredAt`은 최초 실행 생성 시각이고 `updatedAt`은 알려진 완료 시각이며,
  `sourceObservedAt`은 해당 현재 상태를 조회한 시각입니다. 실행 중 재시도 시작 시각을
  추측해 생성하지 않습니다.
- `executionId`와 Event ID는 안정된 원장 Run UUID입니다. `attempt`는 Fenced Lease
  Generation이며 `executionVersion=2*max(1,generation)+terminalBit`는 중복·지연 응답의
  역행을 막는 원천 버전입니다. terminalBit는 저장된 `run_state != RUNNING`만 반영하며
  시계 경과나 조회만으로 버전을 증가시키지 않습니다. 임대가 만료된 `RUNNING`은 원장 변경 없이 `UNKNOWN`으로
  투영하고 실행 중 집계에서 제외합니다. 재시도는 같은 Run ID와 더 높은 Generation을 사용합니다.
- 목록은 `(created_at DESC, run_id DESC)` Keyset과 신규 생성 Watermark를 사용합니다.
  `snapshotAt`은 신규 Run 유입만 제한하며 과거 상태의 불변 Snapshot을 보장하지 않습니다.
  상태·검색 필터는 각 읽기 시점의 원장 현재값을 평가합니다. 완전한 시점별 Audit Export가 아닙니다.
  페이지의 `startCursor`와 각 행의 `resumeCursor`는 Tenant·Owner·필터·Watermark·마지막
  위치에 HMAC으로 결속되고 한 시간 후 만료됩니다. 서명 Key는 기존 Identity Secret 또는
  Service Token에서 목적 분리해 파생하며 고정·빈 Key 대체를 하지 않습니다.
- Summary는 페이지 제한과 무관하게 동일 사용자의 전체 해당 원장 행을 집계합니다.
  질문·답변·대화명·Citation·암호문·임의 Correlation 문자열은 조회·복호화·응답하지 않습니다.
  V31은 opaque Agent audit ID와 여기서 계산한 결정적 UUIDv5 `auditRecordId`를 Run에
  저장하지만, 이 값은 중앙 감사 레코드의 조회 주소일 뿐 실제 수신·검증 증거가 아닙니다.
  현재 Publisher는 중앙 원장의 Ingestion Acknowledgement를 받지 않으므로 정상 Run의
  `auditStatus`와 Run 상세의 `auditEvidence.status`는 `PENDING`입니다. 실제 중앙 Receipt는
  Platform `/v1/workspace/activity/audit/evidence/{auditRecordId}`에서 Tenant·Actor·Source·Target을
  다시 검사해 별도로 확인하며, 아직 수집되지 않았으면 `404`입니다. Checkpoint 무결성도
  Platform Evidence의 `integrityStatus`만 사용하고 Agent가 `LINKED`나 `VERIFIED`를 합성하지
  않습니다. 감사 Tuple이 없는 이전 Run만 `NOT_LINKED`/`null`로 남습니다. 원천 장애는
  `503`이고 빈 목록이나 최신 상태로 위장하지 않습니다.
- 메모리 모드는 동일 Executor의 실제 begin/complete/fail 상태를 읽습니다. DB 설정 변경만으로
  실행 중인 프로세스의 조회 원장을 바꾸지 않으며 Source 전환은 정상 재시작 뒤에 수행합니다.

전용 `*_test`, `*_integration` 또는 `*_verify` PostgreSQL에서
`DWP_AGENT_INTEGRATION_DATABASE_URL`을 지정하고 `tests/test_activity_source.py`를 실행합니다.
동일 Run 재시도·오래된 Worker 완료/실패 차단·메모리/DB 동등성·페이지 밖 실행 집계·Cursor
변조/타인 재사용·원천 권한 회수·임대 만료·Product v5 Fail-closed를 릴리스 증적으로 확인합니다.

Tenant 정책은 `ai_conversation_retention_policies`가 단일 원장입니다. 기본 보존 기간은
90일이며 30일에서 3650일 범위만 허용합니다. Legal Hold가 활성화된 Tenant는 자동
만료와 사용자 삭제를 모두 차단합니다.

정책 변경은 인증된 관리 Control Plane과 감사 로그를 거쳐야 합니다. End-user API에서
직접 변경하거나 Agent가 임의로 보존 기간을 축소하지 않습니다. Legal Hold 해제 전에는
법무·보안 승인과 정책 버전 증가를 확인합니다.

## 6. 암호화 키 회전

1. KMS에서 새 KEK Version을 생성하고 기존 Alias 또는 Key Reference의 승격 절차를 승인합니다.
2. 새 KEK를 Active Version으로 배포한 뒤 Startup Probe와 신규 `dwp2` 쓰기·읽기를 확인합니다.
3. 기존 Envelope는 본문을 재암호화하지 않고 Wrapped DEK만 새 KEK로 Rewrap합니다.
4. Legacy 열은 별도 Migration Worker로 `dwp2` Envelope에 재암호화하고 Dual-read 지표를
   확인합니다.
5. 이전 KEK나 Legacy Key가 참조되지 않거나 보존 만료된 사실을 Query와 감사 증적으로
   확인한 뒤에만 읽기 권한을 제거합니다.
6. 필요한 과거 Key가 없는 배포는 읽기 실패가 정상이며 Key를 복구한 뒤 재배포합니다.

Key Material과 원문 질문·답변·Source ID·Service Token은 로그에 남기지 않습니다.

## 7. 검증 증적

- 2026-09-09: 이미 적용된 `V33`의 checksum
  `b90332db6a11ae2b3fa91d2874146402f499d76f9665a89eea29ba15d58085a4`를 불변으로 보존하고,
  강화된 Domain·Operation·Owner 삭제 Fence를 append-only `V34`로 분리했습니다. 실제 기존
  `V33` DB에서 `V34`까지 순방향 Upgrade하는 회귀를 별도 통과했고, Local Agent DB를 초기화하지
  않은 채 같은 Upgrade를 적용한 후 Startup 완료와 `/health` 200을 확인했습니다.
- 2026-09-09: clean PostgreSQL 네 개에서 `V1`~`V34` 34개를 적용하고 Agent 전체 회귀
  `499 passed`, Python Compileall, Runtime OpenAPI Snapshot, 모듈 크기·Import Cycle·Entrypoint
  Reachability와 Diff Check를 확인했습니다. Artifact Worker는 만료되지 않은 DLP Preflight와
  서버 결속 Citation을 실행 시점에 다시 검사하고 `MARKDOWN`·`DOCX`·`PDF` 바이트/Manifest를
  암호화 저장하며, Stale Lease·Outbox 재전달·Terminal 멱등·Tenant/User 격리·Hash 변조를
  독립 검증했습니다. 삭제 Worker는 Domain-scoped DB Guard, Legal Hold 재검사, Retry Budget,
  저장된 disposition receipt의 binding/keyed fingerprint 재검증과 변조 거부,
  `ROUTINE`·`MEMORY`·`ARTIFACT_EXPORT`·`ARTIFACT` 활성 행 제거 영수증을 검증했습니다.
  Routine background 실행은 위임 자격·Source Connector·Proposal/Notification Delivery가 없어
  계속 Fail-closed입니다. 조직 DLP, 사용자가 입력한 Source 검증, Owner 원천 삭제, Backup 삭제,
  사용자별 Key Crypto-shred도 완료로 기록하지 않습니다. 또한 현재 `:8100` Local Listener는
  별도 로컬 체크아웃의 `DTHub Agent Local`을 구동해 이 저장소의
  `/v1/conversations` 계약이 없으므로, 해당 Browser 화면은 현재 `dwp_agent` 변경본의 배포 검증
  증거로 사용할 수 없습니다.
- 2026-09-08: clean PostgreSQL 네 개에서 `V1`~`V32`를 적용하고 Agent 전체 회귀
  `462 passed`, Python Compileall, Runtime OpenAPI Snapshot을 확인했습니다. `V32`의 기존 사용자
  `UNSET` 기본값, 저장·답변 적용 독립 동의, Tenant·사용자·Session·Revision 멱등 명령, 활성·미만료
  유형별 최신 선호 선택, 프롬프트 인젝션형 선호 차단, 비신뢰 표현 전용 모델 전달과 값 없는 답변별
  적용 증거를 포함합니다. 이 결과는 로컬 `local-inline` Key Provider 회귀이며 관리형 KMS, 조직 DLP,
  물리 삭제 Worker 또는 외부 실행기를 운영 활성화했다는 의미가 아닙니다.
- 같은 기준점의 Frontend는 생성 Agent 계약 동기화, `497`개 파일의 `3,805`개 단위 테스트,
  Node 24 Production Build와 Bundle Budget을 통과했습니다. 개인 AI 제어와 답변 증거 E2E는 저장·적용
  동의 분리, 안전한 대체 답변, 음성·실행·제안 승인 경계, `320/390/768px`, 200% 확대,
  Dark/Forced Colors 및 자동 접근성 검사를 포함해 `29 passed`이며, 구형 Runtime 응답에서는 답변
  적용을 `UNSET`·비활성으로 Fail-closed 처리합니다.
- 2026-09-08: 선택 업무 연계의 Agent 집중 회귀 `61 passed`, clean PostgreSQL에서 `V1`~`V32`를
  적용한 Runtime 회귀 `16 passed`를 확인했습니다. 실제 Public Stream과 서명된 위임 Identity를 통해
  Tenant·사용자·Session·권한·Source ID·Version·Obligation을 검증하고, 모델 호출 전후 권한 회수·
  원천 장애·Stale Version에서 답변 0건, 경쟁·재생 요청의 대화 연속성과 상관관계 ID 결속을
  검증했습니다. Frontend Chromium·Mobile E2E는 `47 passed`, 프로젝트 조건부 `3 skipped`이며
  선택 업무 열기·연속 질문·Actor 전환·늦은 응답 폐기·`320px`·200% 확대를 포함합니다. 같은
  공유 트리의 Node 24 전체 비증분 TypeScript 검사는 오류 0건으로 통과했습니다.
- 2026-09-04: clean PostgreSQL에서 `V1`~`V30` Migration을 적용하고 Agent 전체 회귀
  `352 passed`, `23 skipped`를 확인했습니다. `V23`~`V30`의 개인 보존·삭제 Outbox,
  명시적 동의 기반 `DRY_RUN_ONLY` 루틴, 개인 Memory·Source Preference, mutable Draft와
  immutable Artifact Version·Reference, 최신 Preflight 결속, Export Request Receipt,
  동의 정합성 및 불변 증거 제약을 포함합니다. Python Compileall, Runtime OpenAPI Snapshot,
  모든 Runtime Python 모듈 500줄 이하도 통과했습니다. 검증용 Scratch DB는 삭제했습니다.
  이는 background scheduler, managed KMS, connector verifier, 조직 DLP, 파일 Export Worker,
  물리 삭제 실행기의 운영 활성화를 의미하지 않습니다.
- 2026-08-27: Local/Shared Key Provider Fail-closed, Java/Python Canonical Envelope Golden
  Fixture, Startup Cryptographic Probe와 clean PostgreSQL 전체 Agent 통합 테스트
  `221 passed`, Skip 0을 확인했습니다. 이 결과는 로컬 Key Provider 회귀 증적이며
  관리형 KMS 어댑터의 운영 활성화를 의미하지 않습니다.
- 2026-08-27: 일회성 PostgreSQL에서 `V1`~`V20` 20개 Migration 적용과 중복 0건을 확인했습니다.
  `V13` 신규 `dwp2` 쓰기·Legacy Dual-read, `V15` Lease Generation Fencing, `V16` 암호화
  One-time Question Ticket, `V17` 완료 Lease 세대 기반 대화 가시성, `V18` Legacy Message
  Backfill·Orphan 차단·빈 대화 비노출과 NOT NULL/RESTRICT 제약, `V19` 사용자별 Agent Inbox,
  `V20` HMAC 요청 지문과 Command 경쟁 직렬화를 검증했습니다. Scratch DB는 검증 후
  삭제했고 제안·결정 테스트 행이 남지 않은 것을 확인했습니다.
- 2026-08-27: Gateway·Agent 교차언어 서명 Assertion Golden Vector, Method·Path·수명·변조
  거부와 Local 외 필수 Secret Startup Fail-closed 검증
- 2026-08-27: Source·Action·Safety·Gate 명시적 Bootstrap, Write-on-read 제거, 실행 시 Gate
  재검사, Ask 2분 임대·원자 회수·세대 Fencing, 경쟁 요청의 대화 Orphan 0건, 제한형 Stream
  Worker와 Action Handoff v2 출처 결합 검증
- 2026-08-27: 만료 대화를 포함한 모든 GET 무변경, Bootstrap 전 PATCH 409 전제조건,
  시작 상태와 Live DB·Gate Schema를 함께 확인하는 `/readyz` Fail-closed 검증
- 2026-08-28: clean PostgreSQL에서 `V1`~`V22` 22개 Migration과 `V18` upgrade path를
  적용하고 Agent 전체 회귀 `247 passed`, Skip 0을 확인했습니다. `V21` Meeting workload
  assertion replay 방어와 `V22` 명시적 Workspace 분석 Preference·세대별 Command Lease·
  암호화 Receipt·Clear Fencing을 포함합니다. Python Compileall, Runtime OpenAPI Snapshot,
  모든 Runtime Python 모듈 500줄 이하도 함께 확인했습니다.
- 2026-08-27: Frontend 전체 단위 `253 files / 1,441 tests`, Node 24 Production Build와
  Bundle Budget, Agent·Gateway 생성 OpenAPI 계약 일치를 확인했습니다. Agentic Work OS의
  STT 검토·마이크 해제·명시적 TTS·실행 증거·Agent Inbox 사용자 통제 E2E는 Desktop과
  Mobile에서 `12 passed`이며 Axe serious/critical 0과 수평 Overflow 0을 확인했습니다.
- 2026-08-24: 전체 Agent 테스트 117개 통과, PostgreSQL 통합 시나리오 1개 별도 통과(적용된 migration checksum 불변성 포함)
- 2026-08-24: 전용 PostgreSQL 통합 DB에서 정책 구성, 리비전별 증빙, 누락 차단,
  검증, 자기 승인 차단, 독립 승인, 감사 타임라인과 승인 후 증빙 변경 차단 검증
- 2026-08-24: DB migration `V5`~`V11` 적용 및 보존·Source·Action·Safety·Evaluation·감사·운영 Gate
  스키마 확인. 적용된 migration의 checksum 불변성도 재기동으로 검증
- 2026-08-24: 운영 Gate의 환경 분리, 구성 리비전별 증빙, 버전 충돌, 비밀값 차단,
  구성·검증·승인자 분리와 만료 상태 회귀 검증
- 2026-08-24: Agent OpenAPI 정본과 Frontend 자동 생성 TypeScript 계약의 Drift 검사
- 2026-08-20: 평가 실행 이력·상세·직전 실행 비교·메트릭 전용 CSV와 동시 실행 차단 확인
- 2026-08-20: SKAX 위임 운영자에게 실제 집계 지표와 보존 정책 조회, 일반 Tenant
  관리자에게 메뉴 비노출과 Gateway 차단 확인

## 8. 외부 운영 환경 수용 조건

위 회귀는 코드와 로컬 통합 환경의 배포 준비 증적입니다. 다음 항목은 실제 운영 환경과 승인된
자격 증명 없이는 완료로 기록하지 않으며, 충족 전 외부 Provider 연동은 Fail-closed 상태를 유지합니다.

1. 운영 DNS, TLS 인증서 Chain·SAN, HSTS와 Gateway의 실제 `/api/agent/v1/ask/stream` Route를
   승인된 Canary에서 확인합니다.
2. 비밀값을 저장하거나 출력하지 않는 통제된 운영 사용자로 Tenant·사용자·Session·권한 결속을
   확인하고, Provider 응답의 `requestId`·`correlationId`·`conversationId`·선택 업무 Reference가
   요청과 정확히 일치하는지 검증합니다.
3. 외부 Provider·Trace·Audit에 Owner 업무 본문, 개인 선호 원문 또는 PII가 복제되지 않고 최소화된
   Reference와 Redacted Rationale만 남는지 확인합니다.
4. Stream 처리 중 권한 회수와 Source Version 변경을 실제로 수행해 늦은 답변이 폐기되는지,
   `401/403/404/409/503` 이후 이전 제안이나 답변이 재활성화되지 않는지 검증합니다.
5. 후속 질문이 동일한 Owner·Source·Version 범위에서 매번 재인가되고, 어떤 업무 Form도 자동 적용·
   저장·제출되지 않는지 확인합니다.
6. 증적에는 Timestamp, Commit, Environment, 비식별 Run ID를 기록하되 Secret·질문 원문·응답 원문·
   PII는 포함하지 않습니다. 관리형 KMS, 조직 DLP, 물리 삭제 Worker와 외부 실행기도 각각의 운영
   승인 및 관측 증적이 있어야 활성화할 수 있습니다.

## 9. 근거 기준

- [OWASP LLM Prompt Injection Prevention](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
- [OWASP Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)
- [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework)
- [AWS Operational Readiness Reviews](https://docs.aws.amazon.com/wellarchitected/latest/operational-readiness-reviews/wa-operational-readiness-reviews.html)
- [Azure Pipelines approvals and checks](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/approvals?view=azure-devops)
- [NIST SP 800-53 Rev. 5.1, AC-5 and AU-3](https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final)
- [RFC 9457 Problem Details for HTTP APIs](https://www.rfc-editor.org/rfc/rfc9457.html)
- [RFC 7515 JSON Web Signature](https://www.rfc-editor.org/rfc/rfc7515.html)
- [RFC 7519 JSON Web Token](https://www.rfc-editor.org/rfc/rfc7519.html)
- [RFC 9421 HTTP Message Signatures](https://www.rfc-editor.org/rfc/rfc9421.html)
- [NIST SP 800-204A, Secure Service Mesh](https://csrc.nist.gov/pubs/sp/800/204/a/final)
- [Microsoft Copilot Studio agent evaluation](https://learn.microsoft.com/en-us/microsoft-copilot-studio/analytics-agent-evaluation-intro)
- [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
- [OpenAI API Data Controls](https://platform.openai.com/docs/models/default-usage-policies-by-endpoint)
- [AWS KMS Key Rotation](https://docs.aws.amazon.com/kms/latest/developerguide/rotate-keys.html)

OpenTelemetry GenAI 규격은 전용 저장소로 이전 중이며 Schema URL이 아직 확정되지 않았습니다.
따라서 현재 릴리스는 표준 `X-Correlation-ID`와 기존 관측성 계약을 유지하고, 안정 버전과
마이그레이션 지침이 공개된 뒤 Semantic Convention 버전을 고정합니다.
