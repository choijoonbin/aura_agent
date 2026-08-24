# DWAI·ON Production Gate

DWAI·ON은 읽기 전용 근거 탐색과 담당 앱 초안 전달까지만 수행합니다. 이 문서는 운영
승인 전에 반복 실행할 최소 보안·품질 게이트와 데이터 운영 절차를 정의합니다.

## 1. 필수 실행 설정

- Agent와 Gateway의 `DWP_AGENT_SERVICE_TOKEN`은 동일한 관리형 Secret을 사용합니다.
- Platform 읽기에는 `DWP_PLATFORM_RUNTIME_SERVICE_TOKEN`, Approval 읽기에는 별도의
  `DWP_APPROVAL_RUNTIME_SERVICE_TOKEN`을 사용합니다. Gateway Token을 재사용하지 않습니다.
- `DWP_AGENT_REGISTRY_MODE=enforced`로 실행하고 승인된 Agent Revision이 없으면
  실패 차단합니다.
- `DWP_AGENT_DATA_KEY`는 32-byte Key의 Base64 값이며 KMS/Secret Manager에서 주입합니다.
- `DWP_AGENT_DATA_KEY_VERSION`은 배포마다 명시하고, 아직 보존 중인 암호문에 필요한
  이전 키는 `DWP_AGENT_PREVIOUS_DATA_KEYS` JSON 맵으로 제공합니다.
- 승인된 모델 Snapshot과 관리형 API Key를 사용합니다. 모델 설정이 없으면
  `CONFIGURATION_REQUIRED`가 정상 결과이며 대체 답변을 생성하지 않습니다.
- `DWP_ENVIRONMENT=production`에서는 위 설정과 전용 DB, 분리된 Service Token,
  감사/API 이력 수집 설정이 누락되거나 서로 재사용되면 프로세스가 시작되지 않습니다.
- Gateway의 `agentRuntime` 제한 시간은 모델 호출 제한보다 길어야 합니다. 기본 계약은
  모델 20초, Slow-call 30초, Gateway 45초이며 일반 업무 API의 10초 제한과 분리합니다.

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
6. 과거 Key가 누락되면 암호문을 평문이나 빈 값으로 대체하지 않고 실패 차단하는지
   확인합니다.
7. 동일 평가 세트의 최신 실행과 직전 실행을 비교하고, 통과율 또는 사례별 회귀가 있으면
   릴리스 승인자가 결과를 검토하는지 확인합니다.

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
  `MANAGE`가 필요합니다.
  Legal Hold 필드를 포함한 변경은 `MANAGE`만 허용합니다.
- 변경 요청은 현재 `policyVersion`, 10~500자의 사유와 `X-Correlation-ID`를 요구합니다.
  버전 충돌은 `409`로 실패하며 묵시적으로 덮어쓰지 않습니다.
- `ai_retention_policy_events`는 이전 값, 새 값, 수행자, 상관관계 ID, 사유를 append-only로
  기록합니다. 운영 API로 이 이력을 수정하거나 삭제하는 경로를 제공하지 않습니다.

## 4. 보존 및 Legal Hold

Tenant 정책은 `ai_conversation_retention_policies`가 단일 원장입니다. 기본 보존 기간은
90일이며 30일에서 3650일 범위만 허용합니다. Legal Hold가 활성화된 Tenant는 자동
만료와 사용자 삭제를 모두 차단합니다.

정책 변경은 인증된 관리 Control Plane과 감사 로그를 거쳐야 합니다. End-user API에서
직접 변경하거나 Agent가 임의로 보존 기간을 축소하지 않습니다. Legal Hold 해제 전에는
법무·보안 승인과 정책 버전 증가를 확인합니다.

## 5. 암호화 키 회전

1. KMS에서 새 32-byte Data Key를 생성하고 새 불변 버전 이름을 정합니다.
2. 새 Key와 버전을 Active 설정으로, 기존 Key를 Previous Key 맵으로 배포합니다.
3. 새 실행과 대화가 새 버전으로 기록되고 이전 대화가 정상 복호화되는지 확인합니다.
4. 이전 버전 암호문이 모두 재암호화되거나 보존 만료된 사실을 Query와 감사 증적으로
   확인한 뒤에만 Previous Key를 제거합니다.
5. 필요한 과거 Key가 없는 배포는 읽기 실패가 정상이며 Key를 복구한 뒤 재배포합니다.

Key Material과 원문 질문·답변·Source ID·Service Token은 로그에 남기지 않습니다.

## 6. 검증 증적

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

## 7. 근거 기준

- [OWASP LLM Prompt Injection Prevention](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
- [OWASP Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)
- [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework)
- [AWS Operational Readiness Reviews](https://docs.aws.amazon.com/wellarchitected/latest/operational-readiness-reviews/wa-operational-readiness-reviews.html)
- [Azure Pipelines approvals and checks](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/approvals?view=azure-devops)
- [NIST SP 800-53 Rev. 5.1, AC-5 and AU-3](https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final)
- [RFC 9457 Problem Details for HTTP APIs](https://www.rfc-editor.org/rfc/rfc9457.html)
- [Microsoft Copilot Studio agent evaluation](https://learn.microsoft.com/en-us/microsoft-copilot-studio/analytics-agent-evaluation-intro)
- [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
- [OpenAI API Data Controls](https://platform.openai.com/docs/models/default-usage-policies-by-endpoint)
- [AWS KMS Key Rotation](https://docs.aws.amazon.com/kms/latest/developerguide/rotate-keys.html)

OpenTelemetry GenAI 규격은 전용 저장소로 이전 중이며 Schema URL이 아직 확정되지 않았습니다.
따라서 현재 릴리스는 표준 `X-Correlation-ID`와 기존 관측성 계약을 유지하고, 안정 버전과
마이그레이션 지침이 공개된 뒤 Semantic Convention 버전을 고정합니다.
