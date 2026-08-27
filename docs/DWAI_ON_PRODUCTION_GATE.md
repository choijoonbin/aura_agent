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
- Agent Inbox는 TENANT 관리 생산자가 명시적으로 생성한 제안만 대상 사용자에게 노출합니다.
  제안 본문과 감사 사유는 Envelope 암호화하고, Source Event 중복·명령 멱등성·Revision
  사전조건·append-only 결정 증거를 강제합니다. GET은 만료와 미루기 종료를 투영할 뿐 어떤
  행도 생성·갱신하지 않으며 `ACCEPT`는 자동 실행 권한으로 해석하지 않습니다.
- 생성·결정 명령의 재시도 지문은 평문 Hash가 아니라 Data Key에서 Tenant와
  `agent-proposal` Purpose를 분리해 파생한 HMAC-SHA-256을 사용합니다. 동일 Command와
  Source Event는 PostgreSQL Transaction Advisory Lock으로 직렬화하고, 사유·Revision·Note·
  Snooze 시각을 포함한 전체 Payload가 달라지면 기존 성공을 재사용하지 않습니다.

## 5. 보존 및 Legal Hold

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
- 2026-08-27: 최신 Agent 전체 회귀 `221 passed`, Skip 0, Python Compileall 및 Runtime
  OpenAPI Snapshot 일치, 모든 Runtime Python 모듈 500줄 이하 확인
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

## 8. 근거 기준

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
