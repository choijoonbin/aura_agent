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

**문제 1**: `source_label`에 `gpt-4o-mini`가 매 이벤트마다 반복 표시.

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