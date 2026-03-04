# langgraphPlan3

## 점검 결과

### 전체 판단
- 현재 구현은 `LangGraph + LangChain 기반 에이전트 구조`로 상당 부분 정리되었습니다.
- 문서 기준으로 보면 대부분의 핵심 단계는 구현되었고, 실제로 남아 있는 본질적 이슈는 `Phase F`, `Phase H`, 그리고 일부 문서-구현 정합성입니다.
- 특히 `Phase D`는 더 이상 초기 계획서에 적힌 `Transitional HITL` 상태로 보기 어렵고, 현재 코드는 same-run resume 방향으로 더 진전되어 있습니다.

### 세부 점검 결과
1. `Phase D`
- 코드 기준으로는 `interrupt()` + `checkpointer=MemorySaver()` + `same run_id/thread_id resume` 구조가 들어가 있습니다.
- 따라서 기존 문서에 남아 있는 `resumed_run_id 기반 새 run 재개` 설명은 현행 코드와 맞지 않습니다.
- 정리 필요: 문서와 판정을 실제 코드 기준으로 다시 맞춰야 합니다.

2. `Phase F`
- hierarchical retrieval, query rewrite, citation binding은 구현되어 있습니다.
- `cross-encoder rerank` hook도 존재하지만, 실패 시 passthrough되고 `retrieval_quality_comparison()`은 아직 스텁 성격이 강합니다.
- `evidence verification`은 모듈 분리는 됐지만 reporter 이전 하드 게이트로 완전히 정리된 수준은 아닙니다.

3. `Phase H`
- run 단위 diagnostics API, coverage 계산, workspace 내 진단 expander는 있습니다.
- 하지만 `운영 대시보드형 observability`, `장기 시계열`, `run 간 비교 차트` 수준은 아직 아닙니다.
- 현재는 기초 관찰성 수준입니다.

4. `UI / 시각화`
- 기능은 전반적으로 동작합니다.
- 다만 발표용 기준에서 보면, retrieval 비교 시각화와 diagnostics 표현은 아직 더 세련되게 다듬을 여지가 큽니다.
- 특히 “왜 이 결과가 더 좋은지”를 한눈에 보여주는 비교형 시각화는 아직 약합니다.

---

## 즉시 수정해야 할 것

- [x] 1. `Phase D` 문서 현행화
- `docs/work_info/langgraphPlan.md`
- `docs/work_info/langgraphPlan2.md`
- 관련 점검 문구
- 현재 코드 기준으로 `same-run interrupt/resume + checkpointer` 상태를 반영해야 합니다.
- 문서가 뒤처져 있어 발표/검수 시 혼선을 유발합니다.
- 점검결과: PASS
- [보완]
  - 없음. `docs/work_info/langgraphPlan.md`, `docs/work_info/langgraphPlan2.md` 모두 현재 코드 기준인 `same-run interrupt/resume + MemorySaver + resumed_run_id는 동일 run_id 의미`로 정리되어 있습니다.
  - 발표 전 문구도 `Transitional HITL`이 아니라 `정식 same-run interrupt/resume 적용, 단 persistent checkpointer는 후속 과제`로 통일된 상태입니다.

- [x] 2. `Phase F` 완료 문구 보수화
- 문서에서 `완료`처럼 읽히는 표현은 줄이고, `baseline 완료 + 고급화 잔여`로 정리해야 합니다.
- 현재는 hook와 스텁이 섞여 있어 과대평가 위험이 있습니다.
- 점검결과: PASS
- [보완]
  - 없음. 현재 문서들은 대체로 `baseline 완료 / 고급화 잔여`로 정리되어 있습니다.
  - 단, 이후 신규 문서 작성 시 `rerank 구현 완료` 같은 단정 표현은 피하고, `cross-encoder hook 존재 / 비교 모드와 독립 검증 계층은 잔여` 수준으로 유지해야 합니다.

- [x] 3. `Phase H` 완료 문구 보수화
- 현재는 `run diagnostics 기반 최소 구현 완료` 수준입니다.
- `운영형 observability 완성`처럼 읽히지 않도록 문구를 정리해야 합니다.
- 점검결과: PASS
- [보완]
  - 없음. 현재는 `run diagnostics 기반 최소 구현 완료`, `운영형 observability 아님`으로 충분히 보수적으로 표현되어 있습니다.
  - 다만 발표 자료에서는 `운영 대시보드`라는 표현 대신 `run 단위 진단 화면` 또는 `기초 관찰성`으로 표현하는 것이 더 안전합니다.

- [x] 4. 발표용 설명 정합성 점검
- `에이전트 스튜디오` 그래프 설명
- `AI 워크스페이스`에서 보여주는 스트림 설명
- `README` 및 관련 문서
- 문서/화면/실제 코드가 서로 같은 이야기를 하도록 맞춰야 합니다.
- 점검결과: PASS
- [보완]
  - 없음. `README.md`, `docs/work_info/langgraph.md`, `docs/work_info/langgraphPlan2.md`가 모두 같은 용어 체계를 사용합니다.
  - 현재 설명 기준은 다음으로 정리되어 있습니다.
    - `에이전트 대화` = 실제 LangGraph 실행 중 공개 가능한 작업 메모 스트림
    - `사고 과정` = 실행 후 같은 이벤트를 노드 기준으로 구조화한 리뷰 화면
    - `결과` = 최종 판단 + 규정 근거 + 검증 메모 + run diagnostics
    - `에이전트 스튜디오` = 상위 오케스트레이션 그래프 + 하위 실행 스킬 그래프 설명 화면

---

## 발표 전까지 필요한 최소 보완

1. Retrieval 결과 시각화 강화
- 후보군(top-k before/after rerank)
- 최종 채택 citation
- adoption 이유
를 비교형 카드 또는 표 형태로 보여주기.
- 구현방안
  - `ui/workspace.py`의 결과 탭과 `ui/rag.py`의 Run 인용 조회 화면에서 공통 렌더러를 사용해 `before rerank / after rerank / adopted` 3영역 구조로 통일한다.
  - `services/retrieval_quality.py`와 결과 payload의 `retrieval_snapshot`을 기준으로 `candidate_id`, `score_before`, `score_after`, `adopted`, `adoption_reason` 필드를 표준화한다.
  - UI는 단순 JSON expander가 아니라 카드형 비교 테이블로 구현하고, 각 citation마다 `문서명`, `조항`, `선택 사유`, `최종 사용 여부`를 배지로 표시한다.
  - 발표용 기준으로는 최소 `top-5 후보`, `최종 채택 citation`, `채택 이유 한 줄`까지는 화면에서 바로 읽히도록 한다.
- 보완·확인: 발표 전 범위는 `after rerank 후보 + 최종 채택 citation + adoption_reason`까지로 확정되었고, `score_before/score_after`와 full before/after 비교는 후속 고급화 과제로 유지한다.

2. Evidence verification 결과 표시
- coverage
- missing citation
- gate decision (`hold`, `caution`, `regenerate`)
를 UI에서 분리해서 보여주기.
- 구현방안
  - `services/evidence_verification.py`의 결과를 `verification_summary` 구조로 묶어 `coverage_ratio`, `missing_citations`, `gate_decision`, `ungrounded_sentences`를 고정 필드로 반환한다.
  - `ui/workspace.py` 결과 탭 상단에 verification summary 카드 3개(`근거 연결률`, `누락 citation 수`, `게이트 판정`)를 추가한다.
  - `missing citation`은 문장 단위 목록으로 별도 expander에 노출하고, 각 문장에 어떤 citation이 비었는지 표시한다.
  - `gate decision`은 색상 체계를 고정한다: `hold=red`, `caution=amber`, `regenerate=blue`.
- 보완·확인: 현재는 `critic_output.verification_targets -> verify 단계 evidence verification -> verification_summary 저장 -> hold/regenerate 시 hitl_pause` 구조로 정리되었고, 발표 전 기준 구현은 완료된 상태다.

3. Run diagnostics 가시성 개선
- 현재 expander 수준에서 끝내지 말고,
- 선택 run 기준으로 `citation coverage`, `hitl 여부`, `resume 여부`, `fallback 여부`를 카드/배지로 요약 표시.
- 구현방안
  - `main.py`의 diagnostics API 응답을 `run summary + flags + detail` 구조로 고정하고, `citation_coverage`, `hitl_requested`, `resume_success`, `fallback_used`, `tool_failures`를 최상위에 노출한다.
  - `ui/workspace.py`에서 결과 탭 또는 별도 diagnostics 행을 추가해 run 선택 시 요약 카드로 즉시 보이게 한다.
  - 현재 expander는 유지하되, expander는 상세 JSON/보조 정보만 담고 핵심 값은 카드/배지로 끌어올린다.
  - 향후 `ui/dashboard.py`와도 재사용할 수 있도록 diagnostics summary 렌더 함수를 공용 모듈(`ui/shared.py`)로 분리한다.
- 보완·확인: 현재 구현/문서/화면은 `resume_success` 기준으로 통일되었고, 발표 전 범위에서는 run diagnostics 요약 카드와 비교 화면까지 포함해 완료 처리한다.

**[추가답변]**
**[PASS]**
- 현재 `ui/workspace.py` 결과 탭에는 citation coverage, HITL 요청 여부, resume 성공 여부, fallback 비율이 카드 형태로 노출되고, compare run 영역도 최소 적용되어 있습니다.
- 문서 기준의 발표 전 최소 보완 수준은 충족하며, 남는 것은 운영형 대시보드 수준의 고급 시각화입니다.

4. AI 워크스페이스 마감
- 에이전트 대화: 실제 작업 메모 스트림 가독성 개선
- 사고 과정: 노드별 요약 밀도 조정
- 결과 탭: 최종 판단, 규정 근거, 검증 메모를 더 명확히 분리
- 구현방안
  - `ui/workspace.py`에서 `에이전트 대화`는 라이브 실행 전용으로 제한하고, `사고 과정`은 노드 요약 전용으로 역할을 확실히 분리한다.
  - 에이전트 대화는 `thought / action / observation`을 시각적으로 구분한 채팅 버블 레이아웃으로 바꾸고, 동일 노드 이벤트는 묶어서 렌더링한다.
  - 사고 과정은 `intake / planner / execute / critic / verify / reporter / finalizer`별 타임라인 카드로 요약하고, 각 카드에 핵심 판단 1~2문장만 남긴다.
  - 결과 탭은 `최종 판단 Hero`, `규정 근거`, `검증 메모`, `run diagnostics` 4블록으로 고정해 정보구조를 안정화한다.
  - 발표용 기준으로는 스크롤이 과도하게 길어지지 않도록 좌측 케이스 영역과 우측 결과 영역의 높이를 맞추고, 내부 스크롤만 사용한다.
- 보완·확인: 현재는 동일 이벤트 소스를 `에이전트 대화=라이브`, `사고 과정=노드 요약`, `실행 로그=세부 로그`로 분리해 사용하고 있으며, 발표 전 구조 기준으로는 충분하다.

5. 에이전트 스튜디오 마감
- 그래프 레전드 추가
- 스킬 설명 카드 정리
- 현재 runtime skill과 실제 실행 흐름 정합성 확인
- 구현방안
  - `ui/studio.py`에서 `그래프`, `스킬`, `프롬프트`, `지식` 탭의 시각 톤을 통일하고, 그래프 탭 상단에 범례(`노드`, `조건부 경로`, `HITL 경로`, `실행 스킬`)를 추가한다.
  - 스킬 탭은 DB 기반 legacy 설명이 아니라 실제 `runtime skill` 메타데이터를 기준으로 카드형으로 재구성한다.
  - 각 스킬 카드에는 `입력`, `출력`, `언제 호출되는지`, `발표용 한 줄 설명`을 넣어 운영자/발표자 둘 다 이해 가능하게 한다.
  - 실제 실행 흐름과 스튜디오 표시가 어긋나지 않도록 `build_agent_graph()`, `get_langchain_tools()`, `runtime skill registry`를 기준으로 studio 데이터를 생성한다.
  - 발표용 최종 마감 기준으로 그래프 탭 캡처만으로도 상위 오케스트레이션과 하위 스킬 흐름을 설명할 수 있게 문구 밀도를 조정한다.
- 보완·확인: 현재 스튜디오는 runtime skill 기준 카드, 상위/하위 그래프, 한글 설명을 갖추고 있으며, raw schema JSON은 보조 정보로 유지하는 방향이 발표용 기준에 적합하다.

### 추가 확인사항

1~5번 구현방안을 코드와 맞춰 검토했고, 현재는 발표 전 최소 보완 범위가 모두 충족된 상태입니다. 아래 메모는 “남은 결함”이 아니라, 이후 고급화에서 어떤 방향으로 확장할지 정리한 현행 구현 메모입니다.

#### 현행 구현 메모

**1. Retrieval 결과 시각화 강화**
- 발표 전 범위는 `after rerank 후보 + adopted citation + adoption_reason`으로 확정되어 있고, 현재 UI/결과 payload도 이 기준을 따릅니다.
- `before rerank full 비교`, `score_before/score_after 시각화`, `후보군 차트형 비교`는 후속 고급화 항목입니다.

**2. Evidence verification 결과 표시**
- `critic_output.verification_targets`를 기반으로 `verify` 단계에서 evidence verification을 수행하고, `verification_summary`를 result/UI에 반영하는 구조가 정착되었습니다.
- 이후 고급화는 `gate_decision`별 재생성 정책 세분화, sentence-level evidence diff 시각화 정도가 남아 있습니다.

**3. Run diagnostics 가시성 개선**
- 현재는 `citation_coverage`, `hitl_requested`, `resume_success`, `fallback_usage_rate`, run 비교 카드까지 포함한 발표 전 진단 화면이 제공됩니다.
- 이후 고급화는 장기 시계열, 운영 대시보드형 집계, run cohort 비교입니다.

**4. AI 워크스페이스 마감**
- 현재는 `에이전트 대화(라이브) / 사고 과정(노드 요약) / 실행 로그(세부 로그) / 결과(최종 판단)`로 역할이 분리되었습니다.
- 이후 고급화는 스트림 버블 UX 세련화, 결과 카드 밀도 최적화, 비교형 결과 보기 정도입니다.

**5. 에이전트 스튜디오 마감**
- 현재는 runtime skill 기준 카드, 상위/하위 그래프, 발표용 한글 설명이 갖춰진 상태입니다.
- 이후 고급화는 raw schema 노출 방식 정교화, tool contract 시각화, prompt/runtime diff 보기입니다.
추가 질문
Retrieval 시각화(1번)
“before/after rerank” 비교를 이번 발표까지 반드시 넣을 계획인가요?
아니면 우선 after rerank 후보 + 채택 citation만 통일하고, score_before/score_after/adoption_reason은 스텁/플레이스홀더로 두고 이후에 채워도 될까요?
[답변]
- 발표 전 기준으로는 `after rerank 후보 + 최종 채택 citation + 채택 이유`까지를 우선 구현하는 것이 맞습니다.
- 이유: 현재 소스에는 `retrieval_snapshot`과 `candidates_after_rerank`, `adopted_citations`는 있으나, `score_before`와 안정적인 `adoption_reason`은 전 구간에서 일관되게 저장되지 않습니다.
- 따라서 이번 발표 전에는 다음처럼 구현 범위를 고정하는 것이 리스크가 가장 낮습니다.
  - 필수: `after rerank 후보`, `최종 채택 citation`, `채택 이유(한 줄)`
  - 선택: `before rerank`는 필드가 존재하는 경우에만 표시
- 구현 시 필요한 추가 사항:
  - `policy_rulebook_probe` 또는 상위 retrieval 파이프라인에서 `adoption_reason` 필드를 명시적으로 생성해 `retrieval_snapshot`에 저장
  - `score_before`는 현재 없는 경우가 있으므로, 발표 전에는 스텁으로 채우기보다 **비표시**가 낫습니다. 없는 값을 가짜로 만드는 방식은 금지합니다.
- 결론: 이번 발표 전에는 `after rerank + adopted + adoption_reason`까지를 완료 기준으로 두고, `before rerank full 비교`는 발표 후 고급형 보완으로 넘기는 것이 맞습니다.

**[추가질문]**
- `adoption_reason`(채택 이유 한 줄)은 **어디서 생성**할까요? (예: reporter 노드에서 문장–citation 매핑 시 “이 citation을 쓴 이유”를 LLM으로 한 줄 생성해 저장 vs. `policy_rulebook_probe` 반환에 `reason` 필드 추가 vs. 발표 전에는 스텁 문구 "규정 근거로 채택" 등으로 고정)
- `retrieval_snapshot`에 `adoption_reason`을 넣는 시점은 **reporter 노드 종료 후 finalizer에서 result 조립 시**가 맞을까요, 아니면 reporter 내부에서 문장별로 채택 이유를 붙여서 state에 넣고 finalizer는 그대로 전달만 할까요?
**[추가답변]**
- `adoption_reason`은 `policy_rulebook_probe` 단계에서 1차 생성하고, reporter는 이를 소비만 하는 구조가 맞습니다.
- 이유:
  - 채택 이유는 retrieval 선택 맥락에서 가장 정확하게 만들어져야 합니다.
  - reporter에서 만들면 “문장 생성 후 사후 설명”이 되어 retrieval explainability가 약해집니다.
- 권장 구조:
  - `policy_rulebook_probe`가 각 adopted citation에 대해 `adoption_reason` 필드를 채움
  - reporter는 문장–citation 매핑만 수행
  - finalizer는 state의 `retrieval_snapshot`을 그대로 result에 옮김
- 발표 전 구현 기준:
  - `adoption_reason`은 LLM 자유생성보다 **규칙 + retrieval context 기반 한 줄 생성**이 우선
  - 예: “휴일/야간 식대 조건과 직접 일치하는 조항이어서 채택”
- 결론:
  - 생성 위치: `policy_rulebook_probe`
  - 저장 시점: reporter 이전, state에 먼저 저장
  - finalizer는 전달만 수행

**[PASS]**
- 현재 구현은 `policy_rulebook_probe` 단계에서 `adoption_reason`을 생성하고, `finalizer`가 이를 포함한 `retrieval_snapshot`을 결과에 저장합니다.
- `ui/workspace.py`와 `ui/rag.py` 모두 `after rerank 후보 + 최종 채택 citation + adoption_reason`을 기준으로 표시하고 있어, 발표 전 최소 보완 기준은 충족합니다.
- `before rerank` 비교와 `score_before/score_after` 시각화는 여전히 후속 고급화 과제로 남겨두는 것이 맞습니다.

Evidence verification(2번)
verify_evidence_coverage()를 언제 호출할지 정해주실 수 있을까요?
(A) reporter 직전 단계(verify 노드 이후, reporter 노드 진입 전)에 한 번만 호출
(B) reporter 노드 안에서, 문장 생성 후 호출
(A)면 “reporter 이전 게이트”로 활용 가능하고, (B)면 “결과 해석용 지표”에 가깝습니다. 발표에서 “게이트”까지 보여줄지, “결과 요약”만 보여줄지에 따라 선택이 갈릴 것 같습니다.
[답변]
- `A`가 맞습니다. 즉, `verify 노드 이후 / reporter 진입 전`에 단 한 번 호출해서 게이트로 사용해야 합니다.
- 이유:
  - 현재 목표는 “결과를 보여주는 것”보다 “에이전트가 근거를 검증한 뒤에만 결론을 확정한다”는 구조를 발표에서 보여주는 것입니다.
  - `reporter` 안에서 호출하면 verification이 단순 설명 지표로 떨어지고, best practice 관점의 검증 계층 분리가 약해집니다.
- 구현 시 필요한 추가 사항:
  - `langgraph_agent.py`의 `verify` 단계에서 `verify_evidence_coverage()`를 호출
  - 반환값을 `verification_summary`로 state에 저장
  - `gate_decision`이 `hold` 또는 `regenerate`면 `reporter`에 전달되는 입력을 제한하거나, 재생성/보류 분기로 넘김
  - `reporter`는 검증 완료 결과를 소비하는 노드로만 유지
- 결론: 발표 전에도 `reporter 이전 게이트` 구조로 구현하는 것이 맞고, 이는 임시 구조가 아니라 정석 구조입니다.

**[추가질문]**
- `verify_evidence_coverage()` 호출 위치는 **verify 노드 끝에서** 호출한 뒤 state에 `verification_summary`를 넣고, 기존 `_route_after_verify`에서 `gate_decision`이 `hold`/`regenerate`일 때도 **hitl_pause**로 보낼까요, 아니면 **reporter로 가지 않고** 별도 “hold” 전용 노드로 보낼까요? (현재 conditional edge는 hitl_pause vs reporter 두 갈래만 있음)
- `verify` 노드 내부에서 verification을 호출할 경우, **입력 문장**은 어디서 가져올까요? (reporter가 아직 실행되지 않았으므로, 이전 노드의 `planner_output`/`critic_output` 또는 “검증 대상 문장”을 별도 필드로 두는지)
**[추가답변]**
- 발표 전/정석 구조 기준으로는 `hold`와 `regenerate` 모두 우선 `hitl_pause`로 보내는 것이 맞습니다.
- 이유:
  - 현재 그래프 구조가 `reporter / hitl_pause` 2갈래인 상태에서 별도 hold 전용 노드를 추가하면 발표 전 리스크가 커집니다.
  - `hitl_pause`는 사람이 개입해야 한다는 의미를 가장 분명하게 전달할 수 있습니다.
- 권장 구조:
  - `verify` 노드 끝에서 `verify_evidence_coverage()` 호출
  - `verification_summary`를 state에 저장
  - `_route_after_verify`에서
    - `gate_decision in {hold, regenerate}` -> `hitl_pause`
    - 그 외 -> `reporter`
- 입력 문장 소스는 별도 필드로 두는 것이 맞습니다.
- 구체적으로:
  - `critic` 종료 시점에 `proposed_claims` 또는 `verification_targets` 배열을 state에 저장
  - `verify`는 이 배열을 입력으로 사용
  - reporter는 검증 통과 후 최종 표현만 담당
- 결론:
  - 분기: `hold/regenerate` 모두 `hitl_pause`
  - 입력 문장: reporter 이전에 별도 `verification_targets` 필드로 준비

**[추가질문1]**
- `verification_targets` 배열의 **각 문장(검증 대상)**은 어디서/어떻게 생성할까요? 현재 critic는 `critique`, `recommend_hold` 등만 출력하고 문장 리스트는 없습니다. (예: critic 출력 스키마에 `verification_targets: list[str]` 추가 후, planner의 plan 항목 요약 텍스트를 넣을지 / tool_results 기반으로 1문장씩 추출할지 / critic 내부에서 소규모 LLM으로 “검증할 주장 문장” 생성할지)
**[추가답변1]**
- `verification_targets`는 `critic` 출력 스키마에 명시적으로 추가하는 방식이 맞습니다.
- 이유:
  - 검증 대상 문장은 `reporter`가 최종 문장을 만들기 전에 이미 “무엇을 주장하려는지” 수준에서 정리돼 있어야 합니다.
  - planner의 plan 텍스트는 조사 의도이지 검증 대상 주장 문장이 아닙니다.
  - tool_results에서 직접 1문장씩 추출하면 근거 사실 조각은 얻을 수 있지만, 실제로 검증해야 할 “결론 후보 문장”이 되지 못합니다.
  - 별도 소규모 LLM 호출을 critic 내부에서 추가로 두는 것도 가능하지만, 발표 전 기준으로는 구조와 비용이 늘어납니다.
- 권장 구조:
  - `critic_output` 스키마에 `verification_targets: list[str]` 필드 추가
  - critic는 기존 `critique`, `recommend_hold`, `issues`와 함께 “검증해야 할 주장 문장” 1~3개를 생성
  - verify는 이 `verification_targets`를 입력으로 받아 evidence coverage를 검사
  - reporter는 verification 통과 후 이 문장들을 더 다듬어 최종 표현으로 변환
- 생성 규칙:
  - 최대 3문장
  - 한 문장당 하나의 핵심 주장만 포함
  - 반드시 evidence/citation으로 뒷받침 가능한 주장만 생성
  - 금지: 수사적 표현, 종합 판단이 여러 개 섞인 장문
- 구현 시 필요한 추가 사항:
  - `critic` structured output model에 `verification_targets` 필드 추가
  - `langgraph_agent.py`에서 `critic_node -> verify_node` 전달 state에 `verification_targets` 저장
  - `verify_evidence_coverage()`는 `verification_targets` 기준으로 coverage 계산
- 결론:
  - 생성 위치: `critic`
  - 저장 위치: `critic_output.verification_targets`
  - 사용 위치: `verify`

**[PASS]**
- 현재 구현은 `critic_output.verification_targets` 생성, `verify` 단계의 `verify_evidence_coverage_claims(...)` 호출, `verification_summary`의 state 저장, `final result` 반영, 그리고 `hold/regenerate -> hitl_pause` 분기까지 연결되어 있습니다.
- `ui/workspace.py` 결과 탭에서도 `근거 연결률`, `누락 citation 수`, `게이트 판정`, `missing_citations` 목록이 표시되어 발표 전 최소 보완 기준을 충족합니다.

사고 과정(4번)
“사고 과정” 탭은 실행 로그와 같은 이벤트를 노드별로만 묶어서 보여주면 될까요?
아니면 노드별로 별도 요약 문장(예: LLM으로 “이 노드에서 한 일” 1문장 생성)을 두는 요구가 있나요?
[답변]
- 발표 전 기준으로는 **같은 이벤트를 노드별로 묶고, 결정적 요약만 파생해서 보여주는 방식**이 맞습니다.
- 별도 LLM 요약을 추가로 돌리는 것은 이번 단계에서는 권장하지 않습니다.
- 이유:
  - 이미 `에이전트 대화`에서 LLM 작업 메모 스트림을 보여주고 있습니다.
  - `사고 과정`까지 별도 LLM 요약을 생성하면 중복 비용과 설명 충돌이 생길 수 있습니다.
  - 발표 전에는 “같은 실행 로그를 구조화해서 보여준다”가 더 안정적입니다.
- 구현 시 필요한 추가 사항:
  - `ui/workspace.py`에서 기존 이벤트를 `node` 기준으로 그룹핑
  - 각 노드 카드에는
    - 시작/종료 시각
    - 대표 메시지 1개
    - tool 호출 수
    - 핵심 observation 1개
    를 파생해서 표시
  - 이 대표 문구는 새 LLM 호출이 아니라 기존 이벤트 중 우선순위(`PLAN_READY`, `TOOL_RESULT`, `GATE_APPLIED` 등)로 선택
- 결론: 발표 전에는 `이벤트 재구성형 노드 요약`이 맞고, 별도 LLM 노드 요약은 후속 고급화 과제로 두는 것이 적절합니다.

**[추가질문]**
- “대표 메시지 1개” 선택 규칙을 어떻게 할까요? (예: 해당 노드 이벤트 중 `NODE_END`의 `message` 우선 → 없으면 `TOOL_RESULT`의 `observation` → 없으면 `PLAN_READY`의 `message` 등 우선순위를 문서/코드에 명시할지)
- **execute** 노드는 이벤트 수가 많을 수 있는데, execute 노드 카드에는 “tool 호출 수 + 핵심 observation 1개”만 넣을지, **첫 번째/마지막 tool 이름**도 함께 표시할지 정할까요?
**[추가답변]**
- 대표 메시지 선택 규칙은 문서/코드에 명시해야 합니다.
- 권장 우선순위:
  1. `NODE_END.message`
  2. `GATE_APPLIED.message`
  3. `TOOL_RESULT.observation`
  4. `PLAN_READY.message`
  5. `NODE_START.message`
- 이유:
  - 완료 시점 메시지가 가장 요약 가치가 높고,
  - gate 적용 여부는 발표에서 중요한 판단 근거이기 때문입니다.
- `execute` 노드는 `tool 호출 수 + 핵심 observation 1개 + 첫 번째/마지막 tool 이름`까지 넣는 것이 좋습니다.
- 이유:
  - execute는 실제 에이전트가 “행동”하는 구간이라, 어떤 도구 흐름으로 진행됐는지 보여줘야 agentic 성격이 살아납니다.
  - 다만 모든 tool을 나열하면 과하므로 `first_tool`, `last_tool`, `tool_count`만 요약하는 것이 적절합니다.
- 결론:
  - 대표 메시지 규칙은 문서와 코드에 고정
  - execute 카드는 `tool_count + first_tool + last_tool + 핵심 observation` 구조 권장

**[PASS]** (4. AI 워크스페이스 마감)
- `ui/workspace.py`에서 에이전트 대화/사고 과정/결과 탭 역할이 구분되어 있고, 결과 탭은 최종 판단·규정 근거·검증 메모·run diagnostics 4블록으로 정리되어 있습니다.
- 사고 과정은 노드별 타임라인(`summarize_process_timeline`)으로 그룹핑되며, 대표 메시지 우선순위(NODE_END > GATE_APPLIED > TOOL_RESULT > PLAN_READY > NODE_START)와 execute 노드의 `first_tool`/`last_tool`/`tool_count`가 반영되어 발표 전 최소 보완 기준을 충족합니다.

스튜디오 스킬 카드(5번)
“입력/출력”을 발표용으로는
(A) 스키마 요약 1~2문장(한글),
(B) JSON 스키마 일부 그대로
중 어떤 쪽을 목표로 할까요? (A)면 문구 설계가, (B)면 스키마 추출/포맷만 맞추면 됩니다.
[답변]
- 기본값은 `A`로 가야 합니다.
- 즉, 발표 화면에서는 `한글 요약 1~2문장`이 주력이고, 필요하면 하단 expander에서 raw schema(JSON)를 보조로 여는 방식이 가장 좋습니다.
- 이유:
  - 발표/시연에서는 운영자와 임원이 바로 이해할 수 있는 설명이 우선입니다.
  - JSON 스키마를 전면에 두면 기술자에게만 친화적이고, 정보 밀도가 과합니다.
- 구현 시 필요한 추가 사항:
  - `get_langchain_tools()`로부터 `args_schema`를 읽어 사람이 읽는 문장으로 변환하는 helper 추가
  - 예:
    - 입력: “전표 발생시각, 금액, 근태 상태를 받아 휴일 준수 여부를 판단합니다.”
    - 출력: “휴일 여부, 판정 사유, 적용 규정 후보를 반환합니다.”
  - 각 스킬 카드 하단에 `원본 스키마 보기` expander를 두어 raw schema도 확인 가능하게 함
- 결론: 발표용 기준은 `A(한글 요약)`이 맞고, `B(JSON)`는 디버그/운영 보조로만 두는 것이 좋습니다.

**[추가질문]**
- 한글 요약 1~2문장은 **스킬별로 미리 정의한 고정 문구**(스킬 추가 시 수동 작성)로 갈까요, 아니면 `description` + `args_schema` 필드명을 조합해 **자동 생성**(예: “입력: {필드명 나열}, 출력: 규정 후보·판정 사유 등”)을 목표로 할까요?
- Pydantic/스키마에서 “사람이 읽는 문장”으로 변환할 때, **필드명을 한글 라벨로 매핑한 테이블**을 두고 그걸 우선 사용할지, 필드명 그대로 나열할지 정할까요?
**[추가답변]**
- 발표용 품질 기준으로는 **고정 문구 우선 + 자동 생성 보조**가 맞습니다.
- 이유:
  - 자동 생성만 쓰면 어색한 문장이 나올 수 있고, 발표용 품질이 흔들립니다.
  - 반대로 전부 수동만 두면 스킬 추가 시 유지보수 비용이 커집니다.
- 권장 방식:
  - 각 runtime skill에 `display_summary_ko`를 선택 필드로 둠
  - 값이 있으면 그 문구를 사용
  - 없으면 `description + args_schema` 기반 자동 생성 fallback 사용
- 필드명 변환은 **한글 라벨 매핑 테이블 우선**이 맞습니다.
- 이유:
  - `occurred_at`, `mcc_code`, `budget_exceeded` 같은 필드명을 그대로 노출하면 발표용으로 거칠고 이해가 떨어집니다.
- 구현 기준:
  - `ui/studio.py` 또는 공용 helper에 `FIELD_LABELS_KO` 매핑 테이블 추가
  - 자동 생성 fallback에서도 이 매핑을 우선 사용
- 결론:
  - 기본: 수동 고정 문구
  - fallback: 자동 생성
  - 필드 표현: 한글 라벨 테이블 우선

**[PASS]**
- 현재 `ui/studio.py`는 실제 runtime skill 기준 카드, 그래프 레전드, 상위 오케스트레이션/하위 실행 스킬 그래프, 한글 설명 요약을 모두 반영하고 있습니다.
- 발표 전 최소 보완 기준에서 요구한 스튜디오 설명력과 시각 구조는 충족합니다.

---

## 고급형 후속 보완

1. Retrieval 고급화
- `cross-encoder rerank`를 실제 우선 경로로 안정화
- `LLM rerank` 옵션화
- retrieval comparison을 실험 모드로 분리

2. Evidence verification 고급화
- 독립 검증 계층으로 reporter 이전 강제 게이트화
- verification 결과에 따라 `hold / caution / regenerate`를 분기

3. Observability 고급화
- 운영 대시보드형 화면 추가
- 장기 시계열
- run 간 비교
- fallback / citation / HITL 추세 차트

4. 테스트 고도화
- interrupt/resume replay 실동작 테스트 강화
- rerank 및 citation coverage 회귀 테스트 추가
- diagnostics API/화면 일관성 테스트 추가

5. 발표 이후 운영형 정리
- `MemorySaver`에서 persistent checkpointer(`SqliteSaver` 등) 검토
- retrieval snapshot / adopted citations 저장 구조 강화
- 향후 운영형 vector retrieval abstraction 확장

---

## 권장 우선순위
1. 문서-구현 정합성 수정 (`Phase D`, `Phase F`, `Phase H` 문구)
2. 발표 전 최소 보완 1~5 수행
3. 발표 이후 고급형 후속 보완 착수

---

## 최종 판단
- 지금 구현은 발표 가능한 수준까지는 올라와 있습니다.
- 다만 `문서와 실제 구현 상태가 일부 어긋나는 부분`과 `retrieval/observability의 고급화 미완료`는 분명히 남아 있습니다.
- 따라서 현재 가장 합리적인 판단은 다음과 같습니다.
  - 발표 전: 문서 정합성 + 시각화/설명력 보강
  - 발표 후: retrieval / verification / observability를 고급형으로 확장

---

## 발표 전 필수 체크리스트

### A. 문서 정합성
- [x] `langgraphPlan.md`의 Phase D 설명이 현재 코드와 일치한다.
- [x] `langgraphPlan2.md`의 점검 결과가 현재 구현 상태와 일치한다.
- [x] `README.md`, `docs/work_info/langgraph.md`, 화면 설명 문구가 같은 구조를 설명한다.
- [x] `resumed_run_id`가 현재는 **새 run 생성**이 아니라 **동일 run_id 반환 의미**임을 문서/API 설명에 명시한다. (`README.md` HITL·API 섹션 반영)

### B. 에이전트 실행 구조
- [x] `same-run interrupt/resume` 흐름이 실제로 동작한다.
- [x] `MemorySaver` 기반 resume가 재현된다.
- [x] `tool loop`와 현재 runtime skill 호출이 문서/화면과 일치한다.
- [x] `MemorySaver` resume는 **같은 프로세스 / 같은 세션 범위**에서만 유효하며, 재기동 후 복구는 불가하다는 전제를 문서/발표 멘트에 명시한다. (`README.md` 주의사항 반영)

### C. Retrieval / Evidence
- [x] 후보 citation과 최종 채택 citation이 화면에서 구분되어 보인다.
- [x] evidence verification 결과가 결과 탭 또는 diagnostics에서 확인 가능하다.
- [x] retrieval 결과가 “왜 이 조항이 채택되었는지” 설명 가능하다 (adoption_reason 표시).

### D. UI / 발표 동선
- [x] `AI 워크스페이스`에서 케이스 선택 → 분석 시작 → 결과 확인 흐름이 자연스럽다.
- [x] `에이전트 스튜디오`에서 그래프/스킬/프롬프트 설명이 가능하다.
- [x] `규정문서 라이브러리`에서 청킹 실험/근거 설명이 가능하다.
- [x] `시연 데이터 제어`에서 시나리오 생성 후 곧바로 시연에 사용할 수 있다.

### E. 검수
- [x] interrupt/resume 테스트가 통과한다.
- [x] tool schema 테스트가 통과한다.
- [x] citation binding 테스트가 통과한다.
- [ ] 브라우저 콘솔/앱 실행 중 치명적 오류가 없다.

---

## 발표 후 장기 고도화 백로그

### 1. Retrieval 고도화
- [ ] cross-encoder rerank를 실제 기본 경로로 안정화
- [ ] LLM rerank를 옵션 모드로 추가
- [ ] retrieval candidate/adopted citation 저장 구조 강화
- [ ] retrieval quality comparison을 실제 비교 엔진으로 고도화

### 2. Evidence Verification 고도화
- [ ] verification을 reporter 이전 강제 게이트로 분리
- [ ] coverage 부족 시 `hold / caution / regenerate` 정책을 명시적으로 분기
- [ ] sentence-level grounding coverage를 대시보드 지표로 승격

### 3. Observability 고도화
- [ ] 운영 대시보드형 diagnostics UI 구축
- [ ] run 간 비교 차트 추가
- [ ] 장기 시계열(일/주/월) 추세 추가
- [ ] interrupt rate / fallback rate / citation coverage 추세 추가

### 4. Persistence 고도화
- [ ] `MemorySaver`에서 persistent checkpointer 전환 검토
- [ ] replay 가능한 snapshot 구조 강화
- [ ] run / state / citation lineage 저장 구조 정리
- [ ] retrieval candidate / adopted citation 저장 구조를 시계열·비교 엔진 기준으로 강화

### 5. 테스트 고도화
- [ ] graph transition unit test 확장
- [ ] interrupt/resume replay end-to-end 테스트 추가
- [ ] rerank 회귀 테스트 추가
- [ ] diagnostics API/UI 일관성 테스트 추가

### 6. UX / Studio 고도화
- [ ] AI 워크스페이스 스트림 UX 개선
- [ ] 스튜디오 그래프 레전드/설명 카드 고도화
- [ ] 청킹 실험실 시각화 고도화
- [ ] 발표 버전 / 운영 버전 UI 모드 분리 검토

---

## 실행 권장 순서
1. 발표 전 필수 체크리스트 A~E 완료
2. 발표 종료 후 Retrieval/Evidence/Observability 순으로 고도화
3. 이후 Persistence/Test/UX를 병행 정리
