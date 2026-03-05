from __future__ import annotations

import html
import json
from collections import defaultdict
from typing import Any, Iterator

import requests
import streamlit as st
from ui.shared import stylable_container

from ui.api_client import API, get, post
from ui.shared import (
    budget_exceeded_display,
    case_type_badge,
    case_type_display_name,
    fmt_dt,
    fmt_dt_korea,
    fmt_num,
    hr_status_display_name,
    mcc_display_name,
    render_empty_state,
    render_kpi_card,
    render_page_header,
    render_panel_header,
    severity_badge,
    severity_display_name,
    status_badge,
    status_display_name,
)


def _format_agent_event_line(obj: dict[str, Any]) -> str:
    node = obj.get("node") or "agent"
    event_type = obj.get("event_type") or "event"
    parts = [f"[{node}/{event_type}] {obj.get('message') or ''}"]
    if obj.get("thought"):
        parts.append(f"  - 생각: {obj['thought']}")
    if obj.get("action"):
        parts.append(f"  - 행동: {obj['action']}")
    if obj.get("observation"):
        parts.append(f"  - 관찰: {obj['observation']}")
    return "\n".join(parts) + "\n"


# workspace.md B-6: 이벤트 타입별 아이콘 (TOOL_CALL은 ⚡로 통일)
EVENT_ICON_MAP = {
    "NODE_START": "🤖",
    "NODE_END": "✅",
    "TOOL_CALL": "⚡",
    "TOOL_RESULT": "📊",
    "TOOL_SKIPPED": "⏭️",
    "HITL_PAUSE": "⏸️",
    "HITL_REQUESTED": "⏸️",
    "GATE_APPLIED": "🔒",
    "PLAN_READY": "📋",
    "SCORE_BREAKDOWN": "📈",
    "FINAL_VERDICT": "⚖️",
    "SCREENING_RESULT": "📋",
    "THINKING_TOKEN": "💭",
    "THINKING_DONE": "💭",
    "THINKING_RETRY": "🔄",
}

PIPELINE_NODES = [
    ("screener", "스크리닝"),
    ("intake", "정보 수집"),
    ("planner", "계획 수립"),
    ("execute", "도구 실행"),
    ("critic", "비판 검토"),
    ("verify", "정책 검증"),
    ("reporter", "보고서 생성"),
    ("finalizer", "최종 판정"),
]


def _tool_caption_fragment(ev_type: str, tool: str | None, tool_description: str | None, html_tooltip: bool = False) -> str:
    """TOOL_* 이벤트용 캡션 조각: 'TOOL_CALL: toolname' 형식. html_tooltip=True이면 도구명에 title 툴팁."""
    if not tool or str(ev_type).upper() not in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED"}:
        return ""
    if html_tooltip and tool_description:
        desc = (tool_description or "").replace('"', "&quot;").replace("<", "&lt;")
        return f"{ev_type}: <span title=\"{desc}\">{tool}</span>"
    return f"{ev_type}: {tool}"


def _stream_card_chunks(obj: dict[str, Any]) -> Iterator[str]:
    """Yields markdown card content in chunks for typing effect. Card = header + message (word-by-word) + 생각/행동/관찰."""
    ts = fmt_dt_korea(obj.get("timestamp")) or "-"
    node = obj.get("node") or "agent"
    ev_type = obj.get("event_type") or "event"
    tool = obj.get("tool")
    meta = obj.get("metadata") or {}
    note_source = meta.get("note_source", "")
    note_model = meta.get("note_model") or ""
    source_label = f"{note_model}" if note_source == "llm" and note_model else ("LLM" if note_source == "llm" else "정의문구" if note_source == "fallback" else "")
    tool_desc = meta.get("tool_description")

    part2 = f"{node} / {ev_type}"
    tool_frag = _tool_caption_fragment(ev_type, tool, tool_desc, html_tooltip=False)
    if tool_frag:
        part2 = f"{node} / {tool_frag}"
    header = f"**{ts}** · {part2}  \n"
    if source_label:
        header += f"*{source_label}*  \n"
    yield header

    message = (obj.get("message") or "").strip()
    if message:
        for word in message.split():
            yield word + " "
        yield "  \n\n"

    thought = (obj.get("thought") or "").strip()
    action = (obj.get("action") or "").strip()
    observation = (obj.get("observation") or "").strip()
    if thought or action or observation:
        if thought:
            yield f"**생각** {thought}  \n\n"
        if action:
            yield f"**행동** {action}  \n\n"
        if observation:
            yield f"**관찰** {observation}  \n\n"
    yield "---  \n\n"


def sse_text_stream(stream_url: str, *, run_id: str | None = None) -> Iterator[str]:
    with requests.get(stream_url, stream=True, timeout=300) as response:
        response.raise_for_status()
        event = None
        first_event = True
        thinking_node: str | None = None
        for raw in response.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload = line.split(":", 1)[1].strip()
            if payload == "[DONE]":
                yield "\n\n**분석 스트림 종료**\n"
                break
            try:
                obj = json.loads(payload)
                if event == "AGENT_EVENT":
                    ev_type = str(obj.get("event_type") or "").upper()
                    if ev_type == "THINKING_TOKEN":
                        token = (obj.get("metadata") or {}).get("token") or ""
                        node = obj.get("node") or "agent"
                        if thinking_node is None:
                            if not first_event:
                                yield "\n\n---\n\n"
                            ts = fmt_dt_korea(obj.get("timestamp")) or "-"
                            yield f"**{ts}** · {node} / 추론  \n"
                            thinking_node = node
                        if token:
                            yield token
                        first_event = False
                    elif ev_type == "THINKING_DONE":
                        yield "  \n\n---  \n\n"
                        thinking_node = None
                        first_event = False
                    elif ev_type == "THINKING_RETRY":
                        # 재검토 이벤트: 현재 thinking 카드를 닫고, 재검토 안내를 한 줄로 표시한 뒤 다음 토큰을 새 카드로 받는다.
                        if thinking_node is not None:
                            yield "  \n\n---  \n\n"
                            thinking_node = None
                        if not first_event:
                            yield "\n\n---\n\n"
                        ts = fmt_dt_korea(obj.get("timestamp")) or "-"
                        node = obj.get("node") or "agent"
                        yield f"**{ts}** · {node} / 재검토  \n"
                        yield "_판단 결과와 추론 문구 정합성을 다시 맞추는 중..._  \n\n"
                        first_event = False
                    else:
                        if thinking_node is not None:
                            yield "  \n\n---  \n\n"
                            thinking_node = None
                        if not first_event:
                            yield "\n\n---\n\n"
                        for chunk in _stream_card_chunks(obj):
                            yield chunk
                        first_event = False
                        if ev_type == "HITL_PAUSE":
                            if run_id:
                                st.session_state[_hitl_state_key("dismissed", run_id)] = False
                                st.session_state[_hitl_state_key("shown", run_id)] = True
                                st.session_state[_hitl_state_key("open", run_id)] = True
                            yield "\n\n**[최종]** 담당자 검토 입력을 기다립니다.\n"
                            break
                elif event == "completed":
                    final_text = obj.get("reasonText") or obj.get("summary") or "완료"
                    result = obj.get("result") or {}
                    status = str(result.get("status") or obj.get("status") or "").upper()
                    if status == "HITL_REQUIRED" and run_id:
                        st.session_state[_hitl_state_key("dismissed", run_id)] = False
                        st.session_state[_hitl_state_key("shown", run_id)] = True
                        st.session_state[_hitl_state_key("open", run_id)] = True
                        yield f"\n\n**[최종]** {final_text}\n"
                        break
                    yield f"\n\n**[최종]** {final_text}\n"
                elif event == "failed":
                    yield f"\n\n**[실패]** {obj.get('error', 'unknown error')}\n"
                else:
                    detail = obj.get("detail") or obj.get("message") or obj.get("content") or payload
                    yield f"[{event}] {detail}\n"
            except Exception:
                yield f"[{event}] {payload}\n"


def fetch_case_bundle(voucher_key: str) -> dict[str, Any]:
    latest = get(f"/api/v1/cases/{voucher_key}/analysis/latest")
    history = get(f"/api/v1/cases/{voucher_key}/analysis/history")
    if latest.get("run_id"):
        events = get(f"/api/v1/analysis-runs/{latest['run_id']}/events")
        latest["timeline"] = events.get("events") or []
    else:
        latest["timeline"] = []
    latest["history"] = history.get("items") or []
    return latest


def summarize_tool_results(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for tool in tool_results:
        skill = tool.get("skill") or "unknown"
        facts = tool.get("facts") or {}
        entry = {"skill": skill, "detail": tool.get("summary") or "-"}
        if skill == "policy_rulebook_probe":
            refs = facts.get("policy_refs") or []
            entry.update(
                metric_label="규정 근거",
                metric_value=f"{len(refs)}건",
                detail=", ".join(filter(None, [ref.get("article") for ref in refs[:3]])) or "-",
            )
        elif skill == "document_evidence_probe":
            entry.update(metric_label="전표 라인", metric_value=f"{facts.get('lineItemCount', 0)}건", detail="수집 완료")
        elif skill == "legacy_aura_deep_audit":
            entry.update(metric_label="전문감사", metric_value="실행", detail=((facts.get("reasonText") or facts.get("summary") or "-")[:80]))
        else:
            details = [f"{k}={facts.get(k)}" for k in ("holidayRisk", "budgetExceeded", "merchantRisk") if k in facts]
            entry.update(metric_label="확인 결과", metric_value="OK" if tool.get("ok") else "CHECK", detail=", ".join(details) or "-")
        cards.append(entry)
    return cards


def render_tool_trace_summary(tool_results: list[dict[str, Any]]) -> None:
    cards = summarize_tool_results(tool_results)
    if not cards:
        render_empty_state("도구 실행 요약이 없습니다.")
        return
    cols = st.columns(min(3, len(cards)))
    for idx, card in enumerate(cards):
        with cols[idx % len(cols)]:
            with stylable_container(key=f"tool_summary_{idx}", css_styles="""{padding: 16px 18px; border-radius: 16px; border: 1px solid #e5e7eb; background: #fff; box-shadow: 0 8px 22px rgba(15,23,42,0.04); min-height: 158px;}"""):
                st.caption(card["skill"])
                st.markdown(f"**{card['metric_label']}**")
                st.subheader(card["metric_value"])
                st.caption(card["detail"])


def render_pipeline_progress(completed_nodes: list[str], current_node: str) -> None:
    """workspace.md B-3: 파이프라인 진행 상태 바."""
    total = len(PIPELINE_NODES)
    completed_count = len(completed_nodes)
    progress_pct = (completed_count / total * 100) if total else 0
    node_html_parts = []
    for node_id, node_label in PIPELINE_NODES:
        if node_id in completed_nodes:
            status_class, icon = "done", "✓"
        elif node_id == current_node:
            status_class, icon = "active", "◉"
        else:
            status_class, icon = "pending", "○"
        node_html_parts.append(f"""
        <div class="pipeline-node {status_class}">
          <div class="pipeline-dot">{icon}</div>
          <div class="pipeline-label">{node_label}</div>
        </div>
        """)
    st.markdown(f"""
    <style>
    .pipeline-wrapper {{ background: #0d1117; border: 1px solid #1e2d3d; border-radius: 12px; padding: 16px 20px; margin-bottom: 16px; }}
    .pipeline-track {{ display: flex; align-items: center; justify-content: space-between; position: relative; flex-wrap: wrap; gap: 8px; }}
    .pipeline-track::before {{ content: ''; position: absolute; top: 14px; left: 0; right: 0; height: 2px; background: #1e2d3d; z-index: 0; }}
    .pipeline-node {{ display: flex; flex-direction: column; align-items: center; gap: 6px; position: relative; z-index: 1; min-width: 48px; }}
    .pipeline-dot {{ width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 700; }}
    .pipeline-node.done .pipeline-dot {{ background: #22c55e; color: #000; }}
    .pipeline-node.active .pipeline-dot {{ background: #3b82f6; color: #fff; box-shadow: 0 0 12px #3b82f6aa; animation: pulse-dot 1.2s ease-in-out infinite; }}
    .pipeline-node.pending .pipeline-dot {{ background: #1e2d3d; color: #4b5563; border: 2px solid #1e2d3d; }}
    .pipeline-label {{ font-size: 10px; letter-spacing: 0.5px; color: #6b7280; text-align: center; white-space: nowrap; }}
    .pipeline-node.done .pipeline-label {{ color: #22c55e; }}
    .pipeline-node.active .pipeline-label {{ color: #3b82f6; font-weight: 700; }}
    @keyframes pulse-dot {{ 0%, 100% {{ box-shadow: 0 0 8px #3b82f6aa; }} 50% {{ box-shadow: 0 0 20px #3b82f6; }} }}
    .pipeline-progress-bar {{ height: 3px; background: linear-gradient(to right, #22c55e {progress_pct:.0f}%, #1e2d3d {progress_pct:.0f}%); border-radius: 2px; margin-top: 12px; }}
    .pipeline-meta {{ display: flex; justify-content: space-between; margin-top: 8px; font-size: 11px; color: #4b5563; }}
    </style>
    <div class="pipeline-wrapper">
      <div class="pipeline-track">
        {''.join(node_html_parts)}
      </div>
      <div class="pipeline-progress-bar"></div>
      <div class="pipeline-meta">
        <span>{completed_count}/{total} 단계 완료</span>
        <span>{'분석 완료' if completed_count == total else ('진행 중: ' + current_node) if current_node else ''}</span>
      </div>
    </div>
    """, unsafe_allow_html=True)


def render_score_breakdown_card(policy_score: int, evidence_score: int, final_score: int) -> None:
    """workspace.md B-7: Confidence Score 인라인 게이지 바."""
    def _bar(label: str, value: int, color: str) -> str:
        return f"""
        <div style="margin: 8px 0;">
          <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:4px;">
            <span style="color:#9ca3af;">{label}</span>
            <span style="color:{color}; font-weight:700;">{value}</span>
          </div>
          <div style="background:#1e2d3d; border-radius:4px; height:8px;">
            <div style="width:{min(100, value)}%; background:{color}; height:8px; border-radius:4px; transition:width 0.8s ease;"></div>
          </div>
        </div>
        """
    final_color = "#22c55e" if final_score >= 70 else "#f59e0b" if final_score >= 50 else "#ef4444"
    st.markdown(
        f"""
        <div style="background:#0d1117; border:1px solid #1e2d3d; border-radius:10px; padding:16px; margin:10px 0;">
          <div style="font-size:12px; font-weight:700; letter-spacing:1px; color:#6b7280; margin-bottom:12px;">CONFIDENCE SCORE</div>
          {_bar('정책 점수', policy_score, '#3b82f6')}
          {_bar('근거 점수', evidence_score, '#8b5cf6')}
          {_bar('최종 점수', final_score, final_color)}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _thinking_row_html(label: str, icon: str, content: str, row_class: str, border_color: str) -> str:
    if not content:
        return ""
    esc = html.escape(content)
    return f"""
    <div class="thinking-row {row_class}" style="border-left-color:{border_color}">
      <span class="thinking-icon">{icon}</span>
      <div class="thinking-content">
        <span class="thinking-label">{label}</span>
        <p>{esc}</p>
      </div>
    </div>
    """


def _pipeline_state_from_events(events: list[dict[str, Any]]) -> tuple[list[str], str]:
    """이벤트 목록에서 완료된 노드와 현재 노드 추출."""
    all_ids = [n[0] for n in PIPELINE_NODES]
    completed = []
    for node_id, _ in PIPELINE_NODES:
        if any(
            (e.get("payload") or {}).get("node") == node_id
            and str((e.get("payload") or {}).get("event_type") or "").upper() == "NODE_END"
            for e in events
        ):
            completed.append(node_id)
    if not completed:
        current = all_ids[0]
    else:
        idx = all_ids.index(completed[-1]) if completed[-1] in all_ids else 0
        current = all_ids[idx + 1] if idx + 1 < len(all_ids) else all_ids[-1]
    return completed, current


def render_timeline_cards(events: list[dict[str, Any]], *, view_mode: str = "business") -> None:
    if not events:
        render_empty_state("표시할 스트림 이벤트가 없습니다.")
        return
    completed_nodes, current_node = _pipeline_state_from_events(events)
    render_pipeline_progress(completed_nodes, current_node)
    node_labels_map = dict(PIPELINE_NODES)
    node_groups = defaultdict(list)
    node_order = []
    for event in events:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        node = (payload.get("node") or "unknown").lower()
        if node not in node_groups:
            node_order.append(node)
        node_groups[node].append(event)
    latest_node = node_order[-1] if node_order else None
    st.markdown("""
    <style>
    .thinking-row { display: flex; align-items: flex-start; gap: 12px; padding: 10px 14px; margin: 6px 0; border-radius: 8px; border-left: 3px solid; }
    .thinking-row.thought { background: #0f1a2e; border-color: #3b82f6; }
    .thinking-row.action { background: #0f2a1a; border-color: #22c55e; }
    .thinking-row.observation { background: #1a1500; border-color: #f59e0b; }
    .thinking-icon { font-size: 18px; margin-top: 2px; flex-shrink: 0; }
    .thinking-label { font-size: 10px; font-weight: 700; letter-spacing: 1.2px; text-transform: uppercase; opacity: 0.6; display: block; margin-bottom: 4px; }
    .thinking-content p { margin: 0; font-size: 14px; line-height: 1.6; color: #e2e8f0; }
    </style>
    """, unsafe_allow_html=True)
    with stylable_container(key="timeline_shell", css_styles="""{background: radial-gradient(circle at 1px 1px, rgba(15,23,42,0.10) 1px, transparent 0); background-size: 14px 14px; background-color:#f8fafc; border:1px dashed #dbe2ea; border-radius:18px; padding:14px;}"""):
        for node in node_order:
            node_events = node_groups[node]
            node_label = node_labels_map.get(node, node)
            is_latest = node == latest_node
            with st.expander(
                label=f"{'▶ ' if is_latest else '✓ '}{node_label}  ({len(node_events)}개 이벤트)",
                expanded=is_latest,
            ):
                for index, event in enumerate(node_events):
                    payload = event.get("payload") or {}
                    meta = payload.get("metadata") or {}
                    ev_type = str(payload.get("event_type") or "").upper()
                    icon = EVENT_ICON_MAP.get(ev_type, "🤖")
                    part2 = f"{payload.get('node') or '-'} / {ev_type}"
                    tool_frag = _tool_caption_fragment(ev_type, payload.get("tool"), meta.get("tool_description"), html_tooltip=True)
                    if tool_frag:
                        part2 = f"{payload.get('node') or '-'} / {tool_frag}"
                    cap = f"{icon} {fmt_dt_korea(event.get('at') or payload.get('timestamp')) or '-'} · {part2}"
                    st.caption(cap, unsafe_allow_html=True)
                    if ev_type == "SCORE_BREAKDOWN":
                        sb = meta.get("score_breakdown") or meta
                        policy_score = int(sb.get("policy_score") or sb.get("policy_score_raw") or 0)
                        evidence_score = int(sb.get("evidence_score") or sb.get("evidence_score_raw") or 0)
                        final_score = int(sb.get("final_score") or sb.get("score") or 0)
                        render_score_breakdown_card(policy_score, evidence_score, final_score)
                    if payload.get("message"):
                        st.write(payload["message"])
                    thought = (payload.get("thought") or "").strip()
                    action = (payload.get("action") or "").strip()
                    observation = (payload.get("observation") or "").strip()
                    blocks = []
                    blocks.append(_thinking_row_html("판단", "🧠", thought, "thought", "#3b82f6"))
                    blocks.append(_thinking_row_html("실행", "⚡", action, "action", "#22c55e"))
                    blocks.append(_thinking_row_html("발견", "🔍", observation, "observation", "#f59e0b"))
                    combined = "".join(blocks)
                    if combined:
                        st.markdown(f"<div class=\"thinking-block\">{combined}</div>", unsafe_allow_html=True)
                    if view_mode == "debug":
                        st.json(payload)


# 대표 메시지 선택 우선순위 (docs/work_info/langgraphPlan3.md 추가답변)
_REPR_MSG_PRIORITY = ["NODE_END", "GATE_APPLIED", "TOOL_RESULT", "PLAN_READY", "NODE_START"]


def _pick_representative_message(bucket: dict[str, Any]) -> str:
    """우선순위에 따라 대표 메시지 1개 선택. NODE_END.message > GATE_APPLIED.message > TOOL_RESULT.observation > PLAN_READY.message > NODE_START.message"""
    by_type = bucket.get("by_type") or {}
    for ev in _REPR_MSG_PRIORITY:
        v = by_type.get(ev)
        if v and str(v).strip():
            return str(v).strip()
    msgs = bucket.get("messages") or []
    return msgs[-1] if msgs else "-"


def summarize_process_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    node_order = ["screener", "intake", "planner", "execute", "critic", "verify", "hitl_pause", "reporter", "finalizer"]
    node_labels = {
        "screener": "전표 분석 / 케이스 분류",
        "intake": "입력 해석",
        "planner": "조사 계획 수립",
        "execute": "근거 수집 실행",
        "critic": "비판적 검토",
        "verify": "검증 및 HITL 판단",
        "hitl_pause": "HITL 대기 (run 조기 종료)",
        "reporter": "보고 문장 생성",
        "finalizer": "결과 확정",
    }
    grouped: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        node = str(payload.get("node") or "").lower()
        if node not in node_labels:
            continue
        bucket = grouped.setdefault(
            node,
            {
                "node": node,
                "label": node_labels[node],
                "started_at": event.get("at") or payload.get("timestamp") or "-",
                "messages": [],
                "thoughts": [],
                "actions": [],
                "observations": [],
                "tool_count": 0,
                "last_event_type": None,
                "by_type": {},
                "first_tool": None,
                "last_tool": None,
            },
        )
        ev_type = str(payload.get("event_type") or "").upper()
        if ev_type == "SCREENING_RESULT":
            bucket["last_event_type"] = "NODE_END"
        else:
            bucket["last_event_type"] = ev_type
        message = payload.get("message")
        thought = payload.get("thought")
        action = payload.get("action")
        observation = payload.get("observation")
        if message:
            bucket["messages"].append(str(message))
        if thought:
            bucket["thoughts"].append(str(thought))
        if action:
            bucket["actions"].append(str(action))
        if observation:
            bucket["observations"].append(str(observation))
        if ev_type in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED"}:
            bucket["tool_count"] += 1
            tool_name = payload.get("tool") or ""
            if tool_name:
                if bucket["first_tool"] is None:
                    bucket["first_tool"] = tool_name
                bucket["last_tool"] = tool_name
        if ev_type == "NODE_END" and message:
            bucket.setdefault("by_type", {})["NODE_END"] = message
        elif ev_type == "GATE_APPLIED" and message:
            bucket.setdefault("by_type", {})["GATE_APPLIED"] = message
        elif ev_type == "TOOL_RESULT" and observation:
            bucket.setdefault("by_type", {})["TOOL_RESULT"] = observation
        elif ev_type == "PLAN_READY" and message:
            bucket.setdefault("by_type", {})["PLAN_READY"] = message
        elif ev_type == "NODE_START" and message:
            bucket.setdefault("by_type", {})["NODE_START"] = message

    result = []
    for node in node_order:
        if node not in grouped:
            continue
        bucket = grouped[node]
        result.append(
            {
                "node": node,
                "label": bucket["label"],
                "started_at": bucket["started_at"],
                "summary": _pick_representative_message(bucket),
                "thought": (bucket["thoughts"][-1] if bucket["thoughts"] else ""),
                "action": (bucket["actions"][-1] if bucket["actions"] else ""),
                "observation": (bucket["observations"][-1] if bucket["observations"] else ""),
                "tool_count": bucket["tool_count"],
                "first_tool": bucket.get("first_tool"),
                "last_tool": bucket.get("last_tool"),
                "last_event_type": bucket["last_event_type"] or "-",
            }
        )
    return result


def render_process_story(events: list[dict[str, Any]], *, debug_mode: bool = False) -> None:
    rows = summarize_process_timeline(events)
    if not rows:
        render_empty_state("분석 완료 후 핵심 판단 흐름을 이 영역에서 단계별로 요약합니다.")
        return
    for idx, row in enumerate(rows, start=1):
        with stylable_container(
            key=f"process_story_{idx}_{row['node']}",
            css_styles="""{padding: 15px 16px; border-radius: 16px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.98); box-shadow: 0 8px 22px rgba(15,23,42,0.04); margin-bottom: 0.7rem;}""",
        ):
            top_left, top_right = st.columns([0.78, 0.22])
            with top_left:
                st.markdown(f"**{idx}. {row['label']}**")
                st.caption(fmt_dt_korea(row.get("started_at")) or "-")
            with top_right:
                st.markdown(
                    status_badge("COMPLETED" if str(row["last_event_type"]).upper() in {"NODE_END", "REPORT_READY", "RESULT_FINALIZED"} else "IN_REVIEW"),
                    unsafe_allow_html=True,
                )
            st.write(row["summary"])
            meta = []
            if row.get("tool_count"):
                meta.append(f"도구 호출 {row['tool_count']}건")
            if row.get("first_tool") or row.get("last_tool"):
                ft, lt = row.get("first_tool"), row.get("last_tool")
                if ft and lt and ft != lt:
                    meta.append(f"첫 도구: {ft} → 마지막: {lt}")
                elif ft:
                    meta.append(f"도구: {ft}")
            if row.get("observation"):
                meta.append(f"관찰: {(row['observation'] or '')[:80]}{'…' if len(str(row.get('observation') or '')) > 80 else ''}")
            if meta:
                st.caption(" · ".join(meta))
            detail_cols = st.columns(2)
            with detail_cols[0]:
                if row["thought"]:
                    st.caption("핵심 판단")
                    st.write(row["thought"])
            with detail_cols[1]:
                if row["action"]:
                    st.caption("수행 행동")
                    st.write(row["action"])
            if debug_mode:
                st.json(row)


def render_hitl_history(history: list[dict[str, Any]]) -> None:
    rows = [item for item in history if item.get("hitl_request") or item.get("hitl_response")]
    if not rows:
        render_empty_state("HITL 이력이 없습니다.")
        return
    for idx, item in enumerate(rows):
        with stylable_container(key=f"hitl_history_{idx}", css_styles="""{padding: 14px 16px; border-radius: 16px; border: 1px solid #e5e7eb; background: #fff; margin-bottom: 10px;}"""):
            st.markdown(f"**run_id** `{item.get('run_id')}`")
            lineage = item.get("lineage") or {}
            st.caption(f"mode={lineage.get('mode') or '-'} / parent={lineage.get('parent_run_id') or '-'}")
            if item.get("hitl_request"):
                st.markdown("**요청**")
                st.json(item["hitl_request"])
            if item.get("hitl_response"):
                st.markdown("**응답**")
                st.json(item["hitl_response"])


def _extract_workspace_result_context(latest_bundle: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    result = dict(((latest_bundle.get("result") or {}).get("result") or {}))
    timeline = latest_bundle.get("timeline") or []
    screening_meta: dict[str, Any] = {}
    derived_policy_refs: list[dict[str, Any]] = []
    for event in timeline:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        if payload.get("event_type") == "SCREENING_RESULT":
            screening_meta = payload.get("metadata") or {}
        if payload.get("event_type") == "TOOL_RESULT":
            meta = payload.get("metadata") or {}
            if meta.get("skill") == "policy_rulebook_probe":
                derived_policy_refs = ((meta.get("facts") or {}).get("policy_refs") or [])
    if not result.get("severity") and screening_meta.get("severity"):
        result["severity"] = screening_meta.get("severity")
    if not result.get("score") and screening_meta.get("score") is not None:
        result["score"] = screening_meta.get("score")
    if not result.get("status") and latest_bundle.get("hitl_request"):
        result["status"] = "HITL_REQUIRED"
    return result, screening_meta, result.get("policy_refs") or derived_policy_refs or []


def _has_pending_hitl(latest_bundle: dict[str, Any]) -> bool:
    result = ((latest_bundle.get("result") or {}).get("result") or {})
    timeline = latest_bundle.get("timeline") or []
    if latest_bundle.get("hitl_response"):
        return False
    if latest_bundle.get("hitl_request"):
        return True
    if str(result.get("status") or "").upper() == "HITL_REQUIRED":
        return True
    for event in timeline:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        if str(payload.get("event_type") or "").upper() in {"HITL_REQUESTED", "HITL_PAUSE"}:
            return True
    return False


def _hitl_state_key(kind: str, run_id: str | None) -> str:
    return f"mt_hitl_{kind}_{run_id or 'unknown'}"


def _prime_hitl_form_state(run_id: str, latest_bundle: dict[str, Any]) -> dict[str, str]:
    draft = latest_bundle.get("hitl_draft") or latest_bundle.get("hitl_response") or {}
    defaults = {
        "reviewer": draft.get("reviewer") or "FINANCE_REVIEWER",
        "business_purpose": draft.get("business_purpose") or "",
        "attendees": ", ".join(draft.get("attendees") or []),
        "comment": draft.get("comment") or "",
        "decision": "승인 가능" if draft.get("approved") is True else "보류/추가 검토",
    }
    state_keys = {
        "reviewer": _hitl_state_key("reviewer", run_id),
        "business_purpose": _hitl_state_key("business_purpose", run_id),
        "attendees": _hitl_state_key("attendees", run_id),
        "comment": _hitl_state_key("comment", run_id),
        "decision": _hitl_state_key("decision", run_id),
    }
    for field, key in state_keys.items():
        if key not in st.session_state:
            st.session_state[key] = defaults[field]
    return state_keys


def _fallback_hitl_request(latest_bundle: dict[str, Any]) -> dict[str, Any]:
    result, screening_meta, _policy_refs = _extract_workspace_result_context(latest_bundle)
    verification_summary = result.get("verification_summary") or {}
    reasons = []
    if screening_meta.get("reasonText"):
        reasons.append(str(screening_meta.get("reasonText")))
    gate_policy = verification_summary.get("gate_policy")
    if gate_policy:
        reasons.append(f"검증 게이트 판정: {gate_policy}")
    quality_codes = result.get("quality_gate_codes") or []
    if quality_codes:
        reasons.append(f"품질 신호: {', '.join(str(x) for x in quality_codes)}")
    if not reasons:
        reasons.append("HITL payload가 생성되지 않았지만 현재 run 상태상 담당자 검토가 필요한 것으로 표시되었습니다.")
    return {
        "required": True,
        "handoff": "FINANCE_REVIEWER",
        "why_hitl": reasons[0],
        "blocking_gate": str(gate_policy or "HITL_REQUIRED"),
        "blocking_reason": reasons[0],
        "reasons": reasons,
        "auto_finalize_blockers": reasons,
        "review_questions": [],
        "questions": [],
        "required_inputs": [],
        "evidence_snapshot": [],
        "fallback_generated": True,
    }


def _build_hitl_summary_sections(latest_bundle: dict[str, Any]) -> dict[str, list[str]]:
    result, screening_meta, policy_refs = _extract_workspace_result_context(latest_bundle)
    hitl_request = latest_bundle.get("hitl_request") or {}
    verification_summary = result.get("verification_summary") or {}

    review_reasons = [str(x) for x in (hitl_request.get("unresolved_claims") or hitl_request.get("reasons") or []) if x]
    if not review_reasons:
        if hitl_request.get("why_hitl"):
            review_reasons = [str(hitl_request.get("why_hitl"))]
        elif screening_meta.get("reasonText"):
            review_reasons = [str(screening_meta.get("reasonText"))]
        else:
            review_reasons = ["검토 필요 사유 데이터가 비어 있습니다."]

    stop_reasons: list[str] = [str(x) for x in (hitl_request.get("auto_finalize_blockers") or []) if x]
    if not stop_reasons:
        gate_policy = verification_summary.get("gate_policy")
        if gate_policy:
            stop_reasons.append(f"검증 게이트 판정: {gate_policy}")
        quality_codes = result.get("quality_gate_codes") or []
        if quality_codes:
            stop_reasons.append(f"검증 신호: {', '.join(str(x) for x in quality_codes)}")
        if not stop_reasons and hitl_request.get("blocking_reason"):
            stop_reasons.append(str(hitl_request.get("blocking_reason")))
    if not stop_reasons:
        stop_reasons = ["자동 확정 중단 사유 데이터가 비어 있습니다."]

    questions = [str(x) for x in (hitl_request.get("review_questions") or hitl_request.get("questions") or []) if x]
    if not questions:
        required_inputs = hitl_request.get("required_inputs") or []
        questions = [f"{item.get('field')}: {item.get('reason')}" for item in required_inputs if item.get("field") and item.get("reason")]
    if not questions:
        questions = ["검토자가 답해야 할 질문 데이터가 비어 있습니다."]

    evidence_lines: list[str] = []
    snapshot = hitl_request.get("evidence_snapshot") or []
    for item in snapshot:
        label = item.get("label")
        value = item.get("value")
        if label and value:
            evidence_lines.append(f"{label}: {value}")
    if not evidence_lines:
        case_type = screening_meta.get("caseType") or screening_meta.get("case_type") or result.get("case_type")
        if case_type:
            evidence_lines.append(f"위험 유형: {case_type_display_name(case_type)}")
        if result.get("severity"):
            evidence_lines.append(f"심각도: {severity_display_name(result.get('severity'))}")
        if result.get("score") is not None:
            evidence_lines.append(f"점수: {result.get('score')}")
        if screening_meta.get("reasonText"):
            evidence_lines.append(f"스크리닝 요약: {screening_meta.get('reasonText')}")
        if verification_summary:
            covered = verification_summary.get("covered", 0)
            total = verification_summary.get("total", 0)
            evidence_lines.append(f"검증 대상 문장: {covered}/{total}건 근거 연결")
        if policy_refs:
            ref_preview = [f"{ref.get('article') or '-'} / {ref.get('parent_title') or '-'}" for ref in policy_refs[:3]]
            evidence_lines.append("연결 규정: " + " | ".join(ref_preview))
    if hitl_request.get("handoff"):
        evidence_lines.append(f"검토 담당: {hitl_request.get('handoff')}")
    if not evidence_lines:
        evidence_lines = ["현재 확보된 근거 요약 데이터가 비어 있습니다."]

    return {
        "review_reasons": review_reasons,
        "stop_reasons": stop_reasons,
        "questions": questions,
        "evidence_lines": evidence_lines,
        "debug": {
            "hitl_request": hitl_request,
            "verification_summary": verification_summary,
            "quality_gate_codes": result.get("quality_gate_codes") or [],
        },
    }


def render_hitl_panel(latest_bundle: dict[str, Any]) -> None:
    run_id = latest_bundle.get("run_id")
    hitl_request = latest_bundle.get("hitl_request") or _fallback_hitl_request(latest_bundle)
    if not run_id or not _has_pending_hitl(latest_bundle):
        return
    form_keys = _prime_hitl_form_state(run_id, latest_bundle)
    st.markdown(
        """
        <style>
        /* 팝업 전체 기본 텍스트 색상: 하얀 배경에서 검정 계열로 가독성 확보 */
        div[data-testid="stDialog"],
        div[data-testid="stDialog"] * {
          color: #0f172a !important;
        }
        div[data-testid="stDialog"] div[role="dialog"] {
          width: min(94vw, 1520px) !important;
          max-width: 1520px !important;
          max-height: 90vh !important;
          overflow: hidden !important;
          background: #ffffff !important;
        }
        div[data-testid="stDialog"] div[role="dialog"] > div {
          width: 100% !important;
          max-width: 1520px !important;
          max-height: 90vh !important;
          overflow-y: auto !important;
          background: #ffffff !important;
        }
        div[data-testid="stDialog"] [data-testid="stVerticalBlock"] {
          max-width: 100% !important;
        }
        div[data-testid="stDialog"] [data-testid="stVerticalBlock"] > div:first-child {
          margin-top: 0 !important;
          padding-top: 0 !important;
        }
        /* 제목과 본문 사이 간격 축소 */
        div[data-testid="stDialog"] [data-testid="stDialogHeader"],
        div[data-testid="stDialog"] div[role="dialog"] > div:first-child {
          margin-bottom: 0 !important;
          padding-bottom: 0 !important;
        }
        div[data-testid="stDialog"] div[role="dialog"] > div:last-child {
          margin-top: 0 !important;
          padding-top: 0.125rem !important;
        }
        div[data-testid="stDialog"] button[aria-label="Close"] {
          color: #0f172a !important;
          background: #ffffff !important;
          border: 1px solid #cbd5e1 !important;
          border-radius: 999px !important;
          width: 32px !important;
          height: 32px !important;
        }
        div[data-testid="stDialog"] button[aria-label="Close"]:hover {
          background: #f8fafc !important;
          border-color: #94a3b8 !important;
        }
        /* 제목·본문·라벨 등 모든 텍스트 노드 검정 계열 (#0f172a) */
        div[data-testid="stDialog"] h1,
        div[data-testid="stDialog"] h2,
        div[data-testid="stDialog"] h3,
        div[data-testid="stDialog"] [data-testid="stDialogHeader"],
        div[data-testid="stDialog"] [data-testid="stDialogHeader"] *,
        div[data-testid="stDialog"] [data-testid="stHeading"],
        div[data-testid="stDialog"] [data-testid="stHeading"] *,
        div[data-testid="stDialog"] [data-testid="stMarkdown"],
        div[data-testid="stDialog"] [data-testid="stMarkdown"] *,
        div[data-testid="stDialog"] p,
        div[data-testid="stDialog"] label,
        div[data-testid="stDialog"] span,
        div[data-testid="stDialog"] small {
          color: #0f172a !important;
        }
        /* 검토 요청 원본 보기: 배경 흰색, 텍스트 검정 */
        div[data-testid="stDialog"] details,
        div[data-testid="stDialog"] details summary,
        div[data-testid="stDialog"] [data-testid="stExpanderDetails"],
        div[data-testid="stDialog"] [data-testid="stExpanderDetails"] * {
          background: #ffffff !important;
          color: #0f172a !important;
        }
        div[data-testid="stDialog"] [data-testid="stJson"],
        div[data-testid="stDialog"] [data-testid="stJson"] *,
        div[data-testid="stDialog"] [data-testid="stJson"] pre,
        div[data-testid="stDialog"] [data-testid="stJson"] code {
          background: #ffffff !important;
          color: #0f172a !important;
        }
        /* 검토 요청 원본 보기 행: expander 오른쪽 끝을 위 컴포넌트(검토 의견) 오른쪽 끝과 맞춤 */
        div[data-testid="stDialog"] [data-testid="stForm"] [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"]:last-child {
          display: flex !important;
          align-items: stretch !important;
          gap: 0 !important;
        }
        div[data-testid="stDialog"] [data-testid="stForm"] [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"]:last-child > div:first-child {
          flex: 1 1 0% !important;
          min-width: 0 !important;
          max-width: none !important;
        }
        div[data-testid="stDialog"] [data-testid="stForm"] [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"]:last-child > div:last-child {
          flex: 0 0 auto !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    with stylable_container(
        key=f"workspace_hitl_skin_{run_id}",
        css_styles="""
        {
          border: 1px solid #e5e7eb;
          border-radius: 18px;
          background: rgba(255,255,255,0.98);
          padding: 6px 16px 10px 16px;
          box-shadow: 0 8px 22px rgba(15,23,42,0.05);
        }
        .mt-hitl-note {
          margin: 0 0 4px 0;
          padding: 8px 12px;
          border-radius: 14px;
          border: 1px solid #bfdbfe;
          background: #eff6ff;
          color: #1e3a8a;
          font-size: 0.89rem;
          line-height: 1.35;
        }
        .mt-hitl-grid {
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: 10px;
          margin: 4px 0 10px 0;
        }
        .mt-hitl-box {
          border: 1px solid #e5e7eb;
          border-radius: 16px;
          background: #ffffff;
          padding: 12px 13px;
          height: 300px;
          min-height: 300px;
          display: flex;
          flex-direction: column;
        }
        .mt-hitl-box--reason {
          border-color: #fecaca;
          background: #fff7f7;
        }
        .mt-hitl-box--stop {
          border-color: #fde68a;
          background: #fffbeb;
        }
        .mt-hitl-box--question {
          border-color: #bfdbfe;
          background: #eff6ff;
        }
        .mt-hitl-box--evidence {
          border-color: #c7d2fe;
          background: #f5f3ff;
        }
        .mt-hitl-box-title {
          color: #0f172a;
          font-size: 0.92rem;
          font-weight: 700;
          margin-bottom: 6px;
          display: flex;
          align-items: center;
          gap: 6px;
        }
        .mt-hitl-icon {
          width: 22px;
          height: 22px;
          border-radius: 999px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          font-size: 0.8rem;
          font-weight: 700;
          flex-shrink: 0;
        }
        .mt-hitl-icon--reason {
          background: #fee2e2;
          color: #b91c1c;
        }
        .mt-hitl-icon--stop {
          background: #fef3c7;
          color: #92400e;
        }
        .mt-hitl-icon--question {
          background: #dbeafe;
          color: #1d4ed8;
        }
        .mt-hitl-icon--evidence {
          background: #ede9fe;
          color: #6d28d9;
        }
        .mt-hitl-list {
          margin: 0;
          padding-left: 16px;
          color: #334155;
          line-height: 1.45;
          font-size: 0.88rem;
          flex: 1 1 auto;
          min-height: 0;
          overflow-y: auto;
          padding-right: 4px;
        }
        .mt-hitl-list li + li {
          margin-top: 4px;
        }
        .mt-hitl-checklist {
          margin-top: 8px;
          padding: 8px 10px;
          border-radius: 12px;
          background: rgba(255,255,255,0.82);
          border: 1px dashed #cbd5e1;
        }
        .mt-hitl-checklist-title {
          color: #0f172a;
          font-size: 0.84rem;
          font-weight: 700;
          margin-bottom: 4px;
        }
        .mt-hitl-checklist ul {
          margin: 0;
          padding-left: 18px;
          color: #475569;
          font-size: 0.84rem;
          line-height: 1.4;
        }
        .mt-hitl-form-grid {
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: 10px;
          margin: 4px 0 8px 0;
        }
        .mt-hitl-actions {
          display: grid;
          grid-template-columns: 1fr auto 1fr;
          align-items: center;
          gap: 12px;
          margin-top: 10px;
        }
        .mt-hitl-actions-left {
          justify-self: start;
          min-width: 0;
        }
        .mt-hitl-actions-center {
          justify-self: center;
          width: 280px;
        }
        div[data-baseweb="radio"] label,
        div[role="radiogroup"] label,
        div[role="radiogroup"] * {
          color: #0f172a !important;
        }
        [data-testid="stTextInput"] label,
        [data-testid="stTextArea"] label,
        [data-testid="stCheckbox"] label,
        [data-testid="stCheckbox"] span {
          color: #0f172a !important;
        }
        [data-testid="stTextInput"] input,
        [data-testid="stTextArea"] textarea {
          background: #ffffff !important;
          color: #0f172a !important;
          -webkit-text-fill-color: #0f172a !important;
          caret-color: #0f172a !important;
          border: 1px solid #cbd5e1 !important;
          border-radius: 12px !important;
        }
        [data-testid="stTextInput"] input::placeholder,
        [data-testid="stTextArea"] textarea::placeholder {
          color: #64748b !important;
          opacity: 1 !important;
        }
        [data-testid="stTextInput"] > div,
        [data-testid="stTextArea"] > div {
          background: transparent !important;
        }
        [data-testid="stCheckbox"] > div {
          background: transparent !important;
        }
        [data-testid="stJson"],
        [data-testid="stJson"] *,
        [data-testid="stJson"] pre,
        [data-testid="stJson"] code,
        [data-testid="stJson"] pre * {
          background: #ffffff !important;
          color: #0f172a !important;
        }
        [data-testid="stAlert"] {
          background: #fffbeb !important;
          border: 1px solid #fde68a !important;
          color: #92400e !important;
        }
        [data-testid="stAlert"] * {
          color: #92400e !important;
        }
        div[data-testid="stDialog"] [data-testid="stFormSubmitButton"] button,
        div[data-testid="stDialog"] button[kind="primary"],
        div[data-testid="stDialog"] button[data-testid="baseButton-primary"] {
          background: #2563eb !important;
          color: #ffffff !important;
          border: 1px solid #2563eb !important;
        }
        div[data-testid="stDialog"] [data-testid="stFormSubmitButton"] button:hover,
        div[data-testid="stDialog"] button[kind="primary"]:hover,
        div[data-testid="stDialog"] button[data-testid="baseButton-primary"]:hover {
          background: #1d4ed8 !important;
          border-color: #1d4ed8 !important;
        }
        div[data-testid="stDialog"] button[kind="secondary"],
        div[data-testid="stDialog"] button[data-testid="baseButton-secondary"] {
          background: #ffffff !important;
          color: #0f172a !important;
          border: 1px solid #cbd5e1 !important;
        }
        div[data-testid="stDialog"] details,
        div[data-testid="stDialog"] details * {
          color: #0f172a !important;
        }
        div[data-testid="stDialog"] details summary {
          color: #0f172a !important;
        }
        div[data-testid="stDialog"] details > div,
        div[data-testid="stDialog"] details [data-testid="stExpanderDetails"] {
          background: #ffffff !important;
        }
        @media (max-width: 1200px) {
          .mt-hitl-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
          }
        }
        @media (max-width: 900px) {
          .mt-hitl-form-grid {
            grid-template-columns: 1fr;
          }
          .mt-hitl-actions {
            grid-template-columns: 1fr;
          }
          .mt-hitl-actions-center {
            width: 100%;
          }
        }
        """,
    ):
        summary = _build_hitl_summary_sections(latest_bundle)
        lead_message = (
            hitl_request.get("why_hitl")
            or hitl_request.get("blocking_reason")
            or "담당자 검토가 필요한 상태입니다."
        )
        st.markdown(
            f'<div class="mt-hitl-note"><strong>담당자 검토가 필요한 상태입니다.</strong> {lead_message}</div>',
            unsafe_allow_html=True,
        )

        def _render_box(title: str, lines: list[str], tone: str, icon: str) -> str:
            items = "".join(f"<li>{line}</li>" for line in lines)
            return (
                f'<div class="mt-hitl-box mt-hitl-box--{tone}">'
                f'<div class="mt-hitl-box-title"><span class="mt-hitl-icon mt-hitl-icon--{tone}">{icon}</span>{title}</div>'
                f'<ul class="mt-hitl-list">{items}</ul>'
                "</div>"
            )

        st.markdown(
            '<div class="mt-hitl-grid">'
            + _render_box(
                "검토 필요 사유",
                summary["review_reasons"],
                "reason",
                "!",
            )
            + _render_box(
                "자동 확정 중단 이유",
                summary["stop_reasons"],
                "stop",
                "■",
            )
            + _render_box(
                "검토자가 답해야 할 질문",
                summary["questions"],
                "question",
                "?",
            )
            + _render_box(
                "현재 확보된 근거 요약",
                summary["evidence_lines"],
                "evidence",
                "i",
            )
            + '</div>',
            unsafe_allow_html=True,
        )
        with st.form(key=f"hitl_form_{run_id}"):
            decision = st.radio(
                "판단 선택",
                options=["보류/추가 검토", "승인 가능"],
                horizontal=True,
                label_visibility="collapsed",
                key=form_keys["decision"],
            )
            info_cols = st.columns(3)
            with info_cols[0]:
                reviewer = st.text_input("검토자", key=form_keys["reviewer"])
            with info_cols[1]:
                business_purpose = st.text_input("업무 목적", key=form_keys["business_purpose"], placeholder="예: 주말 장애 대응 회의")
            with info_cols[2]:
                attendees_raw = st.text_input("참석자(쉼표 구분)", key=form_keys["attendees"], placeholder="예: 홍길동, 김민수, 외부 파트너 1명")
            comment = st.text_area(
                "검토 의견",
                height=96,
                key=form_keys["comment"],
                placeholder="왜 승인 또는 보류로 판단했는지 핵심 근거를 적습니다.\n예: 주말 대응 프로젝트로 야간 회의 후 식대 사용. 사전 승인 메일 확인됨.",
            )
            approved = decision == "승인 가능"
            row_cols = st.columns([1, 0.22])
            with row_cols[0]:
                with st.expander("검토 요청 원본 보기", expanded=False):
                    st.json(summary["debug"])
            with row_cols[1]:
                submitted = st.form_submit_button("검토 응답 제출 후 재분석")
    if submitted:
        response = post(
            f"/api/v1/analysis-runs/{run_id}/hitl",
            json_body={
                "reviewer": reviewer,
                "comment": comment,
                "approved": approved,
                "business_purpose": business_purpose,
                "attendees": [p.strip() for p in attendees_raw.split(",") if p.strip()],
            },
        )
        resumed_id = response.get("resumed_run_id") or response.get("run_id") or run_id
        st.session_state.pop(_hitl_state_key("dismissed", run_id), None)
        st.session_state.pop(_hitl_state_key("open", run_id), None)
        st.session_state.pop(_hitl_state_key("shown", run_id), None)
        st.session_state["mt_resume_stream"] = {
            "run_id": resumed_id,
            "stream_path": response.get("stream_path"),
            "voucher_key": latest_bundle.get("voucher_key"),
        }
        st.success(f"HITL 응답 저장 완료: run_id={resumed_id}")
        st.rerun()


@st.dialog("HITL 검토 요청", width="large")
def render_hitl_dialog(latest_bundle: dict[str, Any]) -> None:
    render_hitl_panel(latest_bundle)


def build_workspace_plan_steps(latest_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    timeline = latest_bundle.get("timeline") or []
    node_order = ["screener", "intake", "planner", "execute", "critic", "verify", "hitl_pause", "reporter", "finalizer"]
    meta = {
        "screener": ("전표 분석 / 케이스 분류", "전표 데이터(발생 시각·근태·가맹점 업종 코드(MCC)·예산 등)를 분석해 위반 유형을 식별합니다."),
        "intake": ("입력 해석", "전표 입력값과 위험 지표를 정규화합니다."),
        "planner": ("조사 계획 수립", "검증할 사실과 사용할 skill 순서를 계획합니다."),
        "execute": ("근거 수집 실행", "휴일/예산/업종/전표/규정 근거를 실제로 조회합니다."),
        "critic": ("비판적 검토", "과잉 주장과 반례 가능성을 다시 점검합니다."),
        "verify": ("검증 및 HITL 판단", "자동 판정 가능 여부와 담당자 검토 필요 여부를 결정합니다."),
        "hitl_pause": ("HITL 대기", "담당자 검토 필요로 일시정지. HITL 응답 후 같은 run(thread)으로 재개됩니다."),
        "reporter": ("보고 문장 생성", "근거 중심 설명 문장과 최종 요약을 만듭니다."),
        "finalizer": ("결과 확정", "상태, 점수, 이력, 저장 payload를 최종 확정합니다."),
    }
    seen: set[str] = set()
    completed: set[str] = set()
    running = None
    for event in timeline:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        node = str(payload.get("node") or "").lower()
        event_type = str(payload.get("event_type") or "").upper()
        if node in meta:
            seen.add(node)
            if event_type in {"NODE_END", "COMPLETE", "REPORT_READY", "RESULT_FINALIZED", "HITL_PAUSE", "THINKING_DONE"}:
                completed.add(node)
            if event_type in {"NODE_START", "PLAN_READY", "TOOL_CALL", "TOOL_RESULT", "THINKING_TOKEN", "THINKING_RETRY"}:
                running = node
    steps = []
    for order, node in enumerate(node_order, start=1):
        title, description = meta[node]
        status = "완료" if node in completed else "진행중" if node == running else "수행" if node in seen else "대기"
        steps.append({"order": order, "title": title, "description": description, "status": status})
    return steps


def build_workspace_execution_logs(latest_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for event in latest_bundle.get("timeline") or []:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        event_type = str(payload.get("event_type") or "").upper()
        if event_type not in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED", "HITL_REQUESTED", "SCREENING_RESULT"}:
            continue
        rows.append(
            {
                "at": event.get("at") or payload.get("timestamp") or "-",
                "node": payload.get("node") or "-",
                "event_type": event_type,
                "tool": payload.get("tool") or "-",
                "message": payload.get("message") or "-",
                "observation": payload.get("observation") or "",
            }
        )
    return rows

def render_workspace_case_queue(items: list[dict[str, Any]], selected_key: str | None) -> None:
    render_panel_header("케이스", "분석할 전표를 선택합니다. 좌측은 선택, 우측은 실시간 실행과 판단 리뷰에 집중합니다.")
    review_count = len(
        [item for item in items if str(item.get("case_status") or "").upper() in {"NEW", "IN_REVIEW", "REVIEW_REQUIRED", "HITL_REQUIRED"}]
    )
    completed_count = len(
        [item for item in items if str(item.get("case_status") or "").upper() in {"COMPLETED", "COMPLETED_AFTER_HITL", "RESOLVED", "OK"}]
    )
    hitl_count = len(
        [item for item in items if str(item.get("case_status") or "").upper() in {"HITL_REQUIRED", "REVIEW_AFTER_HITL", "HOLD_AFTER_HITL"}]
    )
    st.markdown(
        f"""
        <div class="mt-workspace-case-stats">
          <div class="mt-workspace-case-stat"><div class="mt-workspace-case-stat-value">{len(items)}</div><div class="mt-workspace-case-stat-label">전체 케이스</div></div>
          <div class="mt-workspace-case-stat"><div class="mt-workspace-case-stat-value">{review_count}</div><div class="mt-workspace-case-stat-label">검토 필요</div></div>
          <div class="mt-workspace-case-stat"><div class="mt-workspace-case-stat-value">{completed_count}</div><div class="mt-workspace-case-stat-label">완료</div></div>
          <div class="mt-workspace-case-stat"><div class="mt-workspace-case-stat-value">{hitl_count}</div><div class="mt-workspace-case-stat-label">HITL 대기</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    tabs = st.tabs(["전체", "검토 필요", "완료", "HITL 대기"])
    grouped = {
        "전체": items,
        "검토 필요": [item for item in items if str(item.get("case_status") or "").upper() in {"NEW", "IN_REVIEW", "REVIEW_REQUIRED", "HITL_REQUIRED"}],
        "완료": [item for item in items if str(item.get("case_status") or "").upper() in {"COMPLETED", "COMPLETED_AFTER_HITL", "RESOLVED", "OK"}],
        "HITL 대기": [item for item in items if str(item.get("case_status") or "").upper() in {"HITL_REQUIRED", "REVIEW_AFTER_HITL", "HOLD_AFTER_HITL"}],
    }
    # 배지는 st.markdown(HTML) → 시각 레이어 (pointer-events: none)
    # 버튼은 margin-top:-55px 으로 배지 영역까지 올려 클릭 영역이 카드 전체를 커버
    st.markdown("""
    <style>
    [class*="st-key-workspace_case_scroll_"] {
      max-height: 66vh !important; overflow-y: auto !important; padding-right: 6px !important;
    }
    /* 카드 컨테이너 — 시각적 카드 외형 제공, 좌우 패딩 최소화 */
    [class*="st-key-case_btn_"] {
      border: 1px solid #e5e7eb !important;
      border-radius: 18px !important;
      background: rgba(255,255,255,0.98) !important;
      box-shadow: 0 8px 22px rgba(15,23,42,0.04) !important;
      margin-bottom: 0.8rem !important;
      padding: 10px 10px 0 10px !important;
      transition: box-shadow 0.15s ease !important;
    }
    [class*="st-key-case_btn_sel_"] {
      border: 2px solid #2563eb !important;
      box-shadow: 0 0 0 3px rgba(37,99,235,0.08), 0 12px 26px rgba(15,23,42,0.08) !important;
    }
    /* stVerticalBlock flex gap 제거 — 배지↔버튼 사이 공백 최소화 */
    [class*="st-key-case_btn_"] [data-testid="stVerticalBlock"] {
      gap: 0 !important;
    }
    /* 배지 행 — 위에 보이지만 클릭 비활성, 하단 마진 제거 */
    [class*="st-key-case_btn_"] [data-testid="stMarkdown"] {
      position: relative !important;
      z-index: 2 !important;
      pointer-events: none !important;
      margin-bottom: 0 !important;
    }
    /* 버튼 element-container — 배지 바로 아래까지 끌어올려 공백 최소화 */
    [class*="st-key-case_btn_"] [data-testid="element-container"]:last-child {
      margin-top: -42px !important;
      position: relative !important;
      z-index: 1 !important;
    }
    /* 실제 button — 투명, 카드 컨텐츠 텍스트 스타일, 배지 높이만큼만 위 패딩 */
    [class*="st-key-case_btn_"] [data-testid="stButton"] > button {
      width: 100% !important;
      height: auto !important;
      min-height: unset !important;
      text-align: left !important;
      padding: 42px 0 12px 0 !important;
      padding-left: 0 !important;
      border: none !important;
      background: transparent !important;
      box-shadow: none !important;
      color: #0f172a !important;
      font-size: 0.9rem !important;
      white-space: pre-wrap !important;
      line-height: 1.6 !important;
      cursor: pointer !important;
      margin: 0 !important;
    }
    /* 버튼 내부 마크다운/문단 왼쪽 여백 완전 제거 */
    [class*="st-key-case_btn_"] [data-testid="stButton"] [data-testid="stMarkdownContainer"],
    [class*="st-key-case_btn_"] [data-testid="stButton"] [data-testid="stMarkdownContainer"] p,
    [class*="st-key-case_btn_"] [data-testid="stButton"] > button > div {
      padding-left: 0 !important;
      margin-left: 0 !important;
    }
    [class*="st-key-case_btn_"] [data-testid="stMarkdownContainer"] p { margin: 0 !important; }
    </style>
    """, unsafe_allow_html=True)
    for idx, (tab, label) in enumerate(zip(tabs, ["전체", "검토 필요", "완료", "HITL 대기"])):
        with tab:
            with st.container(key=f"workspace_case_scroll_{idx}"):
                if not grouped[label]:
                    render_empty_state("표시할 케이스가 없습니다.")
                    continue
                for item in grouped[label]:
                    case_key = item["voucher_key"]
                    is_selected = case_key == selected_key
                    status = status_display_name(item.get("case_status"))
                    severity = severity_display_name(item.get("severity"))
                    case_type = case_type_display_name(item.get("case_type"))
                    occurred_at = fmt_dt(item.get("occurred_at")) or "-"
                    amount = f"{fmt_num(item.get('amount'))} {item.get('currency') or ''}".strip()
                    merchant = item.get("merchant_name") or "-"
                    title = item.get("demo_name") or merchant
                    wrap_key = f"case_btn_sel_{idx}_{case_key}" if is_selected else f"case_btn_{idx}_{case_key}"
                    with st.container(key=wrap_key):
                        # 배지 행 — HTML 유지, pointer-events:none 으로 클릭 투과
                        st.markdown(
                            f'<div style="display:flex;gap:6px;flex-wrap:wrap;">'
                            f'{status_badge(item.get("case_status"))}'
                            f'{severity_badge(item.get("severity"))}'
                            f'{case_type_badge(item.get("case_type"))}'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                        # 버튼 — margin-top:-55px 으로 배지 위까지 클릭 영역 확장
                        btn_label = (
                            f"**{title}**\n\n"
                            f"{amount} · {occurred_at}\n\n"
                            f"전표키　{case_key}　　가맹점　{merchant}"
                        )
                        if st.button(
                            btn_label,
                            key=f"select_{idx}_{case_key}",
                            use_container_width=True,
                        ):
                            st.session_state["mt_selected_voucher"] = case_key
                            st.rerun()


def render_workspace_chat_panel(selected: dict[str, Any], latest_bundle: dict[str, Any]) -> None:
    result = ((latest_bundle.get("result") or {}).get("result") or {})
    timeline = latest_bundle.get("timeline") or []
    selected_vkey = selected.get("voucher_key") or ""
    render_panel_header("에이전트 대화", "선택한 전표에 대해 LangGraph가 현재 무엇을 하고 있는지 실시간으로 보여줍니다.")

    vkey = selected_vkey
    is_unscreened = str(selected.get("case_type") or "").upper() == "UNSCREENED"
    current_status = result.get("status") or selected.get("case_status") or "-"
    current_severity = result.get("severity") or selected.get("severity")
    live_run_id = latest_bundle.get("run_id") or "-"
    strip_text = (
        "분석 시작 시 자동으로 케이스 유형을 분류합니다."
        if is_unscreened
        else f"스크리닝 완료 · {case_type_display_name(selected.get('case_type'))} · 심각도 {severity_display_name(selected.get('severity'))}"
    )
    summary_html = f"""
    <div class="mt-workspace-summary">
      <div class="mt-workspace-hero">
        <div>{status_badge(result.get("status") if result else selected.get("case_status"))}{severity_badge(result.get("severity") if result else selected.get("severity"))}{case_type_badge(selected.get("case_type"))}</div>
        <div class="mt-workspace-hero-title">{selected.get("demo_name") or selected.get("merchant_name") or "선택 전표"}</div>
        <div class="mt-workspace-hero-sub">실시간 스트림은 planning, tool 실행, 검증 게이트, HITL 요청, 최종 결론까지 공개 가능한 이벤트만 표시합니다.</div>
        <div class="mt-workspace-inline-meta">
          <div class="mt-workspace-inline-item"><span class="mt-workspace-inline-label">전표키</span>{selected.get("voucher_key") or "-"}</div>
          <div class="mt-workspace-inline-item"><span class="mt-workspace-inline-label">가맹점</span>{selected.get("merchant_name") or "-"}</div>
          <div class="mt-workspace-inline-item"><span class="mt-workspace-inline-label">금액</span>{fmt_num(selected.get('amount'))} {selected.get('currency') or ''}</div>
          <div class="mt-workspace-inline-item"><span class="mt-workspace-inline-label">발생일시</span>{fmt_dt(selected.get("occurred_at")) or "-"}</div>
        </div>
        <div class="mt-workspace-strip">{strip_text}</div>
      </div>
      <div class="mt-workspace-action">
        <div>
          <div class="mt-workspace-action-title">실행 상태</div>
          <div class="mt-workspace-action-top">이 영역은 현재 선택한 전표의 최신 run 상태와 다음 액션을 한 번에 제시합니다.</div>
          <div class="mt-workspace-action-meta">
            <div class="mt-workspace-action-key">현재 상태</div><div class="mt-workspace-action-value">{status_display_name(current_status)}</div>
            <div class="mt-workspace-action-key">심각도</div><div class="mt-workspace-action-value">{severity_display_name(current_severity)}</div>
            <div class="mt-workspace-action-key">실행 run</div><div class="mt-workspace-action-value">{str(live_run_id)[:12] + "…" if isinstance(live_run_id, str) and len(live_run_id) > 12 else live_run_id}</div>
            <div class="mt-workspace-action-key">다음 액션</div><div class="mt-workspace-action-value">분석 시작 또는 검토 재개</div>
          </div>
        </div>
      </div>
    </div>
    """
    st.markdown(summary_html, unsafe_allow_html=True)
    _cta_l, cta_button_col = st.columns([0.78, 0.22])
    with cta_button_col:
        run_clicked = st.button("분석 시작", key=f"workspace_run_{vkey}", use_container_width=True, type="primary")
    if run_clicked:
        response = post(f"/api/v1/cases/{vkey}/analysis-runs")
        run_id = response["run_id"]
        st.session_state.pop(_hitl_state_key("dismissed", run_id), None)
        st.session_state.pop(_hitl_state_key("open", run_id), None)
        st.session_state.pop(_hitl_state_key("shown", run_id), None)
        st.success(f"분석 시작: run_id={response['run_id']}")
        st.write_stream(sse_text_stream(f"{API}{response['stream_path']}", run_id=run_id))
        st.rerun()

    resume_stream = st.session_state.get("mt_resume_stream")
    if (
        resume_stream
        and resume_stream.get("voucher_key") == selected_vkey
        and resume_stream.get("stream_path")
        and resume_stream.get("run_id")
    ):
        run_id = str(resume_stream["run_id"])
        stream_path = str(resume_stream["stream_path"])
        st.session_state.pop("mt_resume_stream", None)
        st.success(f"HITL 응답 반영 후 재개: run_id={run_id}")
        st.write_stream(sse_text_stream(f"{API}{stream_path}", run_id=run_id))
        st.rerun()

    if _has_pending_hitl(latest_bundle):
        st.warning("이 분석은 담당자 검토가 필요합니다. 검토 의견을 입력하면 같은 run으로 재개됩니다.")
        _hl, hitl_btn_col, _hr = st.columns([0.01, 0.98, 0.01])
        run_id = latest_bundle.get("run_id")
        open_key = _hitl_state_key("open", run_id)
        dismissed_key = _hitl_state_key("dismissed", run_id)
        with hitl_btn_col:
            if st.button("HITL 검토 입력 열기", key=f"workspace_hitl_open_{vkey}", use_container_width=True):
                st.session_state[dismissed_key] = False
                st.session_state[open_key] = True
                st.rerun()
        if run_id:
            st.session_state.setdefault(dismissed_key, False)
            if st.session_state.get(open_key):
                st.session_state[open_key] = False
                render_hitl_dialog(latest_bundle)

    st.markdown('<div class="mt-stream-note">실시간 패널은 최신 이벤트 중심으로 유지합니다. 완료 후 상세 검토는 바로 아래 리뷰 탭에서 확인합니다.</div>', unsafe_allow_html=True)
    if not timeline:
        render_empty_state("분석을 시작하면 이 영역에 실시간 스트림이 표시됩니다.")
        return
    # THINKING_TOKEN은 스트림에서만 타이핑 효과로 표시; 타임라인에서는 제외하고 THINKING_DONE만 추론 카드로 표시
    ag = [e for e in timeline if e.get("event_type") == "AGENT_EVENT"]
    latest_events = [e for e in ag if str((e.get("payload") or {}).get("event_type") or "").upper() != "THINKING_TOKEN"][-8:]
    latest_nodes = []
    for event in latest_events:
        payload = event.get("payload") or {}
        node = payload.get("node")
        if node and node not in latest_nodes:
            latest_nodes.append(str(node))
    if latest_nodes:
        st.markdown(
            '<div class="mt-stream-stage-row">'
            + "".join(f'<span class="mt-stream-stage-pill">{node}</span>' for node in latest_nodes[:6])
            + "</div>",
            unsafe_allow_html=True,
        )
    if len(timeline) > 8:
        st.caption(f"최신 {min(8, len(timeline))}개 이벤트만 표시합니다. 전체 실행 검토는 아래 리뷰 탭에서 확인합니다.")
    with stylable_container(
        key=f"workspace_stream_shell_{selected_vkey}",
        css_styles=[
            """
            {
              background: radial-gradient(circle at 1px 1px, rgba(15,23,42,0.10) 1px, transparent 0);
              background-size: 14px 14px;
              background-color: #f8fafc;
              border: 1px dashed #dbe2ea;
              border-radius: 18px;
              padding: 14px;
              max-height: 46vh;
              overflow-y: auto;
              overflow-x: hidden;
            }
            """,
            """
            [data-testid="stVerticalBlock"] {
              max-height: 42vh;
              overflow-y: auto;
              overflow-x: hidden;
            }
            """,
        ],
    ):
        # role: PoC에서 도구 호출/결과는 "user"(사람 아이콘), 노드 진행은 "assistant"(로봇 아이콘)로 구분해 표시합니다.
        for idx, event in enumerate(latest_events):
            payload = event.get("payload") or {}
            ev_type = str(payload.get("event_type") or "").upper()
            role = "user" if ev_type in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED"} else "assistant"
            tool_name = payload.get("tool")
            meta = payload.get("metadata") or {}
            tool_desc = meta.get("tool_description")
            caption_first = f"{fmt_dt_korea(event.get('at') or payload.get('timestamp')) or '-'}"
            part2 = f"{payload.get('node') or '-'} / {ev_type}"
            if ev_type == "THINKING_DONE":
                part2 = f"{payload.get('node') or '-'} / 추론"
            tool_frag = _tool_caption_fragment(ev_type, tool_name, tool_desc, html_tooltip=True)
            if tool_frag:
                part2 = f"{payload.get('node') or '-'} / {tool_frag}"
            note_source = meta.get("note_source", "")
            note_model = meta.get("note_model") or ""
            source_label = note_model if note_source == "llm" and note_model else ("LLM" if note_source == "llm" else "정의문구" if note_source == "fallback" else "")
            with st.chat_message(role):
                st.caption(f"{caption_first} · {part2}", unsafe_allow_html=True)
                if source_label:
                    st.caption(f"*{source_label}*")
            # THINKING_DONE: 실제 노드 추론문을 메시지로 표시
                display_message = payload.get("message") or ""
                if ev_type == "THINKING_DONE":
                    display_message = meta.get("reasoning") or display_message
                if display_message:
                    st.write(display_message)
                cols = st.columns(3)
                if payload.get("thought"):
                    cols[0].caption("생각")
                    cols[0].write(payload["thought"])
                if payload.get("action"):
                    cols[1].caption("행동")
                    cols[1].write(payload["action"])
                if payload.get("observation"):
                    cols[2].caption("관찰")
                    cols[2].write(payload["observation"])


def render_workspace_results(latest_bundle: dict[str, Any], debug_mode: bool) -> None:
    result = dict(((latest_bundle.get("result") or {}).get("result") or {}))
    timeline = latest_bundle.get("timeline") or []

    screening_meta = {}
    derived_policy_refs: list[dict[str, Any]] = []
    for event in timeline:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        if payload.get("event_type") == "SCREENING_RESULT":
            screening_meta = payload.get("metadata") or {}
        if payload.get("event_type") == "TOOL_RESULT":
            meta = payload.get("metadata") or {}
            if meta.get("skill") == "policy_rulebook_probe":
                derived_policy_refs = ((meta.get("facts") or {}).get("policy_refs") or [])

    if not result.get("severity") and screening_meta.get("severity"):
        result["severity"] = screening_meta.get("severity")
    if not result.get("score") and screening_meta.get("score") is not None:
        result["score"] = screening_meta.get("score")
    if not result.get("status"):
        if result.get("error"):
            result["status"] = "FAILED"
        elif latest_bundle.get("hitl_request"):
            result["status"] = "HITL_REQUIRED"

    failed = bool(result.get("error"))
    hero_title = "분석 실패" if failed else (result.get("status") or "결과 없음")
    hero_sub = (
        f"{result.get('stage') or 'runner'} 단계에서 오류가 발생했습니다: {result.get('error')}"
        if failed
        else (result.get("reasonText") or "최종 판단이 아직 생성되지 않았습니다.")
    )
    render_panel_header("판단 요약", "최종 상태, 점수, 검증 요약을 먼저 보여주고 세부 근거와 진단은 아래에서 이어집니다.")
    st.markdown(
        f"""
        <div class="mt-card-quiet">
          <div class="mt-hero-title">{hero_title}</div>
          <div class="mt-hero-sub">{hero_sub}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    score_display = "-"
    score_val = result.get("score")
    if isinstance(score_val, float) and 0 <= score_val <= 1:
        score_display = f"{score_val:.2f}"
    else:
        score_display = str(score_val or "-")
    st.markdown(
        f"""
        <div class="mt-result-grid">
          <div class="mt-result-metric">
            <div class="mt-result-metric-label">상태</div>
            <div class="mt-result-metric-value">{"FAILED" if failed else str(result.get("status") or "-")}</div>
            <div class="mt-result-metric-foot">현재 run의 최종 상태</div>
          </div>
          <div class="mt-result-metric">
            <div class="mt-result-metric-label">심각도</div>
            <div class="mt-result-metric-value">{severity_display_name(result.get("severity")) if result.get("severity") else "-"}</div>
            <div class="mt-result-metric-foot">정책 및 증거를 합친 위험 수준</div>
          </div>
          <div class="mt-result-metric">
            <div class="mt-result-metric-label">점수</div>
            <div class="mt-result-metric-value">{score_display}</div>
            <div class="mt-result-metric-foot">최종 판단에 사용된 점수</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if failed and debug_mode:
        st.json(result)
    if result.get("score_breakdown"):
        sb = result["score_breakdown"]
        st.caption(f"정책점수 {sb.get('policy_score', '-')} · 근거점수 {sb.get('evidence_score', '-')} · 최종점수 {sb.get('final_score', '-')}")
    quality_codes = result.get("quality_gate_codes") or []
    if quality_codes:
        st.markdown("#### 품질 신호")
        st.markdown("".join(f'<span class="mt-badge mt-badge-amber">{code}</span>' for code in quality_codes), unsafe_allow_html=True)
    if result.get("hitl_request"):
        st.markdown("#### 담당자 검토 상태")
        st.caption("이 run은 자동 확정이 아니라 담당자 검토 이후 재개를 전제로 진행되었습니다.")
    if result.get("verification_summary"):
        verification_summary = result.get("verification_summary") or {}
        st.markdown("#### 검증 요약")
        st.caption(
            f"게이트 판정 {verification_summary.get('gate_policy') or '-'} · "
            f"근거 연결 {verification_summary.get('covered', 0)}/{verification_summary.get('total', 0)}"
        )
    if result.get("critique") and debug_mode:
        st.markdown("#### 검증 메모 (debug)")
        st.json(result.get("critique"))


def render_workspace_evidence_map(latest_bundle: dict[str, Any], debug_mode: bool) -> None:
    result = dict(((latest_bundle.get("result") or {}).get("result") or {}))
    timeline = latest_bundle.get("timeline") or []
    policy_refs = result.get("policy_refs") or []
    retrieval_snapshot = result.get("retrieval_snapshot") or {}
    if not policy_refs:
        for event in timeline:
            payload = event.get("payload") or {}
            if event.get("event_type") != "AGENT_EVENT":
                continue
            if payload.get("event_type") == "TOOL_RESULT":
                meta = payload.get("metadata") or {}
                if meta.get("skill") == "policy_rulebook_probe":
                    policy_refs = ((meta.get("facts") or {}).get("policy_refs") or [])
                    break

    render_panel_header("근거 맵", "최종 판단에 사용된 규정 근거와 citation 연결 상태를 검토합니다.")
    if not policy_refs:
        render_empty_state("연결된 규정 근거가 없습니다.")
        return

    st.markdown("#### 채택된 규정 근거")
    for idx, ref in enumerate(policy_refs, start=1):
        title = f"C{idx}. {ref.get('article') or '-'} / {ref.get('parent_title') or '-'}"
        with st.expander(title, expanded=(idx == 1)):
            meta = []
            if ref.get("retrieval_score") is not None:
                meta.append(f"score={ref.get('retrieval_score')}")
            if ref.get("source_strategy"):
                meta.append(str(ref.get("source_strategy")))
            if ref.get("adoption_reason"):
                meta.append(str(ref.get("adoption_reason")))
            if meta:
                st.caption(" · ".join(meta))
            st.write(ref.get("chunk_text") or "")
            if debug_mode:
                st.json(ref)

    verification_summary = result.get("verification_summary") or {}
    if verification_summary:
        st.markdown("#### 검증 현황")
        v1, v2, v3 = st.columns(3)
        with v1:
            ratio = verification_summary.get("coverage_ratio")
            st.metric("근거 연결률", f"{(ratio or 0) * 100:.0f}%" if ratio is not None else "-", f"{verification_summary.get('covered', 0)}/{verification_summary.get('total', 0)}")
        with v2:
            missing = verification_summary.get("missing_citations") or []
            st.metric("누락 citation", str(len(missing)))
        with v3:
            st.metric("게이트 판정", str(verification_summary.get("gate_policy") or "-"))
        if verification_summary.get("missing_citations"):
            with st.expander("누락된 검증 대상 문장", expanded=False):
                for i, s in enumerate(verification_summary["missing_citations"], 1):
                    st.caption(f"{i}. {(s or '')[:160]}{'…' if len(str(s or '')) > 160 else ''}")

    if retrieval_snapshot:
        with st.expander("Retrieval 인용 현황", expanded=False):
            candidates = retrieval_snapshot.get("candidates_after_rerank") or []
            adopted = retrieval_snapshot.get("adopted_citations") or []
            st.caption("after rerank 후보, 최종 채택 citation, 채택 이유를 함께 표시합니다.")
            st.caption(f"후보 청크 {len(candidates)}건 · 채택 인용 {len(adopted)}건")
            if adopted:
                st.markdown("**최종 채택 citation**")
                for i, c in enumerate(adopted[:10], 1):
                    art = c.get("article") or c.get("title") or "-"
                    reason = c.get("adoption_reason") or "규정 근거로 채택"
                    st.markdown(f"**{i}. {art}**  \n채택 이유: {reason}")
            if candidates:
                st.markdown("**후보 목록 (after rerank)**")
                for i, g in enumerate(candidates[:5], 1):
                    reason = g.get("adoption_reason")
                    line = f"{i}. {g.get('article') or '-'} · {g.get('parent_title') or '-'} (score={g.get('retrieval_score') or '-'})"
                    if reason:
                        line += f" — {reason}"
                    st.caption(line)


def render_workspace_execution_review(latest_bundle: dict[str, Any], debug_mode: bool) -> None:
    timeline = latest_bundle.get("timeline") or []
    tool_results = ((latest_bundle.get("result") or {}).get("result") or {}).get("tool_results") or []
    plan_steps = build_workspace_plan_steps(latest_bundle)
    exec_logs = build_workspace_execution_logs(latest_bundle)
    render_panel_header("실행 내역", "계획 수립, 도구 실행, 게이트 판정 등 실제 오케스트레이션 흔적을 요약해 보여줍니다.")

    if plan_steps:
        st.markdown("#### 작업 계획")
        for step in plan_steps:
            with stylable_container(key=f"plan_review_{latest_bundle.get('run_id')}_{step['order']}", css_styles="""{background:#fff; border:1px solid #e5e7eb; border-radius:16px; padding:0.9rem 1rem; margin-bottom:0.6rem;}"""):
                left_step, right_step = st.columns([0.78, 0.22])
                with left_step:
                    st.markdown(f"**{step['order']}. {step['title']}**")
                    st.caption(step["description"])
                with right_step:
                    st.markdown(status_badge(step["status"] if step["status"] != "진행중" else "IN_REVIEW"), unsafe_allow_html=True)

    st.markdown("#### 도구 실행 요약")
    render_tool_trace_summary(tool_results)

    if exec_logs:
        st.markdown("#### 주요 실행 이벤트")
        for idx, log in enumerate(exec_logs):
            with stylable_container(key=f"log_review_{idx}_{latest_bundle.get('run_id')}", css_styles="""{background:#f8fafc; border:1px solid #e5e7eb; border-radius:14px; padding:0.85rem 0.95rem; margin-bottom:0.55rem;}"""):
                st.caption(f"{fmt_dt_korea(log.get('at')) or '-'} · {log['node']} / {log['event_type']}")
                st.markdown(f"**{log['tool']}**")
                st.write(log["message"])
                if log["observation"]:
                    st.caption(log["observation"])
    else:
        render_empty_state("표시할 실행 이벤트가 없습니다.")

    if debug_mode and timeline:
        with st.expander("원본 타임라인 보기", expanded=False):
            render_process_story(timeline, debug_mode=True)


def render_workspace_review_history(latest_bundle: dict[str, Any]) -> None:
    render_panel_header("검토 이력", "HITL 요청, 담당자 검토 응답, 재개 이력을 run 단위로 확인합니다.")
    render_hitl_history(latest_bundle.get("history") or [])


def render_ai_workspace_page() -> None:
    render_page_header("AI 워크스페이스", "전표 기반 자율형 에이전트가 실제로 추론하고, 도구를 호출하고, 규정 근거를 바탕으로 판단하는 메인 시연 화면입니다.")
    items = get("/api/v1/vouchers?queue=all&limit=50").get("items") or []
    debug_mode = bool(st.session_state.get("mt_debug_mode", False))
    selected_key = st.session_state.get("mt_selected_voucher") or (items[0]["voucher_key"] if items else None)
    latest_bundle = fetch_case_bundle(selected_key) if selected_key else {"timeline": [], "history": []}

    # review_count = len([i for i in items if str(i.get("case_status") or "").upper() in {"NEW", "IN_REVIEW", "REVIEW_REQUIRED", "HITL_REQUIRED"}])
    # analyzed_count = len([i for i in items if str(i.get("case_status") or "").upper() in {"COMPLETED", "RESOLVED", "OK"}])
    # high_risk = len([i for i in items if str(i.get("severity") or "").upper() in {"HIGH", "CRITICAL"}])
    # k1, k2, k3, k4 = st.columns(4)
    # with k1:
    #     render_kpi_card("총 검토 전표", str(len(items)), "전체 큐 기준")
    # with k2:
    #     render_kpi_card("검토 필요", str(review_count), "담당자 또는 추가 검증 필요")
    # with k3:
    #     render_kpi_card("고위험 탐지", str(high_risk), "HIGH/CRITICAL")
    # with k4:
    #     render_kpi_card("분석 완료", str(analyzed_count), "완료/해결 상태")

    left, right = st.columns([0.9, 1.55], gap="large")
    with left:
        with stylable_container(key="workspace_case_queue_card", css_styles="""{padding: 18px 20px; border-radius: 20px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.96); box-shadow: 0 12px 30px rgba(15,23,42,0.05); min-height: 540px;}"""):
            render_workspace_case_queue(items, selected_key)
    with right:
        with stylable_container(key="workspace_chat_card", css_styles="""{padding: 18px 20px; border-radius: 20px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.96); box-shadow: 0 12px 30px rgba(15,23,42,0.05); margin-bottom: 12px;}"""):
            if not selected_key:
                render_empty_state("선택된 케이스가 없습니다.")
            else:
                selected = next((item for item in items if item["voucher_key"] == selected_key), None) or {}
                render_workspace_chat_panel(selected, latest_bundle)
        with stylable_container(key="workspace_result_card", css_styles="""{padding: 18px 20px; border-radius: 20px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.96); box-shadow: 0 12px 30px rgba(15,23,42,0.05); min-height: 520px;}"""):
            if not selected_key:
                render_empty_state("케이스를 선택하면 AI 워크스페이스가 표시됩니다.")
            else:
                tabs = st.tabs(["판단 요약", "근거 맵", "실행 내역", "검토 이력"])
                with tabs[0]:
                    render_workspace_results(latest_bundle, debug_mode)
                    timeline = latest_bundle.get("timeline") or []
                    if timeline:
                        with st.expander("판단 흐름 요약", expanded=False):
                            render_process_story(timeline, debug_mode=debug_mode)
                with tabs[1]:
                    render_workspace_evidence_map(latest_bundle, debug_mode)
                with tabs[2]:
                    render_workspace_execution_review(latest_bundle, debug_mode)
                with tabs[3]:
                    render_workspace_review_history(latest_bundle)
                    run_id = latest_bundle.get("run_id")
                    if run_id:
                        with st.expander("Run 진단 (관찰 지표)", expanded=False):
                            try:
                                diag = get(f"/api/v1/analysis-runs/{run_id}/diagnostics")
                                history = latest_bundle.get("history") or []
                                other_runs = [h for h in history if h.get("run_id") and h.get("run_id") != run_id]
                                compare_run_id = None
                                if other_runs:
                                    opts = ["— 비교 안 함 —"] + [f"{h['run_id'][:8]}… ({h.get('status') or '-'})" for h in other_runs]
                                    sel = st.selectbox("다른 Run과 비교", opts, key=f"run_compare_{selected_key or 'default'}")
                                    if sel and sel != "— 비교 안 함 —":
                                        compare_run_id = other_runs[opts.index(sel) - 1].get("run_id")
                                if compare_run_id:
                                    diag2 = get(f"/api/v1/analysis-runs/{compare_run_id}/diagnostics")
                                    st.caption(f"왼쪽: 현재 Run ({run_id[:8]}…) · 오른쪽: 비교 Run ({compare_run_id[:8]}…)")
                                    col_a, col_b = st.columns(2)
                                    with col_a:
                                        st.metric("Tool 성공률", f"{(diag.get('tool_call_success_rate') or 0) * 100:.1f}%" if diag.get("tool_call_success_rate") is not None else "-", f"{diag.get('tool_call_ok', 0)}/{diag.get('tool_call_total', 0)}")
                                        st.metric("Citation coverage", f"{(diag.get('citation_coverage') or 0) * 100:.1f}%" if diag.get("citation_coverage") is not None else "-", "")
                                        st.metric("HITL 요청", "예" if diag.get("hitl_requested") else "아니오", "재개 성공" if diag.get("resume_success") else "")
                                        st.metric("Fallback 비율", f"{(diag.get('fallback_usage_rate') or 0) * 100:.1f}%" if diag.get("fallback_usage_rate") is not None else "-", f"이벤트 {diag.get('event_count', 0)}건")
                                    with col_b:
                                        st.metric("Tool 성공률", f"{(diag2.get('tool_call_success_rate') or 0) * 100:.1f}%" if diag2.get("tool_call_success_rate") is not None else "-", f"{diag2.get('tool_call_ok', 0)}/{diag2.get('tool_call_total', 0)}")
                                        st.metric("Citation coverage", f"{(diag2.get('citation_coverage') or 0) * 100:.1f}%" if diag2.get("citation_coverage") is not None else "-", "")
                                        st.metric("HITL 요청", "예" if diag2.get("hitl_requested") else "아니오", "재개 성공" if diag2.get("resume_success") else "")
                                        st.metric("Fallback 비율", f"{(diag2.get('fallback_usage_rate') or 0) * 100:.1f}%" if diag2.get("fallback_usage_rate") is not None else "-", f"이벤트 {diag2.get('event_count', 0)}건")
                                else:
                                    c1, c2, c3, c4 = st.columns(4)
                                    c1.metric("Tool 성공률", f"{(diag.get('tool_call_success_rate') or 0) * 100:.1f}%" if diag.get("tool_call_success_rate") is not None else "-", f"{diag.get('tool_call_ok', 0)}/{diag.get('tool_call_total', 0)}")
                                    c2.metric("Citation coverage", f"{(diag.get('citation_coverage') or 0) * 100:.1f}%" if diag.get("citation_coverage") is not None else "-", "")
                                    c3.metric("HITL 요청", "예" if diag.get("hitl_requested") else "아니오", "재개 성공" if diag.get("resume_success") else "")
                                    c4.metric("Fallback 비율", f"{(diag.get('fallback_usage_rate') or 0) * 100:.1f}%" if diag.get("fallback_usage_rate") is not None else "-", f"이벤트 {diag.get('event_count', 0)}건")
                            except Exception:
                                st.caption("진단 API를 불러올 수 없습니다.")
