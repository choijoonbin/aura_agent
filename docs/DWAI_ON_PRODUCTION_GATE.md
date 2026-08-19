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

## 2. 릴리스 차단 테스트

```bash
uv sync --frozen
uv run pytest
uv run python -m compileall -q src
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

운영 모델 또는 Prompt가 바뀌면 고정 회귀 세트 외에 승인된 평가 환경에서 간접
인젝션, 데이터 유출, Citation 정확도, 무응답 적합성, 한국어·영어 동등성을 다시
측정하고 결과를 릴리스 증적으로 보관합니다.

## 3. 보존 및 Legal Hold

Tenant 정책은 `ai_conversation_retention_policies`가 단일 원장입니다. 기본 보존 기간은
90일이며 30일에서 3650일 범위만 허용합니다. Legal Hold가 활성화된 Tenant는 자동
만료와 사용자 삭제를 모두 차단합니다.

정책 변경은 인증된 관리 Control Plane과 감사 로그를 거쳐야 합니다. End-user API에서
직접 변경하거나 Agent가 임의로 보존 기간을 축소하지 않습니다. Legal Hold 해제 전에는
법무·보안 승인과 정책 버전 증가를 확인합니다.

## 4. 암호화 키 회전

1. KMS에서 새 32-byte Data Key를 생성하고 새 불변 버전 이름을 정합니다.
2. 새 Key와 버전을 Active 설정으로, 기존 Key를 Previous Key 맵으로 배포합니다.
3. 새 실행과 대화가 새 버전으로 기록되고 이전 대화가 정상 복호화되는지 확인합니다.
4. 이전 버전 암호문이 모두 재암호화되거나 보존 만료된 사실을 Query와 감사 증적으로
   확인한 뒤에만 Previous Key를 제거합니다.
5. 필요한 과거 Key가 없는 배포는 읽기 실패가 정상이며 Key를 복구한 뒤 재배포합니다.

Key Material과 원문 질문·답변·Source ID·Service Token은 로그에 남기지 않습니다.

## 5. 근거 기준

- [OWASP LLM Prompt Injection Prevention](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
- [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework)
- [OpenAI API Data Controls](https://platform.openai.com/docs/models/default-usage-policies-by-endpoint)
- [AWS KMS Key Rotation](https://docs.aws.amazon.com/kms/latest/developerguide/rotate-keys.html)
