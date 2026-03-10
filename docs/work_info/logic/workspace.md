# Aura Agent 고도화 — Cursor 작업 프롬프트

> **목적**: 임원·사용자 시연용 엔터프라이즈급 자율형 에이전트 UI 완성  
> **대상 파일**: `reasoning_notes.py`, `ui/workspace.py`  
> **핵심 목표**: ① LLM 스트림이 실제 사고처럼 보이게 만들기 ② UI가 살아 숨쉬는 에이전트처럼 느껴지게 만들기

---

## PART A — `reasoning_notes.py` 스트림 사고 고도화

### A-1. 프롬프트 재설계: 공개용 요약 → 실제 판단 과정 노출

**문제 진단**

현재 `generate_working_note()` 함수의 system_prompt에 다음 지시가 포함되어 있음:
```
"내부 비공개 chain-of-thought를 노출하지 말고, 현재 단계에서 외부에 공개 가능한 작업 메모만 JSON으로 작성하라."
```
이로 인해 스트림에 출력되는 생각/행동/관찰이 아래처럼 단순 상황 요약에 그침:
- thought: "분석 대상 전표의 주요 정보를 요약하여 사용자에게 제공했습니다."
- action: "분석 대상 전표에 대한 정보를 기록하였습니다."
- observation: "경비 발생에 대한 검토가 필요합니다."

에이전트가 스스로 판단하고 추론하는 모습이 전혀 보이지 않아 시연 시 설득력 없음.

**변경 방향**

`generate_working_note()` 함수의 system_prompt를 다음 방향으로 교체:

```python
system_prompt = """당신은 엔터프라이즈 전표 감사 AI 에이전트다.
현재 단계에서 당신이 실제로 수행하는 판단과 추론 과정을 JSON으로 출력하라.

[출력 원칙]
- thought: 이 단계에서 어떤 판단 분기가 발생했는지, 왜 이 경로를 선택했는지 1~2문장으로 명시.
  예: "isHoliday=True이고 hrStatus=LEAVE가 동시 충족되어 휴일 사용 여부를 최우선 검증 경로로 설정했다."
- action: 어떤 도구를 왜 선택했는지, 또는 어떤 정책 조항을 적용했는지 명시.
  예: "MCC 5813(주류업종)이므로 merchant_risk_probe를 holiday_probe보다 먼저 실행해 위험도가 휴일 판정에 영향을 주는지 확인한다."
- observation: 실행 결과에서 발견한 구체적 신호와 다음 단계에 미치는 영향을 명시.
  예: "budget_remaining=245,000원으로 초과 없음 확인. 따라서 budget_probe는 생략하고 policy_ref 검증으로 이동한다."

[금지 사항]
- 전표 기본 정보(거래처명, 금액, 날짜)를 단순 재서술하는 문장 금지
- "~했습니다", "~필요합니다" 같은 막연한 결론 금지
- voucher_summary에 있는 내용을 그대로 반복하는 문장 금지

[배경 정보] — 출력 문장에 포함하지 말 것
{voucher_summary}

[현재 단계]: {node_name}
[이전 단계 결과]: {prev_result_summary}
[현재 실행 컨텍스트]: {context}
"""
```

**핵심 변경 포인트**
1. `"chain-of-thought를 노출하지 말라"` 지시 삭제
2. `voucher_summary`를 [배경 정보] 섹션으로 분리 — LLM이 참고만 하고 출력엔 포함 금지
3. `[이전 단계 결과]` 필드 추가 — 각 노드 호출 시 이전 노드의 핵심 결과를 1줄 요약으로 전달
4. 각 필드에 구체적 출력 예시 포함 (few-shot 스타일)

**구현 작업**

`generate_working_note()` 호출부 수정 (langgraph_agent.py의 각 노드):
```python
# 기존
note = generate_working_note(node_name=..., context=voucher_summary)

# 변경
prev_summary = state.get("last_node_summary", "없음")
note = generate_working_note(
    node_name=node_name,
    voucher_summary=voucher_summary,   # 배경정보로 분리
    prev_result_summary=prev_summary,  # 이전 단계 결과
    context=current_context            # 현재 단계 고유 정보만
)
# 완료 후 state에 현재 노드 요약 저장
state["last_node_summary"] = f"{node_name} 완료: {note.get('observation','')}"
```

---

### A-2. 노드별 차별화된 컨텍스트 전달

**문제**: 모든 노드(intake, planner, execute, critic, verify, reporter)가 동일한 `voucher_summary`만 컨텍스트로 받아 출력이 단조로움.

**변경**: 각 노드가 자신의 고유 상태를 `context`로 전달하도록 수정.

```python
# planner_node에서
context = {
    "selected_tools": plan.tool_sequence,        # 선택된 도구 목록
    "skipped_tools": plan.skipped_tools,          # 생략된 도구와 이유
    "flags": {
        "isHoliday": state.isHoliday,
        "budgetExceeded": state.budgetExceeded,
        "mccCode": state.mccCode
    }
}

# execute_node에서
context = {
    "tool_name": current_tool,
    "tool_input": tool_input_summary,
    "tool_result_preview": str(result)[:200]     # 결과 앞부분
}

# critic_node에서
context = {
    "score_before_critic": state.score,
    "policy_violations_found": state.policy_violations,
    "recommend_hold": critic_output.recommend_hold
}

# verify_node에서
context = {
    "verification_targets": state.verification_targets,
    "gate_result": gate_result,                  # PASS / FAIL
    "failed_checks": state.failed_verifications
}
```

---

### A-3. 반복 전표 정보 출력 차단 (출력 후처리 필터)

`generate_working_note()` 반환값에서 voucher 기본 정보 반복 구문을 필터링하는 후처리 함수 추가:

```python
def _clean_working_note(note: dict, voucher_summary: str) -> dict:
    """voucher_summary의 핵심 토큰이 그대로 반복되면 해당 문장 제거"""
    import re
    # voucher_summary에서 핵심 식별자 추출 (거래처명, 금액, 날짜)
    blacklist_patterns = _extract_voucher_tokens(voucher_summary)
    
    for field in ["thought", "action", "observation"]:
        if field in note:
            sentence = note[field]
            # 핵심 토큰 2개 이상이 그대로 등장하면 경고 후 교체 요청
            matches = sum(1 for p in blacklist_patterns if p in sentence)
            if matches >= 2:
                note[field] = f"[재생성 필요: 전표 기본 정보 반복 감지] {sentence}"
    return note
```

---

## PART B — `ui/workspace.py` UI/UX 전면 개선

### B-1. ⏳ 이벤트 대기 텍스트 제거

**문제**: `sse_text_stream()` 함수에서 `first_event=False` 이후 매번 `"⏳ 다음 이벤트 수신 중..."` 텍스트를 yield함. 20개 이벤트 기준 19회 반복 출력.

**변경**:
```python
# 기존 — 삭제할 코드
if not first_event:
    yield "\n\n⏳ 다음 이벤트 수신 중...\n\n"

# 변경 — 텍스트 대신 구분선만 (또는 완전 제거)
if not first_event:
    yield "\n\n---\n\n"  # 또는 이것도 제거하고 카드 간격으로만 구분
```

---

### B-2. 생각/행동/관찰 레이아웃: 3열 → 세로 아코디언

**문제**: `st.columns(3)` 으로 thought / action / observation을 3열 분리. 좁은 화면에서 텍스트 잘림, 전체 가독성 저하, 정보가 한눈에 들어오지 않음.

**변경**:
```python
# 기존 — 삭제
cols = st.columns(3)
cols[0].write(thought)
cols[1].write(action)
cols[2].write(observation)

# 변경 — 세로 구조 with 아이콘
def render_thinking_card(thought: str, action: str, observation: str, node_name: str):
    with st.container():
        # 생각
        if thought:
            st.markdown(f"""
            <div class="thinking-row thought">
              <span class="thinking-icon">🧠</span>
              <div class="thinking-content">
                <span class="thinking-label">판단</span>
                <p>{thought}</p>
              </div>
            </div>
            """, unsafe_allow_html=True)
        
        # 행동
        if action:
            st.markdown(f"""
            <div class="thinking-row action">
              <span class="thinking-icon">⚡</span>
              <div class="thinking-content">
                <span class="thinking-label">실행</span>
                <p>{action}</p>
              </div>
            </div>
            """, unsafe_allow_html=True)
        
        # 관찰
        if observation:
            st.markdown(f"""
            <div class="thinking-row observation">
              <span class="thinking-icon">🔍</span>
              <div class="thinking-content">
                <span class="thinking-label">발견</span>
                <p>{observation}</p>
              </div>
            </div>
            """, unsafe_allow_html=True)
```

CSS (`st.markdown` 상단에 한 번 inject):
```css
<style>
.thinking-row {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 10px 14px;
    margin: 6px 0;
    border-radius: 8px;
    border-left: 3px solid;
}
.thinking-row.thought  { background: #0f1a2e; border-color: #3b82f6; }
.thinking-row.action   { background: #0f2a1a; border-color: #22c55e; }
.thinking-row.observation { background: #1a1500; border-color: #f59e0b; }
.thinking-icon { font-size: 18px; margin-top: 2px; flex-shrink: 0; }
.thinking-label {
    font-size: 10px; font-weight: 700; letter-spacing: 1.2px;
    text-transform: uppercase; opacity: 0.6; display: block; margin-bottom: 4px;
}
.thinking-content p { margin: 0; font-size: 14px; line-height: 1.6; color: #e2e8f0; }
</style>
```

---

### B-3. 파이프라인 진행 바 추가 (핵심 시각 요소)

**문제**: 현재 stage-pill이 node 이름을 텍스트로만 나열. 어느 단계까지 완료됐는지, 전체 흐름에서 지금 어디인지 전혀 파악 불가.

**추가**: `render_pipeline_progress()` 함수 신규 작성.

```python
PIPELINE_NODES = [
    ("screener",  "스크리닝"),
    ("intake",    "정보 수집"),
    ("planner",   "계획 수립"),
    ("execute",   "도구 실행"),
    ("critic",    "비판 검토"),
    ("verify",    "정책 검증"),
    ("reporter",  "보고서 생성"),
    ("finalizer", "최종 판정"),
]

def render_pipeline_progress(completed_nodes: list[str], current_node: str):
    """파이프라인 진행 상태 바 렌더링"""
    total = len(PIPELINE_NODES)
    completed_count = len(completed_nodes)
    progress_pct = completed_count / total
    
    node_html_parts = []
    for node_id, node_label in PIPELINE_NODES:
        if node_id in completed_nodes:
            status_class = "done"
            icon = "✓"
        elif node_id == current_node:
            status_class = "active"
            icon = "◉"
        else:
            status_class = "pending"
            icon = "○"
        
        node_html_parts.append(f"""
        <div class="pipeline-node {status_class}">
          <div class="pipeline-dot">{icon}</div>
          <div class="pipeline-label">{node_label}</div>
        </div>
        """)
    
    connector_html = '<div class="pipeline-connector"></div>'.join(node_html_parts)
    
    st.markdown(f"""
    <style>
    .pipeline-wrapper {{
        background: #0d1117;
        border: 1px solid #1e2d3d;
        border-radius: 12px;
        padding: 16px 20px;
        margin-bottom: 16px;
    }}
    .pipeline-track {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        position: relative;
    }}
    .pipeline-track::before {{
        content: '';
        position: absolute;
        top: 14px;
        left: 0; right: 0;
        height: 2px;
        background: #1e2d3d;
        z-index: 0;
    }}
    .pipeline-node {{
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 6px;
        position: relative;
        z-index: 1;
        min-width: 64px;
    }}
    .pipeline-dot {{
        width: 28px; height: 28px;
        border-radius: 50%;
        display: flex; align-items: center; justify-content: center;
        font-size: 13px; font-weight: 700;
    }}
    .pipeline-node.done .pipeline-dot {{
        background: #22c55e; color: #000;
    }}
    .pipeline-node.active .pipeline-dot {{
        background: #3b82f6; color: #fff;
        box-shadow: 0 0 12px #3b82f6aa;
        animation: pulse-dot 1.2s ease-in-out infinite;
    }}
    .pipeline-node.pending .pipeline-dot {{
        background: #1e2d3d; color: #4b5563; border: 2px solid #1e2d3d;
    }}
    .pipeline-label {{
        font-size: 10px; letter-spacing: 0.5px;
        color: #6b7280; text-align: center; white-space: nowrap;
    }}
    .pipeline-node.done .pipeline-label  {{ color: #22c55e; }}
    .pipeline-node.active .pipeline-label {{ color: #3b82f6; font-weight: 700; }}
    .pipeline-connector {{
        flex: 1; height: 2px;
        background: linear-gradient(to right, #22c55e, #1e2d3d);
        position: relative; z-index: 1; margin-top: -6px;
    }}
    @keyframes pulse-dot {{
        0%, 100% {{ box-shadow: 0 0 8px #3b82f6aa; }}
        50%       {{ box-shadow: 0 0 20px #3b82f6; }}
    }}
    .pipeline-progress-bar {{
        height: 3px;
        background: linear-gradient(to right, #22c55e {progress_pct*100:.0f}%, #1e2d3d {progress_pct*100:.0f}%);
        border-radius: 2px;
        margin-top: 12px;
        transition: width 0.5s ease;
    }}
    .pipeline-meta {{
        display: flex; justify-content: space-between; margin-top: 8px;
        font-size: 11px; color: #4b5563;
    }}
    </style>
    <div class="pipeline-wrapper">
      <div class="pipeline-track">
        {''.join(node_html_parts)}
      </div>
      <div class="pipeline-progress-bar"></div>
      <div class="pipeline-meta">
        <span>{completed_count}/{total} 단계 완료</span>
        <span>{'분석 완료' if completed_count == total else f'진행 중: {current_node}'}</span>
      </div>
    </div>
    """, unsafe_allow_html=True)
```

**호출 위치**: `_stream_card_chunks()` 또는 `sse_text_stream()` 내부에서 각 NODE_START 이벤트 수신 시 `st.session_state.completed_nodes`와 `st.session_state.current_node`를 업데이트하고, 분석 시작 시 파이프라인 바를 `st.empty()`로 place-hold하여 in-place 갱신.

```python
# workspace.py 분석 실행 섹션에서
pipeline_placeholder = st.empty()
completed_nodes = []
current_node = "screener"

# 스트리밍 루프 내
for event in sse_events:
    if event.type == "NODE_START":
        current_node = event.node_name
        with pipeline_placeholder:
            render_pipeline_progress(completed_nodes, current_node)
    elif event.type == "NODE_END":
        completed_nodes.append(event.node_name)
        with pipeline_placeholder:
            render_pipeline_progress(completed_nodes, current_node)
```

---

### B-4. 이벤트 전체 보기: 8개 제한 해제 + 접기/펼치기

**문제**: `latest_events = [...timeline][-8:]` 로 최신 8개만 표시. 15개 이상 이벤트 시 초기 screener/intake 결과 소실.

**변경**:
```python
# 기존
latest_events = st.session_state.timeline[-8:]

# 변경 — 완료 노드는 접기, 현재/최신 노드는 펼침
def render_timeline_with_collapse(timeline: list):
    if not timeline:
        return
    
    # 노드별로 이벤트 그룹화
    from collections import defaultdict
    node_groups = defaultdict(list)
    node_order = []
    for event in timeline:
        node = event.get("node_name", "unknown")
        if node not in node_groups:
            node_order.append(node)
        node_groups[node].append(event)
    
    latest_node = node_order[-1] if node_order else None
    
    for node in node_order:
        events = node_groups[node]
        is_latest = (node == latest_node)
        node_label = _get_node_label(node)   # PIPELINE_NODES 딕셔너리에서 한글명 조회
        event_count = len(events)
        
        # 최신 노드는 기본 펼침, 이전 노드는 기본 접힘
        with st.expander(
            label=f"{'▶ ' if is_latest else '✓ '}{node_label}  ({event_count}개 이벤트)",
            expanded=is_latest
        ):
            for event in events:
                _render_single_event_card(event)
```

---

### B-5. 스트림 완료 후 화면 단절 해소

**문제**: `st.rerun()` 호출 시 `write_stream` 컨텐츠가 사라지고 timeline으로 대체되면서 화면 깜빡임 발생.

**변경**:
```python
# 스트리밍 완료 후 st.rerun() 대신 session_state 플래그 사용
if st.session_state.get("stream_complete") and not st.session_state.get("rerendered"):
    st.session_state["rerendered"] = True
    # rerun 대신 아래 섹션에서 직접 timeline 렌더링 (rerun 없이)
    render_timeline_with_collapse(st.session_state.timeline)
```

또는 스트리밍 컨텐츠를 `session_state`에 캐싱:
```python
# 스트리밍 루프에서
stream_buffer = []
for chunk in stream_generator:
    stream_buffer.append(chunk)
    yield chunk
st.session_state["last_stream_content"] = "".join(stream_buffer)

# 완료 후 expander로 보존
if st.session_state.get("last_stream_content"):
    with st.expander("📋 분석 스트림 로그 보기", expanded=False):
        st.markdown(st.session_state["last_stream_content"])
```

---

### B-6. 모델명 노이즈 제거 + 아이콘 통일

**문제 1**: `source_label`에 모델명(`gpt-4o-mini` 등)이 매 이벤트마다 반복 표시.

```python
# 기존 — 매 카드에 모델명 표시
st.caption(f"{event.timestamp} · {event.source_label}")

# 변경 — 모델명은 헤더/사이드바에 한 번만, 이벤트 카드에서는 제거
# 사이드바 또는 분석 시작 헤더에:
st.caption(f"분석 모델: {model_name}")
```

**문제 2**: `TOOL_CALL` 이벤트가 `role=user` 아이콘으로 표시되어 혼란.

```python
# 이벤트 타입별 아이콘 매핑 수정
EVENT_ICON_MAP = {
    "NODE_START":       "🤖",   # AI 에이전트
    "NODE_END":         "✅",
    "TOOL_CALL":        "⚡",   # 도구 실행 (사람 아이콘 X)
    "TOOL_RESULT":      "📊",
    "HITL_PAUSE":       "⏸️",
    "GATE_APPLIED":     "🔒",
    "SCORE_BREAKDOWN":  "📈",
    "FINAL_VERDICT":    "⚖️",
}

# 기존 role 기반 분기 대신 이벤트 타입 기반으로 통일
icon = EVENT_ICON_MAP.get(event.type, "🤖")
```

---

### B-7. Confidence Score 인라인 시각화

**문제**: `SCORE_BREAKDOWN` 이벤트가 스트림 외부로 분리되어 텍스트로만 출력됨.

**변경**: `_render_single_event_card()` 내에서 `SCORE_BREAKDOWN` 타입 감지 시 게이지 바 렌더링:

```python
def render_score_breakdown_card(policy_score: int, evidence_score: int, final_score: int):
    def score_bar(label, value, color):
        return f"""
        <div style="margin: 8px 0;">
          <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:4px;">
            <span style="color:#9ca3af;">{label}</span>
            <span style="color:{color}; font-weight:700;">{value}</span>
          </div>
          <div style="background:#1e2d3d; border-radius:4px; height:8px;">
            <div style="width:{value}%; background:{color}; height:8px; border-radius:4px; transition:width 0.8s ease;"></div>
          </div>
        </div>
        """
    
    st.markdown(f"""
    <div style="background:#0d1117; border:1px solid #1e2d3d; border-radius:10px; padding:16px; margin:10px 0;">
      <div style="font-size:12px; font-weight:700; letter-spacing:1px; color:#6b7280; margin-bottom:12px;">CONFIDENCE SCORE</div>
      {score_bar('정책 점수', policy_score, '#3b82f6')}
      {score_bar('근거 점수', evidence_score, '#8b5cf6')}
      {score_bar('최종 점수', final_score, '#22c55e' if final_score >= 70 else '#f59e0b' if final_score >= 50 else '#ef4444')}
    </div>
    """, unsafe_allow_html=True)
```

---

## PART C — 전체 작업 우선순위 요약

| 우선순위 | 파일 | 작업 | 예상 임팩트 |
|----------|------|------|------------|
| 🔴 P0-1 | `reasoning_notes.py` | system_prompt 교체 (A-1) | 스트림 내용이 실제 판단처럼 변화 |
| 🔴 P0-2 | `ui/workspace.py` | ⏳ 제거 + 레이아웃 세로 변경 (B-1, B-2) | 즉각적 UX 개선 |
| 🔴 P0-3 | `ui/workspace.py` | 파이프라인 진행 바 추가 (B-3) | 시연 시 가장 인상적인 시각 요소 |
| 🟠 P1-1 | `reasoning_notes.py` | 노드별 컨텍스트 차별화 (A-2) | 각 단계가 다른 내용을 말하게 됨 |
| 🟠 P1-2 | `ui/workspace.py` | 이벤트 접기/펼치기 (B-4) | 전체 맥락 파악 가능 |
| 🟠 P1-3 | `ui/workspace.py` | 스트림 완료 화면 단절 해소 (B-5) | 시연 중 깜빡임 제거 |
| 🟡 P2-1 | `ui/workspace.py` | 모델명 노이즈 제거 + 아이콘 통일 (B-6) | 정보 밀도 정리 |
| 🟡 P2-2 | `ui/workspace.py` | Score 게이지 바 (B-7) | 시각적 완성도 향상 |
| 🟡 P2-3 | `reasoning_notes.py` | 반복 출력 후처리 필터 (A-3) | 품질 안전망 |

---

## 시연 체크리스트 (작업 완료 후 확인 항목)

- [ ] 스트림 실행 시 같은 전표 정보가 3회 이상 반복 출력되지 않는가
- [ ] thought/action/observation이 단순 재서술이 아닌 구체적 판단 분기를 설명하는가
- [ ] 파이프라인 진행 바가 NODE_START/NODE_END 이벤트에 맞춰 실시간으로 갱신되는가
- [ ] ⏳ 텍스트가 화면에 출력되지 않는가
- [ ] 분석 완료 후 화면 깜빡임 없이 결과가 유지되는가
- [ ] TOOL_CALL 이벤트에 사람(user) 아이콘이 사용되지 않는가
- [ ] 15개 이상 이벤트에서 초기 screener 결과가 보이는가 (더 보기 / 접기 동작)
- [ ] Score 게이지 바가 최종 판정 전 스트림 흐름 안에서 나타나는가



#추가보완작업
# Aura Agent 고도화 v2 — 진짜 LLM 스트리밍 + 타이핑 효과

> **전제**: v1 프롬프트 작업(⏳ 제거, 레이아웃 개선, 파이프라인 바)이 완료된 상태에서 추가 적용
> **목표**: `generate_working_note()` 사후 해설 구조를 완전 폐기하고,
> 각 노드 LLM이 실제로 추론하는 토큰을 실시간으로 UI에 타이핑 효과로 출력

---

## 왜 구조를 바꿔야 하는가

현재 스트림에 보이는 내용은 이런 흐름으로 만들어집니다:

```
① planner LLM 실행 (진짜 판단) → 결과만 state에 저장
② generate_working_note() 별도 LLM 호출 → "방금 한 일을 설명해줘"
③ 설명 텍스트를 스트림에 표시
```

즉 스트림에 보이는 것은 **진짜 사고가 아닌 사후 요약**입니다.
바꿔야 할 구조:

```
① planner LLM 실행 — stream=True로 토큰 단위로 직접 UI에 흘림
② 토큰이 흐르는 동안 타이핑 효과로 화면에 표시
③ 완료 후 state 저장
```

Claude나 Cursor가 답변을 타이핑하듯, 에이전트의 실제 추론이 실시간으로 보이는 구조입니다.

---

## STEP 1 — 각 노드 LLM 응답 스키마에 `reasoning` 필드 추가

### 대상 파일: `langgraph_agent.py` (또는 각 노드 파일)

각 노드가 LLM을 호출할 때 사용하는 Pydantic 응답 모델에 `reasoning` 필드를 추가합니다.
이 필드가 스트림에 타이핑될 **진짜 LLM 사고 내용**입니다.

```python
# --- Planner 노드 응답 모델 ---
class PlannerOutput(BaseModel):
    tool_sequence: list[str]
    skipped_tools: list[str]
    skip_reasons: dict[str, str]
    reasoning: str  # ← 추가: "왜 이 도구들을 이 순서로 선택했는가"

# --- Critic 노드 응답 모델 ---
class CriticOutput(BaseModel):
    recommend_hold: bool
    policy_violations: list[str]
    severity: str
    reasoning: str  # ← 추가: "어떤 근거로 이 판정을 내렸는가"

# --- Verifier 노드 응답 모델 ---
class VerifierOutput(BaseModel):
    gate_result: str  # PASS / FAIL
    failed_checks: list[str]
    reasoning: str  # ← 추가: "어떤 정책 조항을 어떻게 검증했는가"

# --- Reporter 노드 응답 모델 ---
class ReporterOutput(BaseModel):
    summary: str
    score: int
    final_status: str
    reasoning: str  # ← 추가: "최종 판단에 이른 추론 과정"
```

### 각 노드 LLM 프롬프트에 reasoning 생성 지시 추가

```python
# planner_node의 LLM system prompt 끝에 추가
"""
[응답 형식]
반드시 아래 JSON 형식으로 응답하라. reasoning 필드는 필수이며,
단순 나열이 아닌 실제 판단 과정을 서술해야 한다.

reasoning 작성 기준:
- 어떤 신호(flag)가 이 결정을 유발했는가
- 여러 선택지 중 왜 이 경로를 선택했는가
- 생략한 도구가 있다면 왜 생략했는가
- 다음 노드에서 주의해야 할 사항이 있는가

예시:
"isHoliday=True이고 hrStatus=LEAVE가 동시에 감지되어
휴일 경비 사용 여부를 최우선 검증 경로로 설정했다.
MCC 5813(주류업종)이므로 merchant_risk_probe를 먼저 실행해
위험도가 휴일 판정에 영향을 주는지 확인할 필요가 있다.
budgetExceeded=False이므로 budget_probe는 생략한다."
"""
```

---

## STEP 2 — `generate_working_note()` 완전 폐기

### 대상 파일: `reasoning_notes.py`, `langgraph_agent.py`

**`reasoning_notes.py`**: 파일 전체를 삭제하거나,
하위 호환을 위해 아래처럼 pass-through 함수만 남깁니다.

```python
# reasoning_notes.py — 기존 generate_working_note() 전체 삭제 후
# 각 노드에서 직접 reasoning 필드를 사용하므로 이 파일은 더 이상 필요 없음
# langgraph_agent.py에서 import 중이라면 아래로 교체

def extract_reasoning(node_output) -> str:
    """노드 출력에서 reasoning 필드를 추출. 없으면 빈 문자열 반환."""
    if hasattr(node_output, "reasoning"):
        return node_output.reasoning
    if isinstance(node_output, dict):
        return node_output.get("reasoning", "")
    return ""
```

**`langgraph_agent.py`**: 각 노드에서 `generate_working_note()` 호출 부분을 모두 제거하고,
LLM 응답의 `.reasoning` 필드를 SSE 이벤트로 직접 emit합니다.

```python
# 기존 패턴 — 삭제
note = generate_working_note(node_name="planner", context=voucher_summary)
await emit_stream_event(type="NODE_THINKING", content=note)

# 변경 패턴 — LLM 응답에서 직접 추출
planner_output = await llm_call(prompt)          # 실제 플래너 LLM 호출
reasoning_text = planner_output.reasoning        # 진짜 추론 텍스트
await emit_stream_event(
    type="NODE_THINKING",
    node="planner",
    content=reasoning_text                        # 실제 LLM이 쓴 내용
)
```

---

## STEP 3 — 핵심: LLM 스트리밍 직접 연결 (가장 중요)

### 대상 파일: `langgraph_agent.py`

LLM 호출을 `stream=True`로 전환하여 토큰이 생성되는 즉시 UI로 흘립니다.
이것이 Claude/Cursor가 타이핑하듯 보이는 구조의 핵심입니다.

```python
async def stream_node_reasoning(
    node_name: str,
    prompt: str,
    response_model: type,
    event_queue: asyncio.Queue
):
    """
    LLM을 stream=True로 호출하여 reasoning 토큰을 실시간으로 큐에 push.
    UI는 이 큐를 SSE로 소비하여 타이핑 효과로 표시.
    """
    
    # --- OpenAI/Azure 사용 시 ---
    full_response = ""
    reasoning_started = False
    
    async for chunk in await openai_client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        stream=True,
        response_format={"type": "json_object"}
    ):
        delta = chunk.choices[0].delta.content or ""
        full_response += delta
        
        # reasoning 필드 값이 시작되면 토큰을 실시간으로 큐에 push
        # JSON 스트리밍 중 "reasoning": " 이후부터 닫는 " 전까지 추출
        reasoning_token = _extract_reasoning_token(delta, full_response)
        if reasoning_token:
            await event_queue.put({
                "type": "THINKING_TOKEN",
                "node": node_name,
                "token": reasoning_token   # 토큰 단위 (1~5글자)
            })
    
    # 스트리밍 완료 후 전체 응답 파싱
    parsed = response_model.model_validate_json(full_response)
    return parsed


def _extract_reasoning_token(delta: str, full_so_far: str) -> str:
    """
    JSON 스트리밍 중 reasoning 필드의 값 부분만 추출.
    "reasoning": "← 여기서부터 토큰 추출 → " 닫히기 전까지
    """
    # reasoning 필드 시작 감지
    if '"reasoning"' in full_so_far and '"reasoning": "' in full_so_far:
        # reasoning 값 시작 이후의 delta만 반환
        reasoning_start = full_so_far.index('"reasoning": "') + len('"reasoning": "')
        current_reasoning = full_so_far[reasoning_start:]
        
        # 아직 닫히지 않았으면 (진행 중)
        if not current_reasoning.endswith('"}') and not current_reasoning.endswith('",'):
            return delta  # 현재 토큰 그대로 반환
    return ""
```

---

## STEP 4 — UI 타이핑 효과 구현

### 대상 파일: `ui/workspace.py`

SSE로 수신한 `THINKING_TOKEN` 이벤트를 타이핑 효과로 렌더링합니다.

```python
def render_live_thinking_stream(node_name: str, event_source):
    """
    THINKING_TOKEN 이벤트를 실시간으로 받아 타이핑 효과로 표시.
    Claude/Cursor처럼 한 글자씩 나타나는 효과.
    """
    
    node_label = NODE_LABEL_MAP.get(node_name, node_name)
    
    # 노드별 컬러 설정
    NODE_COLORS = {
        "planner":   {"bg": "#0f1a2e", "border": "#3b82f6", "icon": "🧠"},
        "critic":    {"bg": "#1a0f0f", "border": "#ef4444", "icon": "🔍"},
        "verify":    {"bg": "#0f1a14", "border": "#22c55e", "icon": "✅"},
        "reporter":  {"bg": "#1a1500", "border": "#f59e0b", "icon": "📊"},
        "execute":   {"bg": "#0f0f1a", "border": "#8b5cf6", "icon": "⚡"},
        "intake":    {"bg": "#0d1117", "border": "#6b7280", "icon": "📥"},
        "screener":  {"bg": "#0d1117", "border": "#6b7280", "icon": "🔎"},
        "finalizer": {"bg": "#0f1a0f", "border": "#22c55e", "icon": "⚖️"},
    }
    color = NODE_COLORS.get(node_name, {"bg": "#0d1117", "border": "#4b5563", "icon": "🤖"})
    
    # 타이핑 컨테이너 CSS (한 번만 inject)
    st.markdown(f"""
    <style>
    .thinking-stream-card {{
        background: {color['bg']};
        border: 1px solid {color['border']};
        border-left: 3px solid {color['border']};
        border-radius: 10px;
        padding: 16px 20px;
        margin: 10px 0;
        font-family: 'JetBrains Mono', 'Fira Code', monospace;
        font-size: 13px;
        line-height: 1.8;
        color: #e2e8f0;
        position: relative;
    }}
    .thinking-node-label {{
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 1.5px;
        text-transform: uppercase;
        color: {color['border']};
        margin-bottom: 10px;
        display: flex;
        align-items: center;
        gap: 6px;
    }}
    .thinking-cursor {{
        display: inline-block;
        width: 2px;
        height: 1em;
        background: {color['border']};
        margin-left: 2px;
        vertical-align: text-bottom;
        animation: blink 0.7s step-end infinite;
    }}
    @keyframes blink {{
        0%, 100% {{ opacity: 1; }}
        50%       {{ opacity: 0; }}
    }}
    .thinking-text-content {{
        min-height: 24px;
    }}
    </style>
    """, unsafe_allow_html=True)
    
    # st.empty()로 자리 잡기 — 토큰 수신마다 in-place 업데이트
    thinking_placeholder = st.empty()
    accumulated_text = ""
    
    # SSE 이벤트 루프
    for event in event_source:
        if event.get("type") == "THINKING_TOKEN" and event.get("node") == node_name:
            accumulated_text += event["token"]
            
            # 매 토큰마다 placeholder 업데이트 (타이핑 효과)
            thinking_placeholder.markdown(f"""
            <div class="thinking-stream-card">
              <div class="thinking-node-label">
                {color['icon']} {node_label} · 추론 중
              </div>
              <div class="thinking-text-content">
                {accumulated_text}<span class="thinking-cursor"></span>
              </div>
            </div>
            """, unsafe_allow_html=True)
        
        elif event.get("type") == "NODE_END" and event.get("node") == node_name:
            # 완료 — 커서 제거, 완료 상태로 전환
            thinking_placeholder.markdown(f"""
            <div class="thinking-stream-card" style="opacity:0.85;">
              <div class="thinking-node-label">
                {color['icon']} {node_label} · 완료
              </div>
              <div class="thinking-text-content">
                {accumulated_text}
              </div>
            </div>
            """, unsafe_allow_html=True)
            break
    
    return accumulated_text  # 완료된 텍스트 반환 (필요 시 저장)
```

---

## STEP 5 — SSE 이벤트 타입 추가

### 대상 파일: SSE 이벤트 정의 파일 (또는 `langgraph_agent.py`)

```python
# 기존 이벤트 타입에 추가
class StreamEventType(str, Enum):
    NODE_START      = "NODE_START"
    NODE_END        = "NODE_END"
    TOOL_CALL       = "TOOL_CALL"
    TOOL_RESULT     = "TOOL_RESULT"
    HITL_PAUSE      = "HITL_PAUSE"
    GATE_APPLIED    = "GATE_APPLIED"
    SCORE_BREAKDOWN = "SCORE_BREAKDOWN"
    FINAL_VERDICT   = "FINAL_VERDICT"
    THINKING_TOKEN  = "THINKING_TOKEN"   # ← 신규 추가: 토큰 단위 스트리밍
    THINKING_DONE   = "THINKING_DONE"    # ← 신규 추가: 해당 노드 추론 완료

# THINKING_TOKEN 이벤트 구조
@dataclass
class ThinkingTokenEvent:
    type: str = "THINKING_TOKEN"
    node: str = ""          # "planner", "critic", 등
    token: str = ""         # 실제 토큰 텍스트 (1~5글자)
    timestamp: str = ""
```

---

## STEP 6 — 전체 스트리밍 흐름 연결

`workspace.py`의 메인 스트리밍 루프를 이벤트 타입에 따라 분기:

```python
async def run_analysis_stream(case_id: str):
    """
    메인 스트리밍 루프.
    THINKING_TOKEN → 타이핑 효과로 실시간 출력
    NODE_START/END → 파이프라인 바 업데이트
    TOOL_CALL/RESULT → 도구 카드 표시
    SCORE_BREAKDOWN → 게이지 바 표시
    """
    pipeline_placeholder = st.empty()
    current_thinking_placeholder = st.empty()
    completed_nodes = []
    current_node = ""
    thinking_buffer = {}   # node_name → 누적 텍스트
    
    async for event in sse_stream(case_id):
        
        if event["type"] == "NODE_START":
            current_node = event["node"]
            thinking_buffer[current_node] = ""
            # 파이프라인 바 업데이트
            with pipeline_placeholder:
                render_pipeline_progress(completed_nodes, current_node)
        
        elif event["type"] == "THINKING_TOKEN":
            node = event["node"]
            thinking_buffer[node] = thinking_buffer.get(node, "") + event["token"]
            # 타이핑 효과 업데이트
            current_thinking_placeholder.markdown(
                _build_thinking_card_html(node, thinking_buffer[node], is_complete=False),
                unsafe_allow_html=True
            )
        
        elif event["type"] == "THINKING_DONE":
            node = event["node"]
            # 커서 제거, 완료 상태로 전환
            current_thinking_placeholder.markdown(
                _build_thinking_card_html(node, thinking_buffer[node], is_complete=True),
                unsafe_allow_html=True
            )
        
        elif event["type"] == "NODE_END":
            completed_nodes.append(event["node"])
            with pipeline_placeholder:
                render_pipeline_progress(completed_nodes, current_node)
        
        elif event["type"] == "TOOL_CALL":
            render_tool_call_card(event)
        
        elif event["type"] == "SCORE_BREAKDOWN":
            render_score_breakdown_card(
                event["policy_score"],
                event["evidence_score"],
                event["final_score"]
            )
        
        elif event["type"] == "FINAL_VERDICT":
            render_final_verdict(event)
```

---

## 최종 구현 결과 — 사용자가 보게 되는 화면

```
┌─ 파이프라인 진행 바 ──────────────────────────────────────┐
│ ✓스크리닝  ✓수집  ◉계획수립  ○실행  ○검토  ○검증  ○보고  ○판정 │
│ ████████████████████░░░░░░░░░░░░░░░  3/8 단계            │
└──────────────────────────────────────────────────────────┘

┌─ 🧠 PLANNER · 추론 중 ────────────────────────────────────┐
│                                                           │
│  isHoliday=True이고 hrStatus=LEAVE가 동시에 감지되어       │
│  휴일 경비 사용 여부를 최우선 검증 경로로 설정했다.          │
│  MCC 5813(주류업종)이므로 merchant_risk_probe를 먼저       │
│  실행해 위험도가 휴일 판정에 영향을 주는지 확인할│           │  ← 커서 깜빡임
│                                                           │
└──────────────────────────────────────────────────────────┘
```

Claude나 Cursor처럼 LLM이 실시간으로 타이핑하는 모습이 그대로 재현됩니다.

---

## 작업 체크리스트

- [ ] 각 노드 Pydantic 모델에 `reasoning: str` 필드 추가 (planner, critic, verify, reporter, execute)
- [ ] 각 노드 LLM 프롬프트에 reasoning 생성 지시 + 예시 추가
- [ ] LLM 호출을 `stream=True`로 전환 + `_extract_reasoning_token()` 구현
- [ ] `THINKING_TOKEN` / `THINKING_DONE` SSE 이벤트 타입 추가
- [ ] `generate_working_note()` 호출 전체 제거 (`reasoning_notes.py` 폐기)
- [ ] `workspace.py` 메인 루프에 `THINKING_TOKEN` 분기 추가
- [ ] `_build_thinking_card_html()` 함수 구현 (노드별 컬러 + 깜빡이는 커서)
- [ ] 완료 노드 thinking card → 커서 제거 + opacity 처리
- [ ] 시연 테스트: 실제 전표 실행 시 각 노드에서 타이핑 텍스트가 순차적으로 출력되는지 확인



# Aura Agent 고도화 v3 — Reasoning 정합성 검증

> **전제**: v1(UI 개선) + v2(실시간 스트리밍 구조 전환) 작업이 완료된 상태에서 추가 적용
> **목표**: reasoning 텍스트와 실제 판단 결과값 사이의 모순을 구조적으로 차단

---

## 왜 필요한가

v2 작업 후 각 노드 LLM은 `reasoning` 필드에 판단 과정을 서술하고,
동시에 `recommend_hold`, `gate_result`, `final_status` 같은 결과값을 반환합니다.

문제는 LLM이 이 두 가지를 한 번에 생성할 때 **모순이 발생할 수 있다**는 점입니다.

```
# 실제로 발생 가능한 모순 예시

reasoning:
"isHoliday=True이고 hrStatus=LEAVE 동시 충족.
 정책 위반 가능성이 낮아 정상 처리가 적합하다고 판단한다."

recommend_hold: True   # ← reasoning과 정반대
```

시연 중 화면에서 "통과"라고 타이핑된 직후 결과가 "보류"로 나오면
에이전트 신뢰도가 즉각 붕괴됩니다.

---

## STEP 1 — 정합성 검증 함수 추가

### 대상 파일: `agent/langgraph_agent.py` (또는 공통 유틸 파일)

각 노드 LLM 응답을 파싱한 직후, `reasoning` 텍스트와 결과 필드를 교차 검증합니다.
모순이 감지되면 LLM에게 재생성을 요청합니다 (최대 1회 재시도).

```python
from dataclasses import dataclass
from typing import Any

@dataclass
class ConsistencyCheckResult:
    is_consistent: bool
    conflict_description: str  # 모순 내용 설명 (재시도 프롬프트에 활용)

def check_reasoning_consistency(node_name: str, output: Any) -> ConsistencyCheckResult:
    """
    reasoning 텍스트와 실제 결과 필드 간 모순을 감지.
    LLM에게 재시도를 요청하기 위한 conflict_description을 반환.
    """
    reasoning = (output.reasoning or "").lower()
    
    # --- Critic 노드 ---
    if node_name == "critic" and hasattr(output, "recommend_hold"):
        hold_signals   = ["보류", "hold", "위반", "문제", "검토 필요", "부적합", "중단"]
        pass_signals   = ["정상", "통과", "pass", "적합", "문제없", "이상없", "승인"]
        
        reasoning_says_hold = any(s in reasoning for s in hold_signals)
        reasoning_says_pass = any(s in reasoning for s in pass_signals)
        
        if output.recommend_hold and reasoning_says_pass and not reasoning_says_hold:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description=(
                    f"reasoning은 '정상/통과' 취지로 작성되었으나 "
                    f"recommend_hold=True가 반환되었습니다. "
                    f"reasoning과 결과값이 일치하도록 재작성하십시오."
                )
            )
        if not output.recommend_hold and reasoning_says_hold and not reasoning_says_pass:
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description=(
                    f"reasoning은 '보류/위반' 취지로 작성되었으나 "
                    f"recommend_hold=False가 반환되었습니다. "
                    f"reasoning과 결과값이 일치하도록 재작성하십시오."
                )
            )
    
    # --- Verifier 노드 ---
    if node_name == "verify" and hasattr(output, "gate_result"):
        if output.gate_result == "PASS" and any(s in reasoning for s in ["실패", "위반", "불일치", "fail"]):
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description=(
                    f"reasoning은 검증 실패 취지이나 gate_result=PASS가 반환되었습니다. "
                    f"일치하도록 재작성하십시오."
                )
            )
        if output.gate_result == "FAIL" and any(s in reasoning for s in ["통과", "적합", "pass", "문제없"]):
            return ConsistencyCheckResult(
                is_consistent=False,
                conflict_description=(
                    f"reasoning은 검증 통과 취지이나 gate_result=FAIL이 반환되었습니다. "
                    f"일치하도록 재작성하십시오."
                )
            )
    
    # --- Reporter / Finalizer 노드 ---
    if node_name in ("reporter", "finalizer") and hasattr(output, "final_status"):
        high_risk_status = ["HITL_REQUIRED", "REJECT", "HOLD"]
        low_risk_signals = ["정상", "이상없", "통과", "승인", "문제없"]
        
        if output.final_status in high_risk_status:
            if any(s in reasoning for s in low_risk_signals) and \
               not any(s in reasoning for s in ["위반", "문제", "보류", "검토"]):
                return ConsistencyCheckResult(
                    is_consistent=False,
                    conflict_description=(
                        f"reasoning은 정상 처리 취지이나 "
                        f"final_status={output.final_status}이 반환되었습니다. "
                        f"일치하도록 재작성하십시오."
                    )
                )
    
    return ConsistencyCheckResult(is_consistent=True, conflict_description="")
```

---

## STEP 2 — 모순 감지 시 자동 재시도 로직

### 대상 파일: `agent/langgraph_agent.py`

각 노드의 LLM 호출 직후 정합성 검사를 수행하고, 모순이 있으면 1회 재시도합니다.

```python
async def call_node_llm_with_consistency_check(
    node_name: str,
    prompt: str,
    response_model: type,
    event_queue: asyncio.Queue,
    max_retries: int = 1
) -> Any:
    """
    LLM 호출 → 정합성 검사 → 모순 시 재시도 (최대 1회).
    reasoning과 결과값이 일치하는 응답만 스트림에 반영.
    """
    for attempt in range(max_retries + 1):
        output = await stream_node_reasoning(
            node_name=node_name,
            prompt=prompt,
            response_model=response_model,
            event_queue=event_queue
        )
        
        check = check_reasoning_consistency(node_name, output)
        
        if check.is_consistent:
            return output
        
        if attempt < max_retries:
            # 재시도 전 UI에 알림 (선택사항)
            await event_queue.put({
                "type": "THINKING_RETRY",
                "node": node_name,
                "reason": "추론 재검토 중..."  # 사용자에게는 이렇게만 표시
            })
            
            # 재시도 프롬프트에 모순 설명 추가
            prompt += f"\n\n[재작성 요청]\n{check.conflict_description}"
        else:
            # 최대 재시도 초과 — 그대로 사용하되 로그 기록
            import logging
            logging.warning(
                f"[{node_name}] reasoning 정합성 실패 (재시도 초과): "
                f"{check.conflict_description}"
            )
    
    return output
```

---

## STEP 3 — THINKING_RETRY 이벤트 UI 처리

### 대상 파일: `ui/workspace.py`

재시도 시 UI에 자연스럽게 "재검토 중" 상태를 표시합니다.
사용자에게는 에이전트가 스스로 검토하는 모습으로 보입니다.

```python
# workspace.py 메인 스트리밍 루프에 분기 추가
elif event["type"] == "THINKING_RETRY":
    node = event["node"]
    # 현재 타이핑 카드를 "재검토" 상태로 교체
    current_thinking_placeholder.markdown(
        f"""
        <div class="thinking-stream-card" style="border-color:#f59e0b; background:#1a1200;">
          <div class="thinking-node-label" style="color:#f59e0b;">
            🔄 {NODE_LABEL_MAP.get(node, node)} · 재검토 중
          </div>
          <div class="thinking-text-content" style="color:#9ca3af; font-style:italic;">
            판단 결과를 다시 검토하고 있습니다...
          </div>
        </div>
        """,
        unsafe_allow_html=True
    )
    # thinking_buffer 초기화 — 재시도 토큰이 새로 채워짐
    thinking_buffer[node] = ""
```

---

## 최종 동작 흐름

```
critic_node LLM 호출 (stream=True)
    ↓
reasoning 토큰 실시간 타이핑 출력
    ↓
응답 완료 → check_reasoning_consistency() 실행
    ↓
[일치] → 그대로 다음 노드 진행
[불일치] → UI에 "재검토 중" 표시 → 수정된 프롬프트로 재호출
    ↓
재시도 reasoning 타이핑 출력 → 결과 확정
```

시연 중 화면에 타이핑된 reasoning과 최종 판정 결과가
**구조적으로 항상 일치**함이 보장됩니다.

---

## 작업 체크리스트

- [ ] `check_reasoning_consistency()` 함수 추가 (critic, verify, reporter/finalizer 커버)
- [ ] `call_node_llm_with_consistency_check()` 래퍼로 각 노드 LLM 호출 교체
- [ ] `THINKING_RETRY` SSE 이벤트 타입 추가
- [ ] `workspace.py` 메인 루프에 `THINKING_RETRY` 분기 추가 (노란색 "재검토 중" 카드)
- [ ] 재시도 초과 시 경고 로그 확인 (시연 전 로그 점검 필수)
- [ ] 시연 테스트: critic이 HOLD 판정 시 reasoning에도 보류 근거가 명시되는지 확인

