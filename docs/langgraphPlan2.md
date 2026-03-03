## 커서 의견
1. 문서 정리 (우선 권장)
langgraphPlan.md §2 “현재 상태 요약”
아직 “execute는 SKILL_REGISTRY 직접 호출”, “structured output 미정식화”, “HITL이 interrupt/resume로 미이행”이라고 되어 있음.
작업: Phase A~D 반영한 현재 구현 상태로 문단 수정 (execute = LangChain tool 루프, structured output·HITL 현재 방식 명시).
2. 점검 재검토 결과 반영
Phase C·D “점검 내용 답변”
재검토 후 “완료”/“부분 완료” 등 최종 판단이 나오면, 해당 Phase의 점검 내용·결론 문구를 그에 맞게 수정.
3. 후속/선택 과제 (점검 특이사항 기준)
구분	내용	출처
Checkpoint	MemorySaver / SqliteSaver 등 LangGraph checkpoint 도입으로 “checkpoint 저장 방식 정리” 완료	Phase E 점검 내용
Retrieval 고도화	cross-encoder 또는 LLM rerank, evidence verification을 독립 검증 계층으로 분리	Phase F 점검 내용
Observability	run 단위가 아닌 운영 대시보드·장기 시계열 지표 (interrupt rate 집계 등)	Phase H 점검 내용
정식 HITL	“정식”을 같은 run 재개(interrupt_before + checkpointer)로 한정할 경우, 그 방식으로 전환 검토	Phase D 점검 내용 답변
4. 테스트 전략 구현 (공식 문서 8.13)
로드맵에 적힌 테스트 전략이 아직 코드로 구현되어 있지 않음. 공식 문서 Section 8.13에 맞춰 다음을 추가하는 것이 “남은 작업”에 해당합니다.
graph unit test — 메인 그래프 노드/엣지·상태 전이
tool schema contract test — 각 tool 입출력 스키마 검증
interrupt/resume replay test — HITL 시나리오 재현·재개
citation binding regression test — reporter citation 구조·coverage 유지
(테스트 디렉터리/파일은 아직 없음.)
5. 우선순위 제안
즉시: §2 “현재 상태 요약” 수정으로, 지금 구현 상태와 로드맵이 어긋나지 않게 함.
단기: Phase C·D 재검토 결과가 나오면 점검 내용/결론 문구 반영.
중기: 테스트 전략 4종을 최소한 한 번씩 구현(또는 스켈레톤 추가).
선택: checkpoint 정식화, retrieval 고도화, 운영 observability, 정식 HITL(같은 run 재개)은 우선순위와 리소스에 따라 순차 진행.
정리하면, 지금 우리가 해야 할 남은 작업은
문서(현재 상태 요약) 업데이트,
점검 재검토 결과 반영,
테스트 전략 구현,
(선택) checkpoint / retrieval / observability / 정식 HITL
입니다.


##codex 의견
핵심 잔여 작업

Phase D 정리
현재는 기능은 있음
하지만 정식 LangGraph interrupt/resume은 아님
선택지 두 개
문서 기준을 완화해서 현재 방식으로 PASS 처리
실제 interrupt/resume + checkpointer로 올려서 정식 완료 처리
제가 보기엔 “best practice” 목표면 2번이 맞습니다.
Phase F 고도화 잔여
현재 PASS지만 baseline 수준입니다.
남은 고도화:
cross-encoder 또는 LLM rerank
evidence verification 독립 계층화
retrieval quality 비교 모드
즉, 완료는 됐지만 “최고 수준”은 아직 아닙니다.
Phase H 운영 관찰성 확장
현재 PASS는 run 단위 diagnostics 기준
남은 작업:
UI에서 diagnostics 시각화
run 간 비교
fallback / citation / HITL 비율 추세
즉, 최소 구현은 끝났고 운영형 관측은 남아 있습니다.
AI 워크스페이스 최종 UX 마감
기능은 대부분 있음
남은 건 발표용 마감입니다.
에이전트 대화 스트림 가독성
사고 과정 요약 품질
케이스 선택/분석 동선
카드/패널 타이포/간격
에이전트 스튜디오 최종 마감
그래프/스킬/프롬프트는 있음
남은 건 발표용 보강
설명 문구
그래프 레전드
스킬 설명 밀도
실제 runtime 기준 정합성 최종 점검
규정문서 라이브러리 / 청킹 실험실 마감
구조는 있음
남은 건 비교와 설명력
청킹 전략별 차이 시각화
품질 설명 강화
retrieval 실험 결과 표현
실질적으로 가장 중요한 남은 것

정석 LangGraph/LangChain 목표 관점에서는 Phase D
발표/시연 완성도 관점에서는 AI 워크스페이스 UX 마감
한 줄 요약

문서 기준으로는 대부분 끝났고,
진짜 남은 건
D를 정석으로 올릴지
F/H를 고도화할지
화면 마감
입니다.
제 권장 순서

Phase D 정식화 여부 결정
AI 워크스페이스 최종 마감
F/H 고도화
스튜디오/라이브러리 발표용 미세조정

@docs/langgraphPlan2.md 


### 커서&코덱스 잔여작업 merge
1. 문서 현행화
- `langgraphPlan.md`의 `§2 현재 상태 요약`을 현재 구현 기준으로 수정
  - execute = LangChain tool loop
  - structured output = 적용 상태 및 남은 한계
  - HITL = 현재 방식(새 run 재개)과 정식 interrupt/resume 차이 명시
- Phase C, D 재검토 결과를 반영해 각 `점검 내용`과 결론 문구를 최종 확정

2. Phase D 정식화 여부 결정 및 구현
- 현재 HITL은 기능적으로 동작하지만, 정식 LangGraph `interrupt/resume + checkpointer`는 아님
- 최종 목표가 best practice라면 아래 방향으로 고도화 필요
  - same run/thread resume
  - checkpointer 도입
  - interrupt/resume replay 가능 구조 정리
- 만약 일정상 유지한다면, 문서에 “transitional design”으로 명확히 고정

3. 테스트 전략 구현
- 공식 문서/비교 문서 기준 테스트 항목을 실제 코드로 추가
  - graph unit test
  - tool schema contract test
  - interrupt/resume replay test
  - citation binding regression test
- 최소한 스켈레톤이라도 먼저 만들어서 이후 회귀 방지 기반 확보

4. Phase F 고도화
- 현재 retrieval은 기본 구현 완료 수준
- 남은 고도화
  - cross-encoder 또는 LLM rerank
  - evidence verification 독립 검증 계층화
  - retrieval quality 비교 모드
- 즉, “동작함”에서 “최고 수준 정확도”로 올리는 구간

5. Phase H 운영 관찰성 확장
- 현재는 run 단위 diagnostics 수준
- 남은 작업
  - diagnostics UI 시각화
  - run 간 비교
  - fallback / citation / HITL 비율 추세
  - 장기 시계열 관찰 지표

6. AI 워크스페이스 최종 UX 마감
- 발표/시연 관점에서 가장 중요한 화면
- 남은 작업
  - 에이전트 대화 스트림 가독성 정리
  - 사고 과정 요약 품질 개선
  - 케이스 선택/분석 동선 정리
  - 카드/패널 타이포/간격 마감

7. 에이전트 스튜디오 발표용 마감
- 그래프/스킬/프롬프트 구조는 있으나 설명력 보강 필요
- 남은 작업
  - 설명 문구 보강
  - 그래프 레전드/색상 의미 보강
  - 스킬 설명 밀도 조정
  - 실제 runtime 기준 정합성 최종 점검

8. 규정문서 라이브러리 / 청킹 실험실 마감
- 구조는 있으나 비교/설명력 보강 필요
- 남은 작업
  - 청킹 전략별 차이 시각화
  - 품질 설명 강화
  - retrieval 실험 결과 표현

9. 선택 과제
- checkpoint 저장 정식화 (`MemorySaver`, `SqliteSaver` 등)
- 정식 HITL same-run resume
- 운영 대시보드형 observability
- retrieval/rerank 고도화 심화

10. 권장 우선순위
- 즉시
  - 문서 현행화
  - Phase C/D 재검토 결과 반영
- 단기
  - Phase D 정식화 여부 결정
  - AI 워크스페이스 UX 마감
  - 테스트 전략 최소 구현
- 중기
  - Phase F/H 고도화
  - 에이전트 스튜디오/규정문서 라이브러리 발표용 마감

---

**잔여작업 1~10 반영 이력 (한 번에 진행)**  
- 1. 문서 현행화: `langgraphPlan.md` §2 현재 상태 요약 수정, Phase C/D 점검 결론 반영(완료 확정·Transitional HITL 명시).  
- 2. Phase D: 동 문서에 Phase D 결정 기준 참조 및 Transitional HITL 유지로 확정 문구 반영.  
- 3. 테스트 전략: `tests/test_graph.py`, `test_tool_schema.py`, `test_interrupt_resume.py`, `test_citation_binding.py` 스켈레톤 추가. (pytest 11 passed.)  
- 4. Phase F: `services/retrieval_quality.py` 추가(rerank/evidence 검증/비교 모드 스텁).  
- 5. Phase H: `ui/workspace.py` 결과 탭에 Run 진단(관찰 지표) expander 추가.  
- 6~8. UX 마감: 워크스페이스 에이전트 대화 캡션, 스튜디오 그래프 캡션, RAG 청킹 실험실 설명 보강.

---

**merge 검토 의견 (Cursor 기준)**  
- 커서 잔여작업 5개(문서 현행화, 점검 반영, 후속/선택, 테스트 전략, 우선순위)와 코덱스 의견(Phase D/F/H, 워크스페이스·스튜디오·라이브러리 마감, 권장 순서)이 누락 없이 반영됨.  
- Phase D는 “정식화 여부 결정”(항목 2)과 “선택 과제”(항목 9)에 중복 기재된 것이 아니라, 2=결정·구현 경로, 9=정식화를 하지 않을 때의 선택 과제로 구분되어 있어 일관적임.  
- 권장 우선순위(즉시·단기·중기)가 문서 현행화 → Phase D 결정·UX 마감·테스트 → F/H·발표 마감 순으로 정리되어 있어 타당함.  
- 수정: 제목 `###커서&코덱스` → `### 커서&코덱스` (마크다운 헤딩 공백).

### Phase D 결정 기준
- `정식 HITL`로 판정하는 조건
  - same run / same thread에서 재개된다.
  - LangGraph checkpointer가 존재한다.
  - `interrupt / resume` replay test가 통과한다.
  - UI와 diagnostics에서 “중단 후 같은 실행 재개”가 확인 가능하다.
- `Transitional HITL 유지`로 판정하는 조건
  - `resumed_run_id` 기반 새 run 재개를 유지한다.
  - `parent_run_id` lineage가 보존된다.
  - 문서(`langgraphPlan.md`, `langgraphPlan2.md`)에 “정식 LangGraph interrupt/resume 아님”을 명시한다.
  - replay test는 “새 run 재개 모델” 기준으로 분리 작성한다.
- 권장 판단
  - 목표가 `best practice`라면 `정식 HITL`을 목표 상태로 유지한다.
  - 발표/일정 우선이면 당장은 `Transitional HITL`을 유지하되, 문서와 점검 결과에 동일하게 명시한다.

---

### 잔여작업 1~10 점검 결과 (Codex)
1. 문서 현행화  
   - 결과: PASS  
   - 점검: `docs/langgraphPlan.md` §2 현재 상태 요약이 현재 구현 기준으로 수정되어 있음. execute=LangChain tool loop, structured output 적용 상태, HITL의 Transitional 방식 차이가 반영됨.

2. Phase D 정식화 여부 결정 및 구현  
   - 결과: PASS (Transitional 기준)  
   - 점검: `docs/langgraphPlan.md`, `docs/langgraphPlan2.md`에 Transitional HITL 유지로 문서화됨. `main.py`는 `resumed_run_id` 기반 새 run 재개를 사용하며, same-run interrupt/resume은 아님.  
   - 특이사항: best practice 관점의 정식 LangGraph interrupt/resume은 미적용.

3. 테스트 전략 구현  
   - 결과: PASS (기초 구현)  
   - 점검: `tests/test_graph.py`, `tests/test_tool_schema.py`, `tests/test_interrupt_resume.py`, `tests/test_citation_binding.py` 존재. 스켈레톤/기초 회귀 방지 기반은 확보됨.  
   - 특이사항: 테스트 밀도와 시나리오 확장은 추가 여지 있음.

4. Phase F 고도화  
   - 결과: PARTIAL  
   - 점검: hierarchical retrieval, query rewrite, citation binding은 존재하며 `services/retrieval_quality.py`도 추가됨.  
   - 특이사항: cross-encoder/LLM rerank는 아직 스텁 수준이고, evidence verification도 독립 검증 계층으로 완전 분리되지는 않음.

5. Phase H 운영 관찰성 확장  
   - 결과: PARTIAL  
   - 점검: `main.py`의 diagnostics API와 `services/run_diagnostics.py`, `ui/workspace.py`의 Run 진단 expander로 run 단위 관찰은 가능함.  
   - 특이사항: run 간 비교, 장기 시계열, 운영 대시보드형 observability는 아직 미구현.

6. AI 워크스페이스 최종 UX 마감  
   - 결과: PASS (발표용 기준)  
   - 점검: `ui/workspace.py`에 케이스/에이전트 대화/사고 과정/작업 계획/실행 로그/결과 구조가 분리되어 있으며, 발표용 동선은 성립함.  
   - 특이사항: 타이포/간격/시각 밀도는 추가 미세조정 가능.

7. 에이전트 스튜디오 발표용 마감  
   - 결과: PASS  
   - 점검: `ui/studio.py`에 활성 에이전트, 프롬프트, 런타임 도구, 연결 지식, 그래프/레전드/설명 문구가 존재함.  
   - 특이사항: 그래프 설명이나 시각 표현은 발표 맥락에 따라 추가 튜닝 가능.

8. 규정문서 라이브러리 / 청킹 실험실 마감  
   - 결과: PASS (기초 설명력 확보)  
   - 점검: `ui/rag.py`에 DB 라이브러리와 청킹 실험실이 분리되어 있고, 전략별 설명과 평균 길이/청크 수 비교 흐름이 있음.  
   - 특이사항: retrieval 결과 표현은 더 시각화할 수 있으나 현재도 기능상 설명은 가능함.

9. 선택 과제  
   - 결과: N/A  
   - 점검: 선택 과제는 필수 완료 항목이 아니며, checkpoint 정식화 / 정식 HITL / 운영 대시보드 / retrieval 심화는 후속 선택 작업으로 보는 것이 맞음.

10. 권장 우선순위  
   - 결과: PASS  
   - 점검: 문서에 즉시/단기/중기 우선순위가 정리되어 있으며, 현재 점검 결과와도 모순 없음.

---

**점검 결과 보완 의견 (Cursor)**  
- **4, 5 PARTIAL:** 잔여작업 1~10 범위에서는 “기본 구현/run 단위 관찰”까지를 완료 기준으로 둔 상태이므로, PARTIAL은 **의도된 결과**로 보는 것이 맞음. Phase F의 rerank·evidence 독립 계층·비교 모드, Phase H의 run 간 비교·장기 시계열·운영 대시보드는 merge 권장 우선순위상 **중기 과제**이므로, 당장 추가 작업 없이 PARTIAL 유지가 적절함.  
- **9 N/A:** 선택 과제는 필수 완료 항목이 아니므로 N/A 판정이 맞음.  
- **추가 작업 반영:** Phase H “run 간 비교”를 위해 `ui/workspace.py` 결과 탭에서 이력 중 다른 run을 선택 시 현재 run과 진단 지표를 나란히 비교할 수 있도록 최소 UI를 추가함.

---

### 고도화 구현 가능성 (2·4·5·8번)

아래는 잔여작업 2·4·5·8번에 대한 **고도화를 통한 구현 가능 여부**와 필요한 작업 범위 요약이다.

**2번 — LangGraph 정식 interrupt/resume**

- **가능 여부:** **가능함.** LangGraph는 `compile(checkpointer=...)`와 `interrupt_before`(또는 노드 내 `interrupt()`)를 지원하며, 동일 `thread_id`로 재호출 시 같은 run에서 재개된다.
- **필요 작업:**
  - `agent/langgraph_agent.py`: `build_agent_graph()`에서 `workflow.compile(checkpointer=...)` 사용. PoC는 `MemorySaver`, 운영은 `SqliteSaver` 등 영속 체크포인터 선택.
  - `verify` → `hitl_pause` 구간을 **정식 중단점**으로 두기: `interrupt_before(["hitl_pause"])` 또는 `hitl_pause` 노드 직전에 interrupt.
  - `main.py`: HITL 제출 시 **새 run 생성이 아니라** 기존 `run_id`(= thread_id)로 `graph.ainvoke(None, config={"configurable": {"thread_id": run_id}}, ...)` 형태로 재호출하여 같은 스레드에서 재개.
  - UI/diagnostics: "중단 후 같은 실행 재개" 문구 및 lineage 대신 thread_id 기반 재개 표시.
- **전제:** 현재 `resumed_run_id` 기반 새 run 설계를 **same-thread 재개**로 전환해야 하며, 스트리밍·결과 저장·API 경로(`/hitl` 응답 후 재개용 진입점) 정리가 필요함.

**4번 — cross-encoder/LLM rerank·evidence verification 독립 계층**

- **가능 여부:** **가능함.** `services/retrieval_quality.py`에 스텁이 있으므로, 여기에 실제 구현을 붙이면 된다.
- **필요 작업:**
  - **Rerank:** `rerank_with_cross_encoder()`에 cross-encoder 모델(sentence-transformers 등) 또는 LLM 기반 rerank API 연동. `policy_service.search_policy_chunks()` 또는 상위 호출부에서 1차 검색 결과를 받은 뒤 rerank 단계를 거치도록 파이프라인 확장.
  - **Evidence verification:** `verify_evidence_coverage()`를 독립 모듈로 분리(예: `services/evidence_verification.py`). 입력: 문장 목록 + 검색된 청크; 출력: covered/total, details. reporter의 citation binding 전/후에 이 계층을 호출하도록 연결.
  - **비교 모드:** `retrieval_quality_comparison()`에서 전략 A/B(예: rerank 유/무, chunk size 차이) 결과를 지표로 비교해 반환하고, 필요 시 UI(규정문서 라이브러리 또는 별도 실험 페이지)에서 시각화.
- **전제:** 모델/API 선택(로컬 cross-encoder vs LLM rerank), 지연·비용, 기존 `policy_rulebook_probe`와의 통합 지점 설계가 필요함.

**5번 — run 간 비교 정밀화·장기 시계열·운영 대시보드형 observability**

- **가능 여부:** **가능함.** 현재 run 단위 diagnostics API와 워크스페이스 Run 진단(2 run 비교) 기반을 확장하면 된다.
- **필요 작업:**
  - **run 간 비교 정밀화:** 이력 목록에서 다중 run 선택(2개 이상), 지표별 정렬/필터, 차이 하이라이트(증감 %, 이벤트 수 차이). `services/run_diagnostics.py`에 비교용 집계 함수 추가, API는 `/api/v1/cases/{id}/runs/compare?run_ids=...` 형태 확장 검토.
  - **장기 시계열:** `case_analysis_result` 또는 전용 집계 테이블에 run별 진단 스냅샷(날짜, tool 성공률, citation coverage, HITL 비율, fallback 비율 등)을 주기적으로 적재. 시계열 조회 API 제공 후, UI에서 기간·케이스별 추세 차트(선/막대) 표시.
  - **운영 대시보드:** 전용 페이지(예: `ui/dashboard.py`)에서 tenant/전체 기준으로 interrupt rate, citation coverage 추세, fallback 비율, run 수/성공률 등을 대시보드 형태로 표시. Streamlit으로 차트+테이블 구성 가능.
- **전제:** 시계열 저장을 위한 스키마/마이그레이션, 적재 주기(매 run 종료 시 또는 배치) 결정이 필요함.

**8번 — retrieval 결과 표현 시각화 고도화**

- **가능 여부:** **가능함.** `ui/rag.py`의 청킹 실험실·DB 라이브러리와 `policy_rulebook_probe` 검색 결과를 더 풍부하게 보여주면 된다.
- **필요 작업:**
  - **청킹 실험실:** 전략 비교를 현재 청크 수·평균 길이 외에, 조항별 분포(막대/파이), 청크 길이 히스토그램, overlap 비율 등으로 확장. `services/rag_chunk_lab_service.py`에서 전략별 메타(분포) 반환 후 UI에서 시각화(Streamlit chart).
  - **Retrieval 결과:** 분석 run에서 사용된 검색 결과(예: `policy_rulebook_probe`의 refs)를 규정문서 라이브러리 화면과 연동해 "이 run에서 인용된 청크"를 문서별·조항별로 하이라이트하거나, 쿼리–청크 매칭 점수/순위를 테이블·바 차트로 표시. (API에서 run_id 기준으로 인용 청크 목록 제공 필요.)
  - **품질 리포트:** 품질 리포트 탭을 JSON만이 아니라 coverage/노이즈/중복 비율을 게이지·막대 등으로 시각화.
- **전제:** retrieval 단계에서 점수/순위를 저장·전달하는 구조가 있으면 구현이 수월함. 없으면 `retrieval_quality.py` 고도화와 함께 스키마 확장 검토.

---

**요약**

| 항목 | 고도화로 구현 가능? | 비고 |
|------|---------------------|------|
| 2 LangGraph interrupt/resume | 예 | checkpointer + same thread_id 재호출로 전환 |
| 4 rerank·evidence 독립 계층 | 예 | retrieval_quality.py 실구현 + evidence 전용 모듈 |
| 5 run 비교·시계열·대시보드 | 예 | diagnostics 확장 + 시계열 저장 + 전용 대시보드 페이지 |
| 8 retrieval 시각화 | 예 | RAG UI에 차트·인용 맵·품질 게이지 추가 |
