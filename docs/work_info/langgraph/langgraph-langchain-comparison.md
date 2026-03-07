# AuraAgent LangGraph / LangChain 공식 기준 문서

> **공식 문서**: 이 문서는 AuraAgent 프로젝트의 LangGraph·LangChain **현재 진단**, **방향성 비교**, **Codex 검토 결론**, **구현 체크리스트**를 통합한 단일 기준 문서입니다.  
> 기존 진단 문서·기준서·비교 문서는 이 문서로 통합되었으며, 해당 개별 문서는 제거 예정입니다.  
> **구현 시 "꼭 필요한 것(Must)"은 빠지면 안 되며, 로직 변경 시 Streamlit UI 대응이 필수입니다.** Should/Later는 단계·순서에 따라 진행하고, 리스크 있거나 당장 불필요한 동시 전면 적용은 하지 않습니다.

---

## 1. 결론 (요약)

| 항목 | 답 |
|------|-----|
| **Cursor 진단과 Codex 기준서 방향이 같은가?** | **예. 같은 방향이다.** |
| **합치면 최고 결과물로 가는가?** | **예.** 진단(현재 상태) + 목표 구조 + 구현 우선순위를 한 문서에서 관리하는 것이 가장 명확한 로드맵이 됨. |
| **한 번에 전면 적용해도 되는가?** | **아니오.** Codex 검토: 단계적 리팩토링이 필수이며, 동시 전면 교체는 PoC 리스크가 큼. |

---

## 2. 같은 방향인지 — 항목별 매핑

진단(Cursor)과 기준서(Codex)가 지적하는 **같은 문제**와 **같은 해결 방향**을 정리하면 아래와 같다.

| 주제 | 진단 측 지적 | 기준서 측 목표 | 일치 |
|------|--------------|----------------|------|
| **LangGraph 현재 상태** | 선형 플로우·스트리밍은 OK, 조건 분기·체크포인트·HITL 그래프화 부족 | graph는 있으나 execute가 registry 직접 호출, interrupt/resume 미정식화 | ✓ |
| **HITL** | `interrupt_before`/human 노드 없음, verify 내부에서만 `hitl_request` | verifier에 interrupt/resume 정식 배치, human input → resume 후 reporter | ✓ |
| **Tool** | `@tool`/StructuredTool/bind_tools 미사용, SKILL_REGISTRY 직접 호출 | tool은 LangChain `@tool`, Execute는 ToolNode 기반 | ✓ |
| **Structured output** | (진단에서 간략 언급) | planner/critic/verifier/reporter는 반드시 structured output | ✓ |
| **체크포인트** | MemorySaver/SqliteSaver 미사용 | persistence = checkpoint + event log + final result 분리 | ✓ |
| **LangChain 역할** | LCEL·Tools·Runnable 미사용, LLM 외부 의존 | LangChain = 모델/tool/structured output 계층 | ✓ |
| **스트리밍** | (진단에서 상세하지 않음) | orchestration stream vs reasoning note stream 분리 | ✓ |

즉, **"뭐가 부족한지"와 "어느 쪽으로 가야 하는지"가 같은 선 위에 있다.**

---

## 3. 역할 분담 (이 문서 내 구성)

- **진단** — "지금 코드 기준으로 맞다/아니다" 판정 + 소스 코드 링크로 빠르게 확인.
- **목표 구조·기준서** — "정석 적용"을 위한 구현 기준: 목표 구조, state 설계, 안티패턴, RAG, MCP, Phase A~E, **파일별 변경 대상(UI 포함)**.
- **비교·결론** — 두 관점의 관계 정리 + Codex 검토 결론으로 "방향 유지 + 단계적 적용" 명시.

---

## 4. 통합 문서 사용 원칙

- **이 문서 = 단일 소스 오브 트루스** — LangGraph/LangChain 관련 "현재 평가", "목표 구조", "우선순위", "검토 결론", **구현 체크리스트**는 이 문서를 따른다.
- **구현 시** — **Section 8 구현 체크리스트**에서 **Must** 항목을 빠짐없이 반영하고, **Section 9 Streamlit UI 대응**을 함께 수행한다. Should/Later는 4.1 순서에 따른다.
- **발표·PoC 안정성** — "최고 기술" 방향은 유지하되, 당장 허용되는 transitional design을 명시한다. (예: ToolNode 전환 전까지 registry dispatcher 허용, 단 tool schema 문서화는 선행.)

### 4.1 꼭 필요한 것 vs 권장 vs 이후 (Must / Should / Later)

구현 항목은 **꼭 필요한 것**만 Must로 하고, 단계적으로 필요한 것은 Should, 당장 불필요하거나 리스크 있는 것은 Later로 둔다. 리스크 있거나 불필요한 내용을 동시에 전면 적용하지 않는다.

| 구분 | 의미 | 해당 항목 예 |
|------|------|----------------|
| **Must** | 빠지면 안 됨. 정석 적용·PoC 완결에 필수. | tool schema 정식화, **로직 변경 시 Streamlit UI 대응**, 안티패턴 10개 금지 준수, Phase A→B→C 적용 순서 준수, orchestration vs reasoning stream 구분 |
| **Should** | 단계적으로 진행. 순서 지키면 리스크 낮음. | planner/critic/verifier/reporter structured output, ToolNode 전환, verifier interrupt/resume, checkpoint·event log·final result 분리, ui/workspace.py 스트림 분리 표시 |
| **Later** | 당장 불필요하거나 전면 적용 시 리스크 큼. 동시에 하지 않음. | MCP 도입, Phase E(observability/LangSmith), 장기 vector DB 교체(Qdrant 등), retrieval 고도화(rerank·evidence verification)는 graph 정식화 이후 |

**이 문서는 Must를 기준으로 "꼭 필요한 것"을 명시하고, Should/Later는 단계·순서에 따라 진행하도록 구성했다.** 언제 끝난 것으로 볼지는 아래 4.2 완료 기준(acceptance criteria)을 따른다.

### 4.2 Phase별 완료 기준 (Acceptance Criteria)

각 Phase마다 아래 기준을 충족해야 해당 Phase를 완료한 것으로 본다. 이게 없으면 구현이 끝없이 퍼진다. 수치형 목표는 검증 가능한 최소 기준이다.

| Phase | 완료 기준 | 예상 변경 파일 |
|-------|-----------|----------------|
| **Phase A** | skills.py의 모든 실행 capability가 LangChain tool schema를 가짐 (이름·입력/출력 schema·설명·docstring) | agent/skills.py, (schema 정의 시 agent/schemas.py 등) |
| **Phase B** | execute에서 registry 직접 호출 제거, ToolNode 또는 tool-calling loop로 도구 실행 | agent/langgraph_agent.py |
| **Phase C** | verifier가 interrupt/resume로 HITL 수행 (저장/재개 설계 포함). **수치**: interrupt 후 resume 성공률 측정 가능(목표 예: 95% 이상) | agent/langgraph_agent.py, agent/hitl.py, persistence layer |
| **Phase D** | sentence-level citation binding 적용. **수치**: citation coverage 측정 가능(목표 예: 90% 이상) | services/policy_service.py, agent/langgraph_agent.py(reporter) |
| **Phase E** | observability pipeline에서 graph run·tool call·interrupt/resume 추적 가능, demo/production 분리 | config·tracing 연동, (선택) LangSmith 등 |

### 4.3 Transitional design 허용/금지 범위

개발 중 흔들리지 않도록 **현재 허용**과 **현재 금지**를 표로 분리한다. **종료 조건**: 해당 transitional이 끝나는 시점을 명시해, 영구 허용으로 읽히지 않게 한다.

| 구분 | 내용 | 종료 조건 |
|------|------|-----------|
| **현재 허용** | SKILL_REGISTRY 유지 가능, 단 tool schema 문서화 선행 | Phase A 완료 후 tool object로 전환 시 registry는 tool 등록용으로만 사용 |
| **현재 금지** | 신규 기능을 registry direct call 방식으로 추가 | — |
| **현재 허용** | 기존 reasoning note 유지 | — |
| **현재 금지** | raw chain-of-thought 노출 | — |
| **현재 허용** | ToolNode 전환 전까지 execute에서 plan 배열 기반 dispatch | **ToolNode 전환 완료 시 registry direct dispatch 제거** |
| **현재 금지** | planner/critic/verifier/reporter가 스키마 없이 자유문장만 반환하는 신규 코드 | — |

---

## 5. 한 줄 요약

- **방향성**: 진단과 기준서는 같다.
- **합침**: 이 문서에서 진단 + 목표 + 비교 + Codex 결론 + **전체 구현 항목**을 한 번에 관리.
- **적용**: 방향은 유지, 구현은 **단계적**으로 진행하며, **로직 변경 시 Streamlit UI 대응 필수**.

---

## 6. Codex 위 내용 확인 후 결론

### 6.1 결론

- 방향은 맞다. Cursor 진단 + Codex 기준서 + 비교 문서 조합은 시너지가 있다.
- 즉시 전면 교체가 아니라 **단계적 리팩토링**으로 가야 리스크가 낮다.
- 기술적으로 "어떤 AI가 봐도 정석적"이라는 목표에 가장 가까운 방향이다.

### 6.2 좋은 점

- 역할 분리 명확 (진단 / 목표·원칙 / 비교).
- 핵심 기술 판단: MCP가 아니라 LangGraph + LangChain; MCP는 외부 도구 표준화 시 adapter만.
- 현재 코드 부족 지점 정확: SKILL_REGISTRY 직접 호출, ToolNode 미사용, structured output 부족, HITL interrupt/resume 미정식화.
- 목표 구조 현실적: planner/critic/verifier/reporter structured output, ToolNode, interrupt/resume, orchestration vs reasoning stream 분리.

### 6.3 리스크

- 한 번에 다 바꾸면 PoC가 깨질 가능성 큼 (execute→ToolNode, interrupt/resume, structured output 동시 전환 시).
- 문서 기준이 이상적이라 PoC 목적과 충돌 가능 → 적용 순서는 보수적으로.
- LangChain 정식화는 tool + model + structured output **세트**로 봐야 함.

### 6.4 Codex 권장 구현 순서

1. LangChain tool 정식화 (`skills.py` → `@tool` 또는 동등)
2. planner / critic / verifier / reporter structured output
3. execute를 ToolNode 또는 tool-calling loop로 전환 (1, 2 완료 후)
4. verifier interrupt / resume (저장/재개 설계와 함께)
5. retrieval / rerank / evidence verification (graph 정식화 이후)

### 6.5 추가 보완 권장

- 이 문서를 소스 오브 트루스로 사용.
- Must / Should / Later 우선순위 명시.
- "발표 전 허용 transitional design" 명시 (예: ToolNode 전환 전까지 registry dispatcher 허용, tool schema 문서화 선행).

### 6.6 한 줄 결론 (Codex)

| 항목 | 내용 |
|------|------|
| 문서 방향 | 맞음 |
| 기술 수준 | 높음 |
| 리스크 관리 | **단계적 적용이 필수** |

---

## 7. Cursor 진단 상세 (코드 링크)

> 구현 시 "현재 무엇이 부족한지"를 코드 위치와 함께 확인할 때 사용한다.

### 7.1 종합 판정

| 구분 | 판정 | 요약 |
|------|------|------|
| **LangGraph** | △ 부분적 | 그래프 구조·스트리밍은 정석에 가깝고, 조건 분기·체크포인트·HITL 그래프화는 미적용 |
| **LangChain** | ✗ 아니다 | LCEL·Tools·Runnable 체인 미사용, LLM은 외부 클라이언트 의존 |
| **전체** | 아니다 | 핵심 기술을 "모범적·정석" 수준으로 적용했다고 보기 어렵다 |

### 7.2 LangGraph — 맞다 (잘 되어 있는 부분)

- **StateGraph + TypedDict 상태** — [AgentState](../agent/langgraph_agent.py#L16) 명시적 스키마 사용.
- **노드·엣지 정의** — add_node 8개, add_edge(START → … → END) 선형 흐름 명확.
- **비동기 스트리밍** — graph.astream(initial_state, stream_mode="updates", config=config) 사용.
- **설계 의도** — analyze → plan → execute → critique → verify → report → finalize 단계가 코드와 일치.

### 7.3 LangGraph — 아니다 (모범 사례 대비 부족)

- **조건부 엣지 없음** — add_conditional_edges 미사용. verify → (HITL 시 human / 아니면 reporter) 분기가 그래프에 없음.
- **HITL이 그래프에 반영되지 않음** — interrupt_before / interrupt_after 또는 "human" 노드 없음. HITL은 verify_node 내부에서 hitl_request만 채우고 그래프는 그대로 진행.
- **체크포인트·영속화 미사용** — MemorySaver / SqliteSaver 등 미적용. 재개·롤백·디버깅을 그래프 수준에서 활용하지 않음.
- **서브그래프·툴 노드 미사용** — execute 안 스킬들이 LangGraph 툴 노드로 정의되지 않고 일반 파이썬 함수 호출.

### 7.4 LangChain — 맞다

- 의존성: langchain, langchain-core, langgraph 명시.
- 콜백 연동: Langfuse CallbackHandler를 config에 넣어 스트리밍 실행에 전달 가능.

### 7.5 LangChain — 아니다

- **LLM·Runnable 계약** — 실제 호출은 core.llm.client.get_llm_client() (Aura 플랫폼 경로). LangChain 모델 객체가 이 레포 안에 직접 선언되는 것이 이상적이지만, **최소 기준**은 LangChain Runnable / structured output / tool-calling contract를 프로젝트 내부에서 명시적으로 보장하는 것이다. 외부 wrapper가 있더라도 그 계약을 유지하면 허용 가능하다. 현재는 그 보장이 명시되지 않음.
- **LCEL 미사용** — `|` 파이프, RunnableSequence, RunnablePassthrough 등 체인 조합 없음.
- **Tools 미사용** — 스킬이 @tool / StructuredTool / bind_tools 로 정의되지 않음. 일반 async 함수 + SKILL_REGISTRY 딕셔너리 방식.

### 7.6 모범 사례에 가깝게 만들려면 (Cursor 제안)

- **LangGraph**  
  - add_conditional_edges("verify", ...) 로 HITL 시 human 노드/인터럽트 분기 추가  
  - 체크포인트 저장소 적용 (MemorySaver 등)  
  - 필요 시 execute 내부를 서브그래프 또는 툴 노드로 표현  
- **LangChain**  
  - 모델: 레포 내에 ChatOpenAI(또는 동등)를 두는 것이 이상적이나, 최소한 **LangChain Runnable / structured output / tool-calling contract**를 내부에서 명시적으로 보장하면 됨 (외부 wrapper 사용 시에도 계약 유지).  
  - 스킬을 @tool / StructuredTool로 정의한 뒤 bind_tools + tool_choice 로 execute와 연동.  
  - 노트/요약 생성 등을 LCEL 체인(`|`)으로 구성.  

---

## 8. 구현 체크리스트 (Codex + Cursor)

> 아래 항목은 Cursor 진단·Codex 기준서에서 도출한 구현 대상이다. **Must(꼭 필요)** 는 빠지면 안 되고, **Should** 는 권장 순서대로, **Later** 는 이후 단계로 적용한다 (Section 4.1 참고). 리스크 있거나 당장 불필요한 동시 전면 적용은 하지 않는다.

### 8.1 목표 구조 (8항)

1. LangGraph = 상태 그래프와 실행 제어  
2. LangChain = 모델, tool, structured output  
3. Tool = `@tool` 또는 동등한 LangChain tool 객체  
4. Execute = ToolNode 기반 tool-calling loop  
5. Planner / Critic / Verifier / Reporter = structured output 기반 노드  
6. Verifier = interrupt / resume로 HITL 수행  
7. Streaming = node event + token stream 분리  
8. Persistence = checkpoint + final result + event log 분리 저장  

### 8.2 구현 원칙

- **오케스트레이션과 capability 분리**  
  - 금지: node 안에서 모든 비즈니스 로직 직접 처리, node가 모델 호출·도구 호출·점수·저장을 한 번에 처리.  
  - 권장: node = 의사결정 단위, tool = 실행 capability, persistence = 저장 계층, **UI = 관찰/제어 계층**.
- **tool은 LangChain tool로 정의**  
  - 명확한 이름(snake_case), 타입힌트, 입력 schema, 설명(docstring), tool result schema.  
  - 예: holiday_compliance_probe, budget_risk_probe, merchant_risk_probe, document_evidence_probe, policy_rulebook_probe, legacy_aura_deep_audit.  
  - 공식: @tool, concise description, typed args, schema clarity.
- **node는 structured output 우선**  
  - planner, critic, verifier, reporter는 반드시 structured output (아래 스키마 예시 참고). 자유문장만 반환 금지.

**Structured output 스키마 예시 (구현 시 참고)**  
- **PlannerOutput**: PlanStep(tool_name, purpose, required, skip_condition), PlannerOutput(objective, steps, stop_after_sufficient_evidence, tool_budget, rationale).  
- **CriticOutput**: overclaim_risk, contradictions, missing_counter_evidence, recommend_hold, rationale.  
- **VerifierOutput**: grounded, needs_hitl, missing_evidence, gate(READY|HITL_REQUIRED|REJECTED), rationale.  
- **ReporterOutput**: Citation(chunk_id, article, title), ReporterSentence(sentence, citations), ReporterOutput(summary, verdict, sentences).

### 8.3 LangGraph 정식 적용

- **메인 그래프 노드**  
  screener, intake, planner, tool_router / tool_node, critic, verifier, reporter, finalizer.  
  현재 execute 단일 노드는 중간 단계. 최종은 (A) planner → tools_condition → ToolNode → critic → verifier 또는 (B) planner → execute_loop subgraph → ToolNode → critic → verifier.
- **tool 실행**  
  ToolNode가 tool call 실행, 결과가 state에 합쳐짐. plan 배열 순회·registry 직접 호출은 transitional design.
- **interrupt / resume**  
  verifier에 배치. evidence 부족, contradiction, policy conflict, 고위험 자동조치 전 → interrupt, human input request 생성, resume 후 reporter 재실행.

### 8.4 State 설계

- state는 작고 명확. 프롬프트용 전체 텍스트 덩어리를 state에 무분별하게 넣지 않음.
- 권장: case_id, body_evidence, screening_result, normalized_signals, planner_output, tool_messages, tool_results, critic_output, verifier_output, hitl_request, hitl_response, reporter_output, final_result 등.
- 원칙: **state = 실행 상태**, **DB payload = 영속 데이터**, **UI payload = 표시용 projection** — 세 개를 섞지 않음.

### 8.5 Streaming

- **두 종류 스트림 분리**  
  - A. orchestration stream: node start/end, tool call/result, gate applied, interrupt, resume (운영/가시성/감사용).  
  - B. model reasoning note stream: 공개 가능한 작업 메모, 현재 무엇을 보고 있는지, 왜 다음 tool을 쓰는지, 무엇이 확보되었는지 (사용자 경험용).
- **raw chain-of-thought 노출 금지** — 공개 가능한 reasoning note를 구조화해서만 보여줌 (message, thought, action, observation 등).
- **UI** — 라이브 패널(에이전트 대화 = 라이브 reasoning note / tool trace)과 리뷰 패널(사고 과정 = 실행 후 구조화 리뷰) 분리. 데이터 소스를 명확히 분리.

### 8.6 RAG / Retrieval

- 단순 chunk 검색으로 끝내지 않음. 순서: signal extraction → query rewriting → hierarchical retrieval → reranking → citation binding → evidence verification.
- Chunk 전략: 조항 단위 parent chunk, 세부 문장/항 단위 child chunk, parent-child link, article/clause/title/effective range 메타, retrieval은 child 우선·설명은 parent context 포함.
- 검색 단계: query rewrite(risk type, mcc, hr status, occurredAt, document evidence) → candidate retrieval(lexical/BM25, vector, metadata filter) → hybrid merge → rerank(cross-encoder 또는 LLM rerank) → citation binding → verification(grounded coverage, mismatch detection).
- Vector DB: 현재 단계는 pgvector 유지, **retrieval 계층은 추상화 유지**가 핵심. 장기 교체는 Later 단계에서 검토.

### 8.7 MCP

- MCP는 이 프로젝트 필수 기술이 아니다. 내부 capability = LangChain tool, 외부 프로토콜로 노출할 도구 = MCP tool. 필요 시 LangChain tool이 상위이고 MCP는 tool 내부 transport로만 사용한다.

### 8.8 현재 코드 기준 변경 대상 (파일별) — **UI 포함**

| 파일/계층 | 현재 상태 | 목표 |
|-----------|-----------|------|
| **agent/skills.py** | 내부 registry 기반 capability | LangChain @tool 또는 tool object 전환, 입력/출력 schema 정식화 |
| **agent/langgraph_agent.py** | graph 존재, execute가 registry 직접 호출, node 메모 일부 자체 구성 | planner structured output, ToolNode 또는 tool-calling loop, critic/verifier/reporter structured output, interrupt/resume 정식화 |
| **agent/reasoning_notes.py** | 공개용 작업 메모 생성 | chain-of-thought 대체용 sanctioned reasoning notes 유지, token streaming 가능하면 partial note 지원 |
| **ui/workspace.py** | (현재 구조 유지) | **orchestration stream과 reasoning note stream 분리**, 사고 과정은 structured review만 보여줌, tool trace는 별도 execution log로 유지 |
| **ui/studio.py, 기타 Streamlit UI** | (현재 구조 유지) | **에이전트/백엔드 로직 변경 시 표시·이벤트·그래프·프롬프트 등 UI 전반이 새 구조에 맞게 대응** (Section 9 참고) |
| **persistence layer** | (현재 구조) | checkpoint, event log, final result 세 축 분리 |

### 8.9 안티패턴 (10개) — 금지

1. LangGraph node 내부에서 직접 DB 저장까지 수행  
2. tool을 함수 레지스트리만으로 숨기고 schema를 노출하지 않음  
3. planner/critic/verifier/reporter가 자유문장만 반환  
4. execute 노드가 모든 실행 세부 로직을 독점  
5. raw chain-of-thought를 그대로 UI에 노출  
6. sentence-level citation 없이 최종 결론 생성  
7. retrieval 결과를 rerank 없이 바로 신뢰  
8. HITL을 단순 버튼 클릭 수준으로 두고 interrupt/resume 구조를 쓰지 않음  
9. MCP tool과 internal skill/tool 개념을 혼동  
10. UI 이벤트용 문구를 하드코딩하고 "실제 에이전트 사고"처럼 설명  

### 8.10 Phase A～E (상세)

각 Phase의 **완료 기준(acceptance criteria)** 은 Section 4.2 참고.

- **Phase A — 정식 tool layer**  
  1) skills.py → LangChain tool 전환  
  2) 입력/출력 schema 정식화  
  3) tool result 공통 envelope 정의  

- **Phase B — graph 정식화**  
  1) planner structured output  
  2) ToolNode 기반 execute loop  
  3) critic/verifier/reporter structured output  

- **Phase C — HITL 정식화**  
  1) verifier interrupt  
  2) human response resume  
  3) review audit trail 정비  

- **Phase D — retrieval 정식화**  
  1) query rewrite  
  2) hybrid retrieval  
  3) rerank  
  4) evidence verification  
  5) sentence citation binding  

- **Phase E — observability / eval**  
  1) LangSmith tracing  
  2) regression evaluation  
  3) groundedness / citation coverage metric  
  4) demo/production observability 분리  

### 8.11 구현 기준 선언

- LangGraph는 orchestration runtime이어야 한다.  
- LangChain은 tool/model/structured-output layer여야 한다. LangChain 모델 객체가 레포 안에 직접 선언되는 것이 이상적이지만, 최소 기준은 **Runnable / structured output / tool-calling contract를 프로젝트 내부에서 명시적으로 보장**하는 것이다.  
- tool은 schema-driven callable이어야 한다.  
- HITL은 interrupt/resume 패턴으로 구현해야 한다.  
- 스트림은 orchestration event와 reasoning note를 분리해야 한다.  
- 최종 판단은 citation-bound, evidence-verified 구조여야 한다.  
- MCP는 필요 시 external tool adapter로만 사용한다.  

이 문서 기준으로 구현하지 않은 기능은, 설령 동작하더라도 best practice 충족으로 보지 않는다.

### 8.12 Non-goals (지금 당장 하지 않을 것)

범위를 안정시키기 위해 당장 하지 않을 것을 명시한다.

- 외부 MCP 서버에 의존하는 구현  
- vector DB 교체 (pgvector 유지, 추상화만 유지)  
- multi-agent full federation  

### 8.13 테스트 전략

구현 기준서이므로 테스트 기준을 둔다. 최소한 아래를 만족한다.

- **graph unit test** — 노드 전이·state 갱신 시나리오  
- **tool schema contract test** — 각 tool의 입력/출력 schema 검증  
- **interrupt/resume replay test** — HITL interrupt 후 resume 시나리오  
- **citation binding regression test** — sentence-level citation 포함 여부·품질  

### 8.14 관찰 지표

구현이 정석적인지 운영 중 어떻게 볼지 정의한다.

| 지표 | 설명 |
|------|------|
| tool call success rate | 도구 호출 성공 비율 |
| interrupt rate | HITL interrupt 발생 비율 |
| grounded citation coverage | 근거가 있는 citation 비율 |
| overclaim rejection rate | critic에서 과잉 주장 거절 비율 |

---

## 9. Streamlit UI 로직 변경 대응

> **에이전트·백엔드 로직을 변경할 때마다 Streamlit UI가 새 구조에 맞게 동작하도록 반드시 대응해야 한다.** 구현 항목 하나라도 빠지면 안 되며, UI 대응도 그 일부다.  
> **UI는 에이전트 구조를 표현하는 계층이며, 오케스트레이션/도구/검증 구조를 왜곡해서는 안 된다.** UI가 구현 기준을 지배하지 않는다.

### 9.1 원칙

- **로직 변경 = UI 변경**  
  agent, skills, langgraph_agent, reasoning_notes, persistence, API 응답 형식 등이 바뀌면, 이를 사용하는 Streamlit 화면·이벤트·표시도 함께 수정한다.
- **데이터 소스 분리**  
  - orchestration stream (node start/end, tool call/result, gate, interrupt/resume)  
  - reasoning note stream (공개용 작업 메모)  
  UI에서는 두 스트림을 혼동하지 않고, 라이브 패널 vs 리뷰 패널 등으로 구분해 표시한다.

### 9.2 대응 대상 (파일·기능)

| 대상 | 대응 내용 |
|------|------------|
| **ui/workspace.py** | orchestration stream과 reasoning note stream 분리 표시, 사고 과정은 structured review만, tool trace는 execution log로 유지. 이벤트 타입·페이로드 변경 시 카드/타임라인 반영. |
| **ui/studio.py** | 그래프(메인 오케스트레이션/스킬 흐름), 모델·프롬프트·도구·지식 탭이 새 노드/툴/state 구조를 반영하도록 업데이트. |
| **기타 Streamlit 페이지** | demo, RAG 라이브러리, 시연 데이터 제어 등에서 에이전트 실행 결과·이벤트·타임라인을 참조하는 부분이 새 스키마·이벤트 형식에 맞게 동작하도록 수정. |

### 9.3 체크

- Phase A～E 또는 파일별 변경(8.8)을 적용할 때마다, **해당 변경이 노출되는 모든 UI 경로**를 점검하고 수정한다.  
- 이 문서의 "구현 체크리스트"에 UI 대응이 포함되어 있으므로, **구현이 완료되었다고 보려면 Streamlit UI 로직 변경 대응까지 포함**해야 한다.

---

## 10. 다음 단계 (선택)

원할 경우, 이 문서를 기준으로 Phase 1 / Phase 2 / Phase 3 및 **파일별 실제 변경 목록(코드 레벨)**까지 정리할 수 있다.

### 10.1 핵심 개발 완료 후 선택 보완

Phase A～E 및 본 문서 Must/Should가 모두 완료된 **이후**에, 모범 수준을 더 끌어올리고 싶을 때 검토할 항목이다. 놓치지 않도록 여기 기재한다.

| 항목 | 내용 |
|------|------|
| **실패·복구·재시도** | 도구 실패·LLM 타임아웃 시 재시도/폴백/에러 분류 정책을 한 줄이라도 명시. 운영 관점에서 정석에 가깝게 만드는 보완. |
| **보안·거버넌스** | 감사/금융 맥락을 고려한 입력 검증·출력 샌드박싱·감사 로그 보존 기간 등을 "운영 시 고려할 것"으로 정리. |

— 위 두 가지는 **선택**이며, 핵심 개발 완료 후 필요 시 적용한다.

---

## 11. 참고 자료 및 원본 소스

구현 시 모듈별로 참고할 수 있는 자료와 원본 소스 위치를 정리한다.

| 구분 | 경로·문서 | 용도 |
|------|-----------|------|
| **Aura DB 스키마** | [docs/db_info/aura_db.md](../db_info/aura_db.md) | Aura 관련 테이블 스키마 정보. persistence·연동 시 참고. |
| **원본 소스 (PoC 출처)** | `/Users/joonbinchoi/Work/dwp/dwp-front` | 프론트엔드 원본. UI·플로우 참고 시 참고. |
| | `/Users/joonbinchoi/Work/dwp/dwp-backend` | 백엔드 원본. API·서비스 로직 참고 시 참고. |
| | `/Users/joonbinchoi/Work/dwp/aura-platform` | Aura 플랫폼 원본. LLM·감사 파이프라인 등 참고 시 참고. |

— 위 경로는 이 프로젝트를 PoC로 만들기 위해 가져온 원본 폴더이다. 필요 시 모듈별로 참고하면 된다.
