## 커서 의견
1. 문서 정리 (우선 권장)
langgraphPlan.md §2 “현재 상태 요약”
작업: Phase A~D 반영한 현재 구현 상태로 문단 수정 (execute = LangChain tool 루프, structured output 적용, HITL = same-run interrupt/resume + checkpointer 적용).
2. 점검 재검토 결과 반영
Phase C·D “점검 내용 답변”
재검토 후 “완료”/“부분 완료” 등 최종 판단이 나오면, 해당 Phase의 점검 내용·결론 문구를 그에 맞게 수정.
3. 후속/선택 과제 (점검 특이사항 기준)
구분	내용	출처
Checkpoint	MemorySaver / SqliteSaver 등 LangGraph checkpoint 도입으로 “checkpoint 저장 방식 정리” 완료	Phase E 점검 내용
Retrieval 고도화	cross-encoder 또는 LLM rerank, evidence verification을 독립 검증 계층으로 분리	Phase F 점검 내용
Observability	run 단위가 아닌 운영 대시보드·장기 시계열 지표 (interrupt rate 집계 등)	Phase H 점검 내용
정식 HITL	같은 run 재개(interrupt + checkpointer) 적용 완료. 전환 검토 불필요.	Phase D 점검 내용 답변
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
- 정식 LangGraph interrupt/resume + checkpointer(MemorySaver) 적용 완료. same run_id/thread_id 재개, resumed_run_id는 동일 run_id 의미.
- 한계: persistent checkpointer(SqliteSaver 등) 미도입은 별도 후속 과제.
Phase F 고도화 잔여
현재 PASS지만 baseline 수준입니다.
남은 고도화:
cross-encoder 또는 LLM rerank
evidence verification 독립 계층화
retrieval quality 비교 모드
즉, 완료는 됐지만 “최고 수준”은 아직 아닙니다.
Phase H 운영 관찰성 확장
- run diagnostics 기반 최소 구현 완료. (운영형 observability 완성은 아님.)
- 남은 작업: diagnostics UI 고도화, run 간 비교, 장기 시계열, 운영 대시보드.

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

Phase D는 정식 same-run interrupt/resume + checkpointer 적용 완료. 남은 건 F/H 고도화와 화면 마감.
발표/시연 완성도 관점에서는 AI 워크스페이스 UX 마감
한 줄 요약

문서 기준으로는 대부분 끝났고,
진짜 남은 건
F/H 고도화
화면 마감
입니다.
제 권장 순서

AI 워크스페이스 최종 마감
F/H 고도화
스튜디오/라이브러리 발표용 미세조정

@docs/langgraphPlan2.md 


### 커서&코덱스 잔여작업 merge
1. 문서 현행화
- `langgraphPlan.md`의 `§2 현재 상태 요약`을 현재 구현 기준으로 수정
  - execute = LangChain tool loop
  - structured output = 적용 상태 및 남은 한계
  - HITL = same-run interrupt/resume + checkpointer 적용 명시
- Phase C, D 재검토 결과를 반영해 각 `점검 내용`과 결론 문구를 최종 확정

2. Phase D 현재 구현 상태 반영
- **적용 완료.** 정식 LangGraph `interrupt/resume + checkpointer`(MemorySaver) 적용. same run_id/thread_id 재개, `resumed_run_id`는 동일 run_id 의미.
- 한계: persistent checkpointer(SqliteSaver 등) 미도입은 별도 후속 과제로만 분리. 발표 전에는 "Transitional HITL" 표현 사용하지 않음.

3. 테스트 전략 구현
- 공식 문서/비교 문서 기준 테스트 항목을 실제 코드로 추가
  - graph unit test
  - tool schema contract test
  - interrupt/resume replay test
  - citation binding regression test
- 최소한 스켈레톤이라도 먼저 만들어서 이후 회귀 방지 기반 확보

4. Phase F 고도화
- 현재 retrieval은 baseline 완료 수준이며, 고급화는 잔여
- 남은 고도화
  - cross-encoder 또는 LLM rerank
  - evidence verification 독립 검증 계층화
  - retrieval quality 비교 모드
- 즉, “동작함”에서 “최고 수준 정확도”로 올리는 구간

5. Phase H 운영 관찰성 확장
- run diagnostics 기반 최소 구현 완료. (운영형 observability 완성은 아님.)
- 남은 작업: diagnostics UI 고도화, run 간 비교, 장기 시계열, 운영 대시보드

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
  - AI 워크스페이스 UX 마감
  - 테스트 전략 최소 구현
- 중기
  - Phase F/H 고도화
  - 에이전트 스튜디오/규정문서 라이브러리 발표용 마감
- (Phase D는 정식 적용 완료되어 단기 항목에서 제외)

---

**잔여작업 1~10 반영 이력 (한 번에 진행)**  
- 1. 문서 현행화: `langgraphPlan.md` §2 현재 상태 요약 수정, Phase C/D 점검 결론 반영(완료 확정·정식 HITL same-run + checkpointer 반영).
- 2. Phase D: 동 문서에 Phase D 현재 구현 상태 참조 및 정식 HITL(same-run interrupt/resume + checkpointer) 적용 완료 문구 반영.
- 3. 테스트 전략: `tests/test_graph.py`, `test_tool_schema.py`, `test_interrupt_resume.py`, `test_citation_binding.py` 스켈레톤 추가. (pytest 11 passed.)  
- 4. Phase F: `services/retrieval_quality.py` 추가(rerank/evidence 검증/비교 모드 스텁).  
- 5. Phase H: `ui/workspace.py` 결과 탭에 Run 진단(관찰 지표) expander 추가.  
- 6~8. UX 마감: 워크스페이스 에이전트 대화 캡션, 스튜디오 그래프 캡션, RAG 청킹 실험실 설명 보강.

---

**merge 검토 의견 (Cursor 기준)**  
- 커서 잔여작업 5개(문서 현행화, 점검 반영, 후속/선택, 테스트 전략, 우선순위)와 코덱스 의견(Phase D/F/H, 워크스페이스·스튜디오·라이브러리 마감, 권장 순서)이 누락 없이 반영됨.  
- Phase D는 항목 2에서 “현재 구현 상태 반영”(정식 적용 완료)으로 정리되었고, 선택 과제(9)는 persistent checkpointer 등 후속만 해당함.  
- 권장 우선순위(즉시·단기·중기)가 문서 현행화 → UX 마감·테스트 → F/H·발표 마감 순으로 정리되어 있으며, Phase D는 적용 완료로 단기 항목에서 제외됨.  
- 수정: 제목 `###커서&코덱스` → `### 커서&코덱스` (마크다운 헤딩 공백).

### Phase D 현재 구현 상태
- **정식 HITL 적용 완료.** 아래 조건 충족.
  - same run / same thread에서 재개된다.
  - LangGraph checkpointer(MemorySaver)가 존재한다.
  - UI와 diagnostics에서 "중단 후 같은 실행 재개"가 확인 가능하다.
  - `resumed_run_id`는 동일 run_id를 의미한다.
- **한계:** persistent checkpointer(SqliteSaver 등) 미도입은 별도 후속 과제. 발표/문서에서는 "Transitional HITL" 표현을 사용하지 않고, "정식 same-run interrupt/resume 적용, 한계는 persistent checkpointer 미도입"으로 통일한다.

---

### 잔여작업 1~10 점검 결과 (Codex)
1. 문서 현행화  
   - 결과: PASS  
   - 점검: `docs/langgraphPlan.md` §2 현재 상태 요약이 현재 구현 기준으로 수정되어 있음. execute=LangChain tool loop, structured output 적용 상태, HITL=same-run interrupt/resume + checkpointer 반영됨.

2. Phase D 현재 구현 상태 반영  
   - 결과: PASS (정식 기준)  
   - 점검: `docs/langgraphPlan.md`, `docs/langgraphPlan2.md`에 정식 HITL(same-run interrupt/resume + checkpointer) 적용 완료로 문서화됨. `main.py`는 동일 `run_id`로 `_run_analysis_task(..., resume_value=...)` 호출하여 같은 run에서 재개.  
   - 특이사항: 없음. 정식 LangGraph interrupt/resume + checkpointer 적용 상태.

3. 테스트 전략 구현  
   - 결과: PASS (기초 구현)  
   - 점검: `tests/test_graph.py`, `tests/test_tool_schema.py`, `tests/test_interrupt_resume.py`, `tests/test_citation_binding.py` 존재. 스켈레톤/기초 회귀 방지 기반은 확보됨.  
   - 특이사항: 테스트 밀도와 시나리오 확장은 추가 여지 있음.

4. Phase F 고도화  
   - 결과: PARTIAL (baseline 완료, 고급화 잔여)  
   - 점검: hierarchical retrieval, query rewrite, citation binding은 존재하며 `services/retrieval_quality.py`도 추가됨. baseline 완료 수준.  
   - 특이사항: cross-encoder/LLM rerank는 아직 스텁 수준이고, evidence verification도 독립 검증 계층으로 완전 분리되지는 않음. 고급화는 잔여.

5. Phase H 운영 관찰성 확장  
   - 결과: PARTIAL (run diagnostics 기반 최소 구현 완료. 운영형 observability 아님.)  
   - 점검: `main.py`의 diagnostics API와 `services/run_diagnostics.py`, `ui/workspace.py`의 Run 진단 expander로 run 단위 관찰은 가능함.  
   - 특이사항: run 간 비교, 장기 시계열, 운영 대시보드형 observability는 미구현(후속 과제).

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

- **현재 구현 상태:** **적용 완료.** `compile(checkpointer=MemorySaver())`, 노드 내 `interrupt()` 호출, 동일 `run_id`/`thread_id`로 재호출 시 같은 run에서 재개. `resumed_run_id`는 동일 run_id를 반환.
- **구현 내용:** `verify` → HITL 필요 시 `hitl_pause` 노드에서 `interrupt(hitl_request)` → `main.py`의 `POST .../hitl`은 새 run 생성 없이 동일 run_id로 `_run_analysis_task(..., resume_value=...)` 호출하여 같은 run에서 재개.
- **한계:** PoC는 `MemorySaver` 사용. persistent checkpointer(SqliteSaver 등) 미도입은 별도 후속 과제.

**4번 — cross-encoder/LLM rerank·evidence verification 독립 계층**

- **가능 여부:** **가능함.** `services/retrieval_quality.py`에 스텁이 있으므로, 여기에 실제 구현을 붙이면 된다.
- **필요 작업:**
  - **Rerank:** `rerank_with_cross_encoder()`에 실제 모델 연동. **구현 우선순위:** 1차는 **cross-encoder rerank**(sentence-transformers 등), 2차는 **LLM rerank 옵션화**. 비용·지연·재현성 측면에서 1차는 cross-encoder가 더 안정적이다. `policy_service.search_policy_chunks()` 또는 상위 호출부에서 1차 검색 결과를 받은 뒤 rerank 단계를 거치도록 파이프라인 확장.
  - **Evidence verification:** `verify_evidence_coverage()`를 독립 모듈로 분리(예: `services/evidence_verification.py`). 입력: 문장 목록 + 검색된 청크; 출력: covered/total, details. reporter의 citation binding 전/후에 이 계층을 호출하도록 연결. **게이트 정책 명시 필요:** verification 결과가 reporter **이전** 게이트에 영향을 주는지 정해야 한다. coverage 부족 시 **hold / caution / regenerate citations** 중 어느 정책을 택할지 문서·코드에 명시할 것. 명시하지 않으면 verification이 진단용으로만 끝날 수 있다.
  - **비교 모드:** `retrieval_quality_comparison()`에서 전략 A/B(예: rerank 유/무, chunk size 차이) 결과를 지표로 비교해 반환하고, 필요 시 UI(규정문서 라이브러리 또는 별도 실험 페이지)에서 시각화.
- **전제:** 모델/API 선택(1차 cross-encoder, 2차 LLM 옵션), 지연·비용, 기존 `policy_rulebook_probe`와의 통합 지점 설계가 필요함.

**5번 — run 간 비교 정밀화·장기 시계열·운영 대시보드형 observability**

- **가능 여부:** **가능함.** 현재 run 단위 diagnostics API와 워크스페이스 Run 진단(2 run 비교) 기반을 확장하면 된다.
- **필요 작업:**
  - **run 간 비교 정밀화:** 이력 목록에서 다중 run 선택(2개 이상), 지표별 정렬/필터, 차이 하이라이트(증감 %, 이벤트 수 차이). `services/run_diagnostics.py`에 비교용 집계 함수 추가, API는 `/api/v1/cases/{id}/runs/compare?run_ids=...` 형태 확장 검토.
  - **장기 시계열:** `case_analysis_result` 또는 전용 집계 테이블에 run별 진단 스냅샷(날짜, tool 성공률, citation coverage, HITL 비율, fallback 비율 등) 적재. **적재 단위 우선순위:** 1차는 **run 종료 시 snapshot 1건 적재**로 고정하고, 배치/사후 집계는 나중에 추가. 이 우선순위를 문서에 두면 구현이 덜 흔들린다. 시계열 조회 API 제공 후, UI에서 기간·케이스별 추세 차트(선/막대) 표시.
  - **운영 대시보드:** 전용 페이지(예: `ui/dashboard.py`)에서 tenant/전체 기준으로 interrupt rate, citation coverage 추세, fallback 비율, run 수/성공률 등을 대시보드 형태로 표시. Streamlit으로 차트+테이블 구성 가능.
- **전제:** 시계열 저장을 위한 스키마/마이그레이션, 1차는 “run 종료 시 1건”으로 적재 주기 고정.

**8번 — retrieval 결과 표현 시각화 고도화**

- **가능 여부:** **가능함.** `ui/rag.py`의 청킹 실험실·DB 라이브러리와 `policy_rulebook_probe` 검색 결과를 더 풍부하게 보여주면 된다.
- **저장 구조 선결:** run에서 사용된 검색 결과를 **어떻게 저장할지**가 시각화의 핵심이다. **권장:** retrieval candidate list(top-k before/after rerank)와 **adopted citations**를 **별도 payload로 저장**할 것. 그래야 시각화를 나중에 붙이는 것이 아니라, 처음부터 재현 가능한 자료가 된다.
- **필요 작업:**
  - **청킹 실험실:** 전략 비교를 현재 청크 수·평균 길이 외에, 조항별 분포(막대/파이), 청크 길이 히스토그램, overlap 비율 등으로 확장. `services/rag_chunk_lab_service.py`에서 전략별 메타(분포) 반환 후 UI에서 시각화(Streamlit chart).
  - **Retrieval 결과:** 분석 run에서 사용된 검색 결과(예: `policy_rulebook_probe`의 refs)를 규정문서 라이브러리 화면과 연동해 "이 run에서 인용된 청크"를 문서별·조항별로 하이라이트하거나, 쿼리–청크 매칭 점수/순위를 테이블·바 차트로 표시. (위 저장 payload 기반으로 run_id 기준 인용·candidate 목록 API 제공.)
  - **품질 리포트:** 품질 리포트 탭을 JSON만이 아니라 coverage/노이즈/중복 비율을 게이지·막대 등으로 시각화.
- **전제:** retrieval candidate list + adopted citations를 별도 payload로 저장하는 구조를 먼저 도입하면, 이후 시각화·`retrieval_quality.py` 고도화가 수월함.

---

**요약**

| 항목 | 고도화로 구현 가능? | 비고 |
|------|---------------------|------|
| 2 LangGraph interrupt/resume | 예 | checkpointer + same thread_id 재호출, interrupt 방식 단일화(`interrupt_before`), run_id/thread_id 기준 고정 |
| 4 rerank·evidence 독립 계층 | 예 | 1차 cross-encoder rerank → 2차 LLM 옵션, evidence 전용 모듈 + coverage 부족 시 게이트 정책 명시 |
| 5 run 비교·시계열·대시보드 | 예 | diagnostics 확장 + run 종료 시 snapshot 1건 적재(배치/집계는 후순위) + 전용 대시보드 페이지 |
| 8 retrieval 시각화 | 예 | candidate list·adopted citations 별도 payload 저장 선행, RAG UI에 차트·인용 맵·품질 게이지 추가 |

- **우선 구현 순서 권장:** **2 → 4 → 5 → 8.** 2가 끝나야 실행 구조가 안정되고, 4가 정확도 핵심이며, 5·8은 관찰과 시연 강화에 해당한다.
- **보완 시 문서가 더 단단해지는 6가지:** (1) interrupt 방식 단일화, (2) run_id/thread_id 기준 고정, (3) rerank 1차 우선순위(cross-encoder), (4) verification 결과의 게이트 정책(hold/caution/regenerate), (5) run 종료 시 snapshot 1건 적재, (6) retrieval candidate list·adopted citations 별도 payload 저장.
