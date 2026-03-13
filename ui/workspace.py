from __future__ import annotations

import html
import json
import logging
import math
import queue
import re
import threading
import time
from collections import defaultdict
from typing import Any, Iterator

import requests

logger = logging.getLogger(__name__)
import streamlit as st
import plotly.graph_objects as go
from ui.shared import stylable_container

from ui.api_client import API, get, post, post_multipart
from utils.config import settings
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

# SSE 스트림/실행 내역에서 "node / 단계 시작" 대신 "한글 설명 (node) 단계 시작" 형태로 사용자에게 표시
NODE_DISPLAY_LABEL: dict[str, str] = {
    "start_router": "사전 스크리닝 여부에 따라 Intake 직행 또는 Screener로 분기",
    "screener": "전표 기반 케이스 유형 분류",
    "intake": "전표 입력과 위험 지표 정규화",
    "planner": "조사 계획과 tool 순서 수립",
    "execute": "실제 LangChain tool 호출",
    "critic": "과잉 주장과 반례 검토",
    "verify": "자동 판정 가능 여부 검증",
    "hitl_pause": "담당자 검토 대기",
    "hitl_validate": "담당자 응답 검증 후 재개",
    "reporter": "설명 문장과 최종 요약 생성",
    "finalizer": "상태/점수/이력 최종 확정",
    "bootstrap": "분석 시작",
}


STREAM_TERM_MAP: dict[str, str] = {
    "policy_rulebook_probe": "규정 조항 조회 도구",
    "holiday_compliance_probe": "휴일/휴무 적격성 점검 도구",
    "merchant_risk_probe": "가맹점 업종 위험 점검 도구",
    "document_evidence_probe": "증빙 점검 도구",
    "budget_risk_probe": "예산 초과 점검 도구",
    "legacy_aura_deep_audit": "심층 감사 도구",
    "MCC": "가맹점 업종 코드",
    "HITL": "담당자 검토(HITL)",
    "REVIEW_REQUIRED": "검토 필요(REVIEW_REQUIRED)",
    "finalizer": "최종 판정 단계",
    "FINALIZER": "최종 판정 단계",
    "NODE_START": "단계 시작",
    "NODE_END": "단계 종료",
    "TOOL_CALL": "도구 호출",
    "TOOL_RESULT": "도구 결과",
    "THINKING_TOKEN": "추론 중",
    "THINKING_DONE": "추론 완료",
}


def _humanize_stream_text(text: str) -> str:
    out = str(text or "")
    for key in sorted(STREAM_TERM_MAP.keys(), key=len, reverse=True):
        out = out.replace(key, STREAM_TERM_MAP[key])
    return out


def _stream_node_event_label(node: str, event_type: str, *, tool_frag: str | None = None) -> str:
    """노드+이벤트를 사용자용 문구로 변환. 예: '전표 입력과 위험 지표 정규화(intake) 단계 시작'."""
    n = (node or "agent").strip().lower()
    desc = NODE_DISPLAY_LABEL.get(n, n)
    ev_type = (event_type or "").strip().upper()
    if tool_frag:
        return f"{desc} ({n}) · {tool_frag}"
    if ev_type == "NODE_START":
        return f"{desc} ({n}) 단계 시작"
    if ev_type == "NODE_END":
        return f"{desc} ({n}) 단계 종료"
    ev_label = _humanize_stream_text(ev_type)
    if ev_type == "THINKING_DONE":
        return f"{desc} ({n}) · 추론"
    if ev_type == "THINKING_RETRY":
        return f"{desc} ({n}) · 재검토"
    return f"{desc} ({n}) · {ev_label}"


def _stream_node_activity_label(node: str, activity: str) -> str:
    """추론/재검토 등 활동만 있을 때(이벤트 타입 없이) 표시용. 예: '전표 입력과 위험 지표 정규화(intake) · 추론'."""
    n = (node or "agent").strip().lower()
    desc = NODE_DISPLAY_LABEL.get(n, n)
    return f"{desc} ({n}) · {activity}"


def _build_thinking_card_html(node_name: str, text: str, *, is_complete: bool) -> str:
    colors = {
        "planner": ("#0f1a2e", "#3b82f6", "🧠"),
        "critic": ("#1a0f0f", "#ef4444", "🔍"),
        "verify": ("#0f1a14", "#22c55e", "✅"),
        "reporter": ("#1a1500", "#f59e0b", "📊"),
        "execute": ("#0f0f1a", "#8b5cf6", "⚡"),
        "intake": ("#0d1117", "#6b7280", "📥"),
        "screener": ("#0d1117", "#6b7280", "🔎"),
        "finalizer": ("#0f1a0f", "#22c55e", "⚖️"),
    }
    bg, border, icon = colors.get(str(node_name or "").lower(), ("#0d1117", "#4b5563", "🤖"))
    safe_text = html.escape(text or "")
    cursor = "" if is_complete else '<span style="display:inline-block;width:2px;height:1em;background:#9ca3af;margin-left:2px;vertical-align:text-bottom;animation:blink 0.7s step-end infinite;"></span>'
    status = "완료" if is_complete else "추론 중"
    opacity = "0.85" if is_complete else "1"
    return f"""
    <style>@keyframes blink {{ 0%,100%{{opacity:1;}} 50%{{opacity:0;}} }}</style>
    <div style="background:{bg}; border:1px solid {border}; border-left:3px solid {border}; border-radius:10px; padding:12px 14px; margin:8px 0; opacity:{opacity};">
      <div style="font-size:10px; font-weight:700; letter-spacing:1px; color:{border}; margin-bottom:8px;">{icon} {node_name} · {status}</div>
      <div style="font-size:13px; line-height:1.7; color:#e2e8f0;">{safe_text}{cursor}</div>
    </div>
    """


# execute 단계 TOOL_CALL/TOOL_RESULT용 짧은 라벨 (반복적인 "도구 호출: …" 대신 한 줄 요약용)
_TOOL_SHORT_LABEL: dict[str, str] = {
    "policy_rulebook_probe": "규정 조항 조회",
    "holiday_compliance_probe": "휴일·근태 적격성 확인",
    "merchant_risk_probe": "가맹점 업종 위험 점검",
    "document_evidence_probe": "전표·증빙 수집",
    "budget_risk_probe": "예산 초과 점검",
    "legacy_aura_deep_audit": "심층 감사",
}


def _is_generic_execute_tool_event(payload: dict[str, Any]) -> bool:
    """execute 노드의 TOOL_CALL/TOOL_RESULT에서 판단/실행/발견이 템플릿 문구만 있으면 True (컴팩트 렌더 시 블록 생략용)."""
    node = str(payload.get("node") or "").lower()
    ev_type = str(payload.get("event_type") or "").upper()
    if node != "execute" or ev_type not in {"TOOL_CALL", "TOOL_RESULT"}:
        return False
    thought = (payload.get("thought") or "").strip()
    action = (payload.get("action") or "").strip()
    observation = (payload.get("observation") or "").strip()
    tool = payload.get("tool") or ""
    if ev_type == "TOOL_CALL":
        if observation not in ("도구 실행 중.", ""):
            return False
        if action != f"{tool} 실행":
            return False
        return True
    if ev_type == "TOOL_RESULT":
        if thought != "수집한 사실을 다음 판단 단계에 반영한다.":
            return False
        if action != f"{tool} 결과 반영":
            return False
        return True
    return False


def _tool_caption_fragment(ev_type: str, tool: str | None, tool_description: str | None, html_tooltip: bool = False) -> str:
    """TOOL_* 이벤트용 캡션 조각: 'TOOL_CALL: toolname' 형식. html_tooltip=True이면 도구명에 title 툴팁."""
    if not tool or str(ev_type).upper() not in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED"}:
        return ""
    tool_label = _humanize_stream_text(tool)
    if html_tooltip and tool_description:
        desc = (tool_description or "").replace('"', "&quot;").replace("<", "&lt;")
        return f"{_humanize_stream_text(ev_type)}: <span title=\"{desc}\">{tool_label}</span>"
    return f"{_humanize_stream_text(ev_type)}: {tool_label}"


def _stream_card_chunks(obj: dict[str, Any]) -> Iterator[str]:
    """Yields markdown card content in chunks for typing effect. Card = header + message (word-by-word) + 생각/행동/관찰. execute 도구 이벤트는 컴팩트 한 줄."""
    ts = fmt_dt_korea(obj.get("timestamp")) or "-"
    node = obj.get("node") or "agent"
    ev_type = str(obj.get("event_type") or "event").upper()
    tool = obj.get("tool")
    meta = obj.get("metadata") or {}
    tool_desc = meta.get("tool_description")
    short_label = _TOOL_SHORT_LABEL.get(tool or "", "") or _humanize_stream_text(tool or "")

    # execute 노드 TOOL_CALL/TOOL_RESULT: 반복적인 "도구 호출/결과 + 판단/실행/발견" 대신 한 줄 카드
    if node == "execute" and ev_type in {"TOOL_CALL", "TOOL_RESULT"}:
        icon = EVENT_ICON_MAP.get(ev_type, "🤖")
        if ev_type == "TOOL_CALL":
            line = f"{short_label} 실행 중."
        else:
            raw = (obj.get("observation") or obj.get("message") or "").strip()
            summary = _humanize_stream_text(raw)
            line = (summary[:120] + ("…" if len(summary) > 120 else "")) if summary else "수집 완료."
        yield f"{icon} **{ts}** · {short_label}  \n"
        for word in line.split():
            yield word + " "
        yield "  \n\n"
        return

    tool_frag = _tool_caption_fragment(ev_type, tool, tool_desc, html_tooltip=False)
    part2 = _stream_node_event_label(node, ev_type, tool_frag=tool_frag if tool_frag else None)
    icon = EVENT_ICON_MAP.get(ev_type, "🤖")
    header = f"{icon} **{ts}** · {part2}  \n"
    yield header

    message = _humanize_stream_text((obj.get("message") or "").strip())
    if message:
        for word in message.split():
            yield word + " "
        yield "  \n\n"

    thought = _humanize_stream_text((obj.get("thought") or "").strip())
    action = _humanize_stream_text((obj.get("action") or "").strip())
    observation = _humanize_stream_text((obj.get("observation") or "").strip())
    if thought or action or observation:
        if thought:
            yield f"🧠 **판단** {thought}  \n\n"
        if action:
            yield f"⚡ **실행** {action}  \n\n"
        if observation:
            yield f"🔍 **발견** {observation}  \n\n"
    yield "\n"


def _score_breakdown_stream_block(score: dict[str, Any]) -> str:
    policy = int(score.get("policy_score") or score.get("policy_score_raw") or 0)
    evidence = int(score.get("evidence_score") or score.get("evidence_score_raw") or 0)
    final = int(score.get("final_score") or score.get("score") or 0)
    severity = str(score.get("severity") or "-")

    def _bar(value: int, width: int = 20) -> str:
        clamped = max(0, min(100, int(value)))
        filled = round((clamped / 100) * width)
        return "█" * filled + "░" * (width - filled)

    return (
        "\n\n📈 **CONFIDENCE SCORE**  \n"
        f"- 정책 점수: **{policy:>3}** {_bar(policy)}  \n"
        f"- 근거 점수: **{evidence:>3}** {_bar(evidence)}  \n"
        f"- 최종 점수: **{final:>3}** {_bar(final)} ({severity})  \n\n"
    )


def sse_text_stream(stream_url: str, *, run_id: str | None = None) -> Iterator[str]:
    stream_buffer: list[str] = []
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
                done_line = "\n\n**분석 스트림 종료**\n"
                stream_buffer.append(done_line)
                yield done_line
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
                                sep = "\n\n---\n\n"
                                stream_buffer.append(sep)
                                yield sep
                            ts = fmt_dt_korea(obj.get("timestamp")) or "-"
                            header = f"💭 **{ts}** · {_stream_node_activity_label(node, '추론')}  \n"
                            stream_buffer.append(header)
                            yield header
                            thinking_node = node
                        if token:
                            stream_buffer.append(token)
                            yield token
                        first_event = False
                    elif ev_type == "THINKING_DONE":
                        done_sep = "  \n\n---  \n\n"
                        stream_buffer.append(done_sep)
                        yield done_sep
                        thinking_node = None
                        first_event = False
                    elif ev_type == "THINKING_RETRY":
                        # 재검토 이벤트: 현재 thinking 카드를 닫고, 재검토 안내를 한 줄로 표시한 뒤 다음 토큰을 새 카드로 받는다.
                        if thinking_node is not None:
                            done_sep = "  \n\n---  \n\n"
                            stream_buffer.append(done_sep)
                            yield done_sep
                            thinking_node = None
                        if not first_event:
                            sep = "\n\n---\n\n"
                            stream_buffer.append(sep)
                            yield sep
                        ts = fmt_dt_korea(obj.get("timestamp")) or "-"
                        node = obj.get("node") or "agent"
                        retry_header = f"🔄 **{ts}** · {_stream_node_activity_label(node, '재검토')}  \n"
                        retry_body = "_판단 결과와 추론 문구 정합성을 다시 맞추는 중..._  \n\n"
                        stream_buffer.append(retry_header)
                        stream_buffer.append(retry_body)
                        yield retry_header
                        yield retry_body
                        first_event = False
                    else:
                        if ev_type == "PLAN_READY":
                            first_event = False
                            continue
                        if thinking_node is not None:
                            done_sep = "  \n\n---  \n\n"
                            stream_buffer.append(done_sep)
                            yield done_sep
                            thinking_node = None
                        if not first_event:
                            sep = "\n\n---\n\n"
                            stream_buffer.append(sep)
                            yield sep
                        for chunk in _stream_card_chunks(obj):
                            stream_buffer.append(chunk)
                            yield chunk
                        first_event = False
                        if ev_type == "HITL_PAUSE":
                            if run_id:
                                st.session_state[_hitl_state_key("dismissed", run_id)] = False
                                st.session_state[_hitl_state_key("shown", run_id)] = True
                            reason = (obj.get("metadata") or {}).get("reason") or ""
                            final_line = "\n\n**[최종]** 담당자 검토 입력을 기다립니다." + (f" 사유: {reason}" if reason else "") + "\n"
                            stream_buffer.append(final_line)
                            yield final_line
                            break
                elif event == "confidence":
                    score = obj.get("score_breakdown") or obj
                    block = _score_breakdown_stream_block(score)
                    stream_buffer.append(block)
                    yield block
                elif event == "completed":
                    final_text = obj.get("reasonText") or obj.get("summary") or "완료"
                    result = obj.get("result") or {}
                    status = str(result.get("status") or obj.get("status") or "").upper()
                    if status == "HITL_REQUIRED" and run_id:
                        st.session_state[_hitl_state_key("dismissed", run_id)] = False
                        st.session_state[_hitl_state_key("shown", run_id)] = True
                        line_out = f"\n\n**[최종]** {final_text}\n"
                        stream_buffer.append(line_out)
                        yield line_out
                        break
                    line_out = f"\n\n**[최종]** {final_text}\n"
                    stream_buffer.append(line_out)
                    yield line_out
                elif event == "failed":
                    fail_line = f"\n\n**[실패]** {obj.get('error', 'unknown error')}\n"
                    stream_buffer.append(fail_line)
                    yield fail_line
                else:
                    detail = obj.get("detail") or obj.get("message") or obj.get("content") or payload
                    other_line = f"[{event}] {detail}\n"
                    stream_buffer.append(other_line)
                    yield other_line
            except Exception:
                raw_line = f"[{event}] {payload}\n"
                stream_buffer.append(raw_line)
                yield raw_line
    if run_id:
        st.session_state[f"mt_last_stream_content_{run_id}"] = "".join(stream_buffer)


def _prefix_with_typed_append(prefix: str, addition: str) -> Iterator[str]:
    """placeholder.write_stream용: 기존 본문은 즉시, 새 본문은 타이핑."""
    if prefix:
        yield prefix
    for ch in addition:
        yield ch
        time.sleep(0.004)


def _render_stream_waiting_indicator(placeholder: Any, status_text: str, node_name: str | None = None) -> None:
    """스트림 이벤트 간 공백 구간에 표시되는 진행중 인디케이터."""
    safe_status = html.escape((status_text or "").strip())
    if not safe_status:
        safe_status = "LLM 응답을 기다리는 중입니다."
    safe_node = html.escape((node_name or "agent").strip() or "agent")
    html_block = f"""
    <style>
      @keyframes mtWaitPulse {{ 0%,100%{{opacity:.28; transform:translateY(0)}} 50%{{opacity:1; transform:translateY(-1px)}} }}
      .mt-stream-wait {{
        display:flex; align-items:center; gap:10px;
        padding:8px 10px; margin-top:6px;
        border:1px dashed #cbd5e1; border-radius:12px; background:#f8fafc;
        color:#334155; font-size:12px; line-height:1.4;
      }}
      .mt-stream-wait .dots {{ display:inline-flex; gap:4px; }}
      .mt-stream-wait .dot {{
        width:7px; height:7px; border-radius:999px; background:#2563eb;
        animation: mtWaitPulse 1s infinite ease-in-out;
      }}
      .mt-stream-wait .dot:nth-child(2) {{ animation-delay:.15s; }}
      .mt-stream-wait .dot:nth-child(3) {{ animation-delay:.3s; }}
      .mt-stream-wait .label {{ font-weight:700; color:#1e40af; margin-right:2px; }}
      .mt-stream-wait .node {{
        display:inline-flex; align-items:center; justify-content:center;
        font-size:11px; font-weight:800; color:#ffffff;
        background:#0f766e; border:1px solid #115e59; border-radius:999px;
        padding:2px 8px; margin-right:2px;
        text-shadow: 0 1px 0 rgba(0,0,0,0.15);
      }}
      .mt-stream-wait .status {{ color:#475569; }}
    </style>
    <div class="mt-stream-wait">
      <span class="dots"><span class="dot"></span><span class="dot"></span><span class="dot"></span></span>
      <span class="node">{safe_node}</span>
      <span class="label">생각중</span>
      <span class="status">{safe_status}</span>
    </div>
    """
    try:
        placeholder.markdown(html_block, unsafe_allow_html=True)
    except Exception:
        placeholder.write(f"{safe_node} 생각중 · {safe_status}")


def _stream_status_keys(run_id: str | None) -> tuple[str | None, str | None]:
    if not run_id:
        return None, None
    return f"mt_stream_status_{run_id}", f"mt_stream_node_{run_id}"


# idle(생각중) 표시 시 마지막 완료 노드가 아닌 다음 단계 문구를 보여주기 위한 메타
_STREAM_NEXT_FOR_IDLE: dict[str, tuple[str, str]] = {
    "bootstrap": ("입력 해석", "분석을 시작하는 중입니다."),
    "screener": ("입력 해석", "전표 입력값과 위험 지표를 정규화합니다."),
    "intake": ("조사 계획 수립", "검증할 사실과 사용할 도구 순서를 계획합니다."),
    "planner": ("근거 수집 실행", "휴일/예산/업종/규정 근거를 조회합니다."),
    "execute": ("비판적 검토", "과잉 주장과 반례 가능성을 점검합니다."),
    "critic": ("검증 및 HITL 판단", "자동 판정 가능 여부를 결정합니다."),
    "verify": ("HITL 대기 / 보고", "담당자 검토 또는 보고 문장 생성으로 이어갑니다."),
    "hitl_pause": ("보고 문장 생성", "근거 중심 설명 문장을 만듭니다."),
    "reporter": ("결과 확정", "상태·점수·이력을 최종 확정합니다."),
    "finalizer": ("완료", "분석을 마쳤습니다."),
}


def _get_stream_waiting_status(run_id: str | None) -> tuple[str, str]:
    """idle 시 표시할 (status 문구, 노드 라벨). 방금 끝난 단계가 아닌 다음 단계 문구를 써서 오해를 줄인다."""
    status_key, node_key = _stream_status_keys(run_id)
    status_text = st.session_state.get(status_key or "", "") if status_key else ""
    node_name = str(st.session_state.get(node_key or "", "") or "agent").strip().lower()
    next_info = _STREAM_NEXT_FOR_IDLE.get(node_name)
    if next_info:
        next_label, next_phrase = next_info
        return next_phrase, next_label
    if str(status_text or "").strip():
        return str(status_text or ""), str(node_name or "agent")
    return "이벤트 수신을 기다리는 중입니다.", "준비"


def sse_node_block_generator_with_idle(
    stream_url: str,
    *,
    run_id: str | None = None,
    idle_after_sec: float = 1.0,
) -> Iterator[dict[str, Any]]:
    """
    SSE 블록 스트림을 백그라운드 스레드로 소비하고,
    메인 루프는 큐를 폴링해 1초 이상 이벤트 공백 시 idle 이벤트를 발생시킨다.
    """
    out_q: queue.Queue = queue.Queue()
    worker_done = threading.Event()

    def _worker() -> None:
        try:
            for prefix, addition in sse_node_block_generator(stream_url, run_id=run_id):
                out_q.put({"type": "block", "prefix": prefix, "addition": addition})
        except Exception as e:
            out_q.put({"type": "error", "message": str(e)})
        finally:
            worker_done.set()

    worker = threading.Thread(target=_worker, daemon=True, name="workspace-sse-worker")
    # Streamlit 스레드 컨텍스트를 전달하지 않으면 worker에서 st.session_state 접근 시
    # "missing ScriptRunContext" 경고가 반복된다.
    try:
        from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

        ctx = get_script_run_ctx()
        if ctx is not None:
            try:
                add_script_run_ctx(worker, ctx)
            except TypeError:
                # 일부 버전은 add_script_run_ctx(thread) 시그니처만 제공
                add_script_run_ctx(worker)
    except Exception:
        pass
    worker.start()

    had_block = False
    last_block_at = time.monotonic()
    last_idle_emit = 0.0

    while True:
        if worker_done.is_set() and out_q.empty():
            break
        try:
            item = out_q.get(timeout=0.1)
            had_block = True
            last_block_at = time.monotonic()
            yield item
            continue
        except queue.Empty:
            pass

        now = time.monotonic()
        if (now - last_block_at) >= idle_after_sec and (now - last_idle_emit) >= 0.6:
            status_text, node_name = _get_stream_waiting_status(run_id)
            yield {"type": "idle", "status_text": status_text, "node_name": node_name}
            last_idle_emit = now


def sse_node_block_generator(stream_url: str, *, run_id: str | None = None) -> Iterator[tuple[str, str]]:
    """
    노드 단위 Replace 스트림.
    - NODE_START 수신 시 prefix를 비워 기존 화면을 교체
    - 같은 노드 내 이벤트는 prefix(현재 누적본)+addition(신규) 형태로 전달
    """
    history_buffer: list[str] = []
    current_block = ""
    current_node: str | None = None
    thinking_open = False
    thinking_buffer = ""
    event_name: str | None = None
    hitl_message_shown = False
    status_key, node_key = _stream_status_keys(run_id)
    if status_key:
        st.session_state[status_key] = ""
    if node_key:
        st.session_state[node_key] = "agent"

    with requests.get(stream_url, stream=True, timeout=300) as response:
        response.raise_for_status()
        for raw in response.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload = line.split(":", 1)[1].strip()
            if payload == "[DONE]":
                if run_id and st.session_state.get(f"mt_run_terminal_status_{run_id}") == "HITL_REQUIRED":
                    done_line = "\n\n**담당자 검토 의견 입력 후 \"분석 이어가기 실행\" 버튼을 클릭하시면 분석을 이어서 진행하겠습니다.**\n"
                else:
                    done_line = "\n\n**분석 스트림 종료**\n"
                prefix = current_block
                current_block += done_line
                history_buffer.append(done_line)
                yield prefix, done_line
                break
            try:
                obj = json.loads(payload)
            except Exception:
                raw_line = f"[{event_name}] {payload}\n"
                prefix = current_block
                current_block += raw_line
                history_buffer.append(raw_line)
                yield prefix, raw_line
                continue

            if event_name == "started":
                continue

            addition = ""
            if event_name == "AGENT_EVENT":
                ev_type = str(obj.get("event_type") or "").upper()
                node = str(obj.get("node") or "agent")

                if ev_type == "NODE_START":
                    thinking_open = False
                    thinking_buffer = ""
                    current_node = node
                    if node_key:
                        st.session_state[node_key] = node
                    addition = "".join(_stream_card_chunks(obj))
                    if status_key:
                        message = _humanize_stream_text(obj.get("message") or "")
                        if message.strip():
                            st.session_state[status_key] = message[:220]
                    current_block = addition
                    history_buffer.append(addition)
                    yield "", addition
                    continue

                if current_node is None:
                    current_node = node
                if node_key:
                    st.session_state[node_key] = node

                if ev_type == "THINKING_TOKEN":
                    token = _humanize_stream_text((obj.get("metadata") or {}).get("token") or "")
                    if not thinking_open:
                        ts = fmt_dt_korea(obj.get("timestamp")) or "-"
                        if current_block:
                            addition += "\n\n"
                        addition += f"💭 **{ts}** · {_stream_node_activity_label(node, '추론')}  \n"
                        thinking_open = True
                    if token:
                        addition += token
                        thinking_buffer = (thinking_buffer + token)[-280:]
                        if status_key and thinking_buffer.strip():
                            st.session_state[status_key] = thinking_buffer.strip()
                elif ev_type == "THINKING_DONE":
                    if thinking_open:
                        addition += "  \n\n"
                    else:
                        # THINKING_TOKEN 없이 DONE만 온 경우(예: LLM 실패 후 fallback)에도 추론 문구 표시
                        done_reasoning = _humanize_stream_text(
                            (obj.get("metadata") or {}).get("reasoning") or obj.get("message") or ""
                        ).strip()
                        if done_reasoning:
                            ts = fmt_dt_korea(obj.get("timestamp")) or "-"
                            if current_block or addition:
                                addition += "\n\n"
                            addition += f"💭 **{ts}** · {_stream_node_activity_label(node, '추론')}  \n{done_reasoning}  \n\n"
                    thinking_open = False
                    if status_key and thinking_buffer.strip():
                        st.session_state[status_key] = thinking_buffer.strip()
                    thinking_buffer = ""
                elif ev_type == "THINKING_RETRY":
                    if thinking_open:
                        addition += "  \n\n"
                        thinking_open = False
                    thinking_buffer = ""
                    if current_block or addition:
                        addition += "\n\n"
                    ts = fmt_dt_korea(obj.get("timestamp")) or "-"
                    addition += f"🔄 **{ts}** · {_stream_node_activity_label(node, '재검토')}  \n"
                    addition += "_판단 결과와 추론 문구 정합성을 다시 맞추는 중..._  \n\n"
                    if status_key:
                        retry_msg = _humanize_stream_text(obj.get("message") or "")
                        if retry_msg.strip():
                            st.session_state[status_key] = retry_msg[:220]
                else:
                    if ev_type == "PLAN_READY":
                        continue
                    if thinking_open:
                        addition += "  \n\n"
                        thinking_open = False
                    if status_key:
                        meta = obj.get("metadata") or {}
                        message = _humanize_stream_text(
                            (meta.get("reasoning") or obj.get("message") or obj.get("observation") or "")
                        )
                        if message.strip():
                            st.session_state[status_key] = message[:220]
                    if current_block or addition:
                        addition += "\n\n"
                    addition += "".join(_stream_card_chunks(obj))
                    if ev_type == "HITL_PAUSE":
                        if run_id:
                            st.session_state[_hitl_state_key("dismissed", run_id)] = False
                            st.session_state[_hitl_state_key("shown", run_id)] = True
                        hitl_message_shown = True
                        reason = (obj.get("metadata") or {}).get("reason") or ""
                        addition += "\n\n**[최종]** 담당자 검토 입력을 기다립니다." + (f" 사유: {reason}" if reason else "") + "\n"
            elif event_name == "confidence":
                score = obj.get("score_breakdown") or obj
                addition = _score_breakdown_stream_block(score)
            elif event_name == "completed":
                final_text = _humanize_stream_text(obj.get("reasonText") or obj.get("summary") or "완료")
                if status_key:
                    st.session_state[status_key] = final_text[:220]
                result = obj.get("result") or {}
                status = str(result.get("status") or obj.get("status") or "").upper()
                if run_id and status:
                    # 완료 이벤트에서 받은 최종 상태를 즉시 반영해, API 재조회 지연 시에도 배너/버튼 노출이 늦지 않도록 한다.
                    st.session_state[f"mt_run_terminal_status_{run_id}"] = status
                if status == "HITL_REQUIRED" and run_id:
                    st.session_state[_hitl_state_key("dismissed", run_id)] = False
                    st.session_state[_hitl_state_key("shown", run_id)] = True
                # HITL_PAUSE에서 이미 "[최종] 담당자 검토 입력을 기다립니다." 표시한 경우 한 번만 노출
                if status == "HITL_REQUIRED" and hitl_message_shown:
                    addition = ""
                else:
                    addition = f"\n\n**[최종]** {final_text}\n"
            elif event_name == "failed":
                if status_key:
                    st.session_state[status_key] = _humanize_stream_text(obj.get("error", "unknown error"))[:220]
                addition = f"\n\n**[실패]** {_humanize_stream_text(obj.get('error', 'unknown error'))}\n"
            else:
                detail = obj.get("detail") or obj.get("message") or obj.get("content") or payload
                if status_key:
                    st.session_state[status_key] = _humanize_stream_text(str(detail))[:220]
                addition = f"[{_humanize_stream_text(str(event_name or 'event'))}] {_humanize_stream_text(str(detail))}\n"

            if not addition:
                continue
            addition = _humanize_stream_text(addition)
            prefix = current_block
            current_block += addition
            history_buffer.append(addition)
            yield prefix, addition

    if run_id:
        st.session_state[f"mt_last_stream_content_{run_id}"] = "".join(history_buffer)


def fetch_case_bundle(voucher_key: str) -> dict[str, Any]:
    latest = get(f"/api/v1/cases/{voucher_key}/analysis/latest") or {}
    history = get(f"/api/v1/cases/{voucher_key}/analysis/history") or {}
    latest["voucher_key"] = voucher_key
    if latest.get("run_id"):
        events = get(f"/api/v1/analysis-runs/{latest['run_id']}/events") or {}
        latest["timeline"] = events.get("events") or []
    else:
        latest["timeline"] = []
    latest["history"] = history.get("items") or []
    return latest


def _tool_name(r: dict[str, Any]) -> str:
    return str(r.get("tool") or r.get("skill") or "unknown")


def summarize_tool_results(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for tool in tool_results:
        tname = _tool_name(tool)
        facts = tool.get("facts") or {}
        entry = {"tool": tname, "detail": tool.get("summary") or "-"}
        if tname == "policy_rulebook_probe":
            refs = facts.get("policy_refs") or []
            entry.update(
                metric_label="규정 근거",
                metric_value=f"{len(refs)}건",
                detail=", ".join(filter(None, [ref.get("article") for ref in refs[:3]])) or "-",
            )
        elif tname == "document_evidence_probe":
            entry.update(metric_label="전표 라인", metric_value=f"{facts.get('lineItemCount', 0)}건", detail="수집 완료")
        elif tname == "legacy_aura_deep_audit":
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
    # 4개 카드를 한 라인에 나란히 (width 균등 분할)
    num_cols = min(4, len(cards))
    cols = st.columns(num_cols)
    for idx, card in enumerate(cards):
        with cols[idx % num_cols]:
            with stylable_container(
                key=f"tool_summary_{idx}",
                css_styles="""{padding: 10px 12px; border-radius: 14px; border: 1px solid #e5e7eb; background: #fff; box-shadow: 0 6px 18px rgba(15,23,42,0.04); min-height: 120px;}""",
            ):
                st.caption(card["tool"])
                st.markdown(f"**{card['metric_label']}**")
                st.subheader(card["metric_value"])
                st.caption(card["detail"])


def render_pipeline_progress(completed_nodes: list[str], current_node: str) -> None:
    """workspace.md B-3: 파이프라인 진행 상태 바. st.html로 렌더해 expander 내부에서도 태그가 이스케이프되지 않고 표시되도록 함."""
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
    pipeline_html = f"""
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
    """
    try:
        st.html(pipeline_html)
    except Exception:
        st.markdown(pipeline_html, unsafe_allow_html=True)


def render_score_breakdown_card(policy_score: int, evidence_score: int, final_score: int) -> None:
    """workspace.md B-7: Confidence Score 인라인 게이지 바. st.html로 렌더해 expander 내부에서도 태그가 보이지 않도록 함."""
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
    score_html = f"""
    <div style="background:#0d1117; border:1px solid #1e2d3d; border-radius:10px; padding:16px; margin:10px 0;">
      <div style="font-size:12px; font-weight:700; letter-spacing:1px; color:#6b7280; margin-bottom:12px;">CONFIDENCE SCORE</div>
      {_bar('정책 점수', policy_score, '#3b82f6')}
      {_bar('근거 점수', evidence_score, '#8b5cf6')}
      {_bar('최종 점수', final_score, final_color)}
    </div>
    """
    try:
        st.html(score_html)
    except Exception:
        st.markdown(score_html, unsafe_allow_html=True)


_SCORE_CATEGORY_LABELS: dict[str, str] = {
    "policy": "정책",
    "evidence": "근거",
    "amount": "금액",
    "multiplier": "승수",
}


def _extract_score_breakdown_from_timeline(timeline: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(timeline):
        if event.get("event_type") != "AGENT_EVENT":
            continue
        payload = event.get("payload") or {}
        if str(payload.get("event_type") or "").upper() != "SCORE_BREAKDOWN":
            continue
        meta = payload.get("metadata") or {}
        score = meta.get("score_breakdown") if isinstance(meta.get("score_breakdown"), dict) else meta
        if isinstance(score, dict):
            return score
    return {}


def _fmt_score_signal_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _fmt_score_points(points: Any) -> str:
    try:
        p = float(points or 0.0)
    except Exception:
        return str(points or "0")
    if p > 0:
        return f"+{p:g}"
    return f"{p:g}"


@st.dialog("점수 계산 로직", width="medium")
def _render_score_logic_dialog(kind: str, score_breakdown: dict[str, Any]) -> None:
    if kind == "policy":
        st.markdown("### policy 점수 계산 로직")
        st.markdown(
            "`policy_score = min(100, (base_policy + tool_policy_delta) × compound_multiplier × amount_multiplier)`"
        )
        st.markdown("**기본 정책 신호 (base_policy)**")
        st.table(
            [
                {"항목": "휴일/주말 사용", "조건": "휴일 여부 = 예", "점수": "+35"},
                {"항목": "근태 충돌", "조건": "근태 상태 = 휴가/휴무/결근", "점수": "+20"},
                {"항목": "심야 시간", "조건": "심야 여부 = 예", "점수": "+10"},
                {"항목": "예산 초과", "조건": "예산 초과 여부 = 예", "점수": "+15"},
            ]
        )
        st.markdown("**도구 기반 정책 보정 신호 (tool_policy_delta)**")
        st.table(
            [
                {"항목": "휴일 위험도 높음", "점수": "+10"},
                {"항목": "휴일 위험도 중간", "점수": "+5"},
                {"항목": "가맹점 위험도 높음", "점수": "+20"},
                {"항목": "가맹점 위험도 중간", "점수": "+10"},
                {"항목": "가맹점 위험도 낮음", "점수": "+3"},
            ]
        )
        st.markdown("**승수(가중치) 적용**")
        st.table(
            [
                {"항목": "복합 위험 승수", "규칙": "고위험 신호 2개=1.15, 3개=1.30, 4개+=설정 최대값"},
                {"항목": "금액 승수", "규칙": "10만원 이하: 1.00~1.07 (금액 비례)"},
                {"항목": "금액 승수", "규칙": "10만원~50만원: 1.07~1.15 (금액 비례)"},
                {"항목": "금액 승수", "규칙": "50만원~200만원: 1.15~1.30 (금액 비례)"},
                {"항목": "금액 승수", "규칙": "200만원 초과: 1.30 (상한, 설정 최대값 적용)"},
            ]
        )
        st.caption(
            f"이번 run 결과: 정책점수={score_breakdown.get('policy_score', '-')}, "
            f"복합 위험 승수={score_breakdown.get('compound_multiplier', '-')}, "
            f"금액 가중치={score_breakdown.get('amount_weight', '-')}"
        )
    else:
        st.markdown("### evidence 점수 계산 로직")
        st.markdown(
            "`evidence_score = base_evidence(20) + tool_evidence_delta + 성공률/계획 보정 + HITL 보정`"
        )
        st.markdown("**tool_evidence_delta 신호**")
        st.table(
            [
                {"항목": "규정 조항 수", "규칙": "5건=+30, 3건=+22, 2건=+15, 1건=+10"},
                {"항목": "전표 라인 수", "규칙": "3건=+20, 2건=+15, 1건=+10"},
                {"항목": "심층 감사 결과", "규칙": "legacy_aura_deep_audit 존재 시 +15"},
                {"항목": "HITL 응답 확보", "규칙": "hasHitlResponse=true 시 +10"},
            ]
        )
        st.markdown("**실행 보정**")
        st.table(
            [
                {"항목": "성공률 패널티", "규칙": "도구 성공률 < 50%면 감점"},
                {"항목": "전체 성공 보너스", "규칙": "도구 3개 이상 + 전부 성공 시 +5"},
                {"항목": "HITL 승인 보정", "규칙": "_score_with_hitl_adjustment 경로에서 승인 시 +10"},
            ]
        )
        st.caption(
            f"이번 run 결과: evidence_score={score_breakdown.get('evidence_score', '-')}, "
            f"final_score={score_breakdown.get('final_score', '-')}"
        )


def render_score_breakdown_detail(score_breakdown: dict[str, Any]) -> None:
    dialog_kind = st.session_state.pop("mt_score_logic_dialog_kind", None)
    if dialog_kind in {"policy", "evidence"}:
        _render_score_logic_dialog(dialog_kind, score_breakdown)

    with st.expander("점수 산정 근거 보기", expanded=False):
        # 판단 흐름 요약과 동일한 expander 내부 가독성(밝은 카드/진한 텍스트) 적용
        with stylable_container(
            key="process_story_score_breakdown_detail",
            css_styles="""{
                padding: 0.2rem 0.15rem;
                border-radius: 12px;
            }""",
        ):
            st.caption("최종 점수 기준으로 계산식과 가산/감점 내역을 확인합니다.")

            trace = str(score_breakdown.get("calculation_trace") or "").strip()
            if trace:
                st.markdown(f"`{trace}`")
            if bool(score_breakdown.get("conflict_warning")):
                st.warning("판단 불일치 주의: 규칙 점수와 LLM 점수 편차가 큽니다.")

            signals = score_breakdown.get("signals") or []
            if isinstance(signals, list) and signals:
                policy_rows: list[dict[str, str]] = []
                evidence_rows: list[dict[str, str]] = []
                policy_signal_points = 0.0
                evidence_signal_points = 0.0
                for sig in signals:
                    if not isinstance(sig, dict):
                        continue
                    category = str(sig.get("category") or "").strip().lower()
                    label = str(sig.get("label") or sig.get("signal") or "-").strip() or "-"
                    try:
                        points_val = float(sig.get("points") or 0.0)
                    except Exception:
                        points_val = 0.0
                    row = {
                        "항목": label,
                        "값": _fmt_score_signal_value(sig.get("raw_value")),
                        "점수 영향": _fmt_score_points(sig.get("points")),
                    }
                    # 정책 점수는 정책 신호 + 승수 신호(복합위험/금액)를 함께 보여주어 산정 흐름을 이해할 수 있게 한다.
                    if category in {"policy", "multiplier", "amount"}:
                        policy_rows.append(row)
                        if category == "policy":
                            policy_signal_points += points_val
                    elif category == "evidence":
                        evidence_rows.append(row)
                        evidence_signal_points += points_val

                policy_score = score_breakdown.get("policy_score")
                evidence_score = score_breakdown.get("evidence_score")

                policy_btn_text = "policy 로직 보기"
                try:
                    compound_multiplier = float(score_breakdown.get("compound_multiplier") or 1.0)
                except Exception:
                    compound_multiplier = 1.0
                try:
                    amount_weight = float(score_breakdown.get("amount_weight") or 1.0)
                except Exception:
                    amount_weight = 1.0
                policy_calc = min(100.0, policy_signal_points * compound_multiplier * amount_weight)
                policy_final = float(policy_score if policy_score is not None else policy_calc)
                policy_calc_formula = (
                    f"정책 신호합 {policy_signal_points:g} × 복합승수 {compound_multiplier:.2f} × 금액승수 {amount_weight:.2f}"
                    f" = {policy_calc:.1f} → {int(round(policy_final))}점"
                )
                policy_calc_text = f"정책 계산 ({policy_calc_formula})"
                with stylable_container(
                    key="score_logic_row_policy",
                    css_styles=[
                        """{}""",
                        """
                        > [data-testid="stHorizontalBlock"] {
                            align-items: center !important;
                        }
                        """,
                    ],
                ):
                    p_text_col, p_btn_col = st.columns([0.80, 0.20])
                    with p_text_col:
                        st.caption(policy_calc_text)
                    with p_btn_col:
                        with stylable_container(
                            key="score_logic_link_policy",
                            css_styles=[
                                """{margin:0 !important; padding:0 !important;}""",
                                """
                                > [data-testid="stButton"] {
                                    margin: 0 !important;
                                    display: flex !important;
                                    justify-content: flex-end !important;
                                }
                                """,
                                """
                                > [data-testid="stButton"] > button {
                                    background: transparent !important;
                                    border: none !important;
                                    box-shadow: none !important;
                                    color: #2563eb !important;
                                    padding: 0 !important;
                                    margin: 0 !important;
                                    text-decoration: underline !important;
                                    font-weight: 700 !important;
                                    text-align: right !important;
                                    justify-content: flex-end !important;
                                    width: 100% !important;
                                }
                                """,
                                """
                                > [data-testid="stButton"] > button:hover { color: #1d4ed8 !important; }
                                """,
                            ],
                        ):
                            if st.button(policy_btn_text, key="score_logic_policy_open_btn"):
                                st.session_state["mt_score_logic_dialog_kind"] = "policy"
                                st.rerun()
                if policy_rows:
                    st.table(policy_rows)
                else:
                    st.caption("정책 점수 상세 신호 데이터가 없습니다.")

                evidence_btn_text = "evidence 로직 보기"
                reasons = [str(r).strip() for r in (score_breakdown.get("reasons") or []) if str(r).strip()]
                # evidence는 기본점수(20) + 근거 신호점수 + (도구성공/실패 보정) + (HITL 승인 보정)으로 합산된다.
                evidence_base = 20.0
                plan_bonus = 0.0
                fail_penalty = 0.0
                for reason in reasons:
                    if ("전체 성공" in reason or "계획한" in reason) and "evidence_score" in reason:
                        m = re.search(r"evidence_score\s*([+-]?\d+(?:\.\d+)?)", reason)
                        if m:
                            plan_bonus = float(m.group(1))
                            break
                for reason in reasons:
                    if "도구 실행 성공률" in reason and "evidence_score" in reason:
                        m = re.search(r"evidence_score\s*([+-]?\d+(?:\.\d+)?)", reason)
                        if m:
                            fail_penalty = float(m.group(1))
                            break
                hitl_bonus = 10.0 if any("담당자 검토 승인 의견 반영" in r for r in reasons) else 0.0
                evidence_known = evidence_base + evidence_signal_points + plan_bonus + fail_penalty + hitl_bonus
                evidence_final = float(evidence_score if evidence_score is not None else evidence_known)
                evidence_unknown = evidence_final - evidence_known
                calc_parts = [f"기본 {evidence_base:g}", f"근거 신호합 {evidence_signal_points:g}"]
                if abs(plan_bonus) > 0:
                    calc_parts.append(f"도구성공 보정 {plan_bonus:+g}")
                if abs(fail_penalty) > 0:
                    calc_parts.append(f"성공률 보정 {fail_penalty:+g}")
                if abs(hitl_bonus) > 0:
                    calc_parts.append(f"HITL 승인 보정 {hitl_bonus:+g}")
                if abs(evidence_unknown) >= 0.5:
                    calc_parts.append(f"기타 보정 {evidence_unknown:+.1f}")
                evidence_calc_formula = f"{' + '.join(calc_parts)} = {evidence_final:.1f} → {int(round(evidence_final))}점"
                evidence_calc_text = f"근거 계산 ({evidence_calc_formula})"
                with stylable_container(
                    key="score_logic_row_evidence",
                    css_styles=[
                        """{}""",
                        """
                        > [data-testid="stHorizontalBlock"] {
                            align-items: center !important;
                        }
                        """,
                    ],
                ):
                    e_text_col, e_btn_col = st.columns([0.80, 0.20])
                    with e_text_col:
                        st.caption(evidence_calc_text)
                    with e_btn_col:
                        with stylable_container(
                            key="score_logic_link_evidence",
                            css_styles=[
                                """{margin:0 !important; padding:0 !important;}""",
                                """
                                > [data-testid="stButton"] {
                                    margin: 0 !important;
                                    display: flex !important;
                                    justify-content: flex-end !important;
                                }
                                """,
                                """
                                > [data-testid="stButton"] > button {
                                    background: transparent !important;
                                    border: none !important;
                                    box-shadow: none !important;
                                    color: #2563eb !important;
                                    padding: 0 !important;
                                    margin: 0 !important;
                                    text-decoration: underline !important;
                                    font-weight: 700 !important;
                                    text-align: right !important;
                                    justify-content: flex-end !important;
                                    width: 100% !important;
                                }
                                """,
                                """
                                > [data-testid="stButton"] > button:hover { color: #1d4ed8 !important; }
                                """,
                            ],
                        ):
                            if st.button(evidence_btn_text, key="score_logic_evidence_open_btn"):
                                st.session_state["mt_score_logic_dialog_kind"] = "evidence"
                                st.rerun()
                if evidence_rows:
                    st.table(evidence_rows)
                else:
                    st.caption("근거 점수 상세 신호 데이터가 없습니다.")


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


def _normalize_pipeline_node(node: str | None) -> str:
    raw = str(node or "").lower().strip()
    alias = {
        "executor": "execute",
        "verifier": "verify",
    }
    return alias.get(raw, raw)


def _pipeline_state_from_events(events: list[dict[str, Any]]) -> tuple[list[str], str]:
    """이벤트 목록에서 완료된 노드와 현재 노드 추출."""
    all_ids = [n[0] for n in PIPELINE_NODES]
    completed_set: set[str] = set()
    current = all_ids[0] if all_ids else ""
    for event in events:
        payload = event.get("payload") or {}
        if event.get("event_type") != "AGENT_EVENT":
            continue
        event_type = str(payload.get("event_type") or "").upper()
        node_id = _normalize_pipeline_node(payload.get("node"))
        if node_id not in all_ids:
            continue
        if event_type in {"NODE_END", "SCREENING_RESULT"} and node_id == "screener":
            completed_set.add(node_id)
        elif event_type == "NODE_END":
            completed_set.add(node_id)
        elif event_type == "NODE_START":
            current = node_id
    completed = [node_id for node_id, _ in PIPELINE_NODES if node_id in completed_set]
    if completed and current in completed:
        idx = all_ids.index(completed[-1])
        current = all_ids[idx + 1] if idx + 1 < len(all_ids) else all_ids[-1]
    return completed, current


def render_timeline_cards(events: list[dict[str, Any]], *, view_mode: str = "business", nested_under_expander: bool = False) -> None:
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
        top_type = str(event.get("event_type") or "")
        if top_type == "AGENT_EVENT":
            node = _normalize_pipeline_node(payload.get("node") or "unknown")
            if str(payload.get("event_type") or "").upper() == "THINKING_TOKEN":
                continue
            if node not in node_groups:
                node_order.append(node)
            node_groups[node].append(event)
            continue
        if top_type.lower() == "confidence":
            node = "execute"
            synthetic = {
                "event_type": "AGENT_EVENT",
                "payload": {
                    "node": "execute",
                    "event_type": "SCORE_BREAKDOWN",
                    "message": event.get("detail") or event.get("message") or "",
                    "metadata": (event.get("score_breakdown") or event.get("payload") or {}),
                    "timestamp": event.get("at"),
                },
                "at": event.get("at"),
            }
            if node not in node_groups:
                node_order.append(node)
            node_groups[node].append(synthetic)
            continue
    latest_node = node_order[-1] if node_order else None
    _THINKING_ROW_CSS = """
    <style>
    .thinking-row { display: flex; align-items: flex-start; gap: 12px; padding: 10px 14px; margin: 6px 0; border-radius: 8px; border-left: 3px solid; min-width: 0; }
    .thinking-row.thought { background: #0f1a2e; border-color: #3b82f6; }
    .thinking-row.action { background: #0f2a1a; border-color: #22c55e; }
    .thinking-row.observation { background: #1a1500; border-color: #f59e0b; }
    .thinking-icon { font-size: 18px; margin-top: 2px; flex-shrink: 0; }
    .thinking-label { font-size: 10px; font-weight: 700; letter-spacing: 1.2px; text-transform: uppercase; opacity: 0.6; display: block; margin-bottom: 4px; }
    .thinking-content { flex: 1 1 0; min-width: 0; overflow-wrap: break-word; word-break: break-word; }
    .thinking-content p { margin: 0; font-size: 14px; line-height: 1.6; color: #e2e8f0; overflow-wrap: break-word; word-break: break-word; max-width: 100%; }
    </style>
    """
    try:
        st.html(_THINKING_ROW_CSS)
    except Exception:
        st.markdown(_THINKING_ROW_CSS, unsafe_allow_html=True)
    with stylable_container(key="timeline_shell", css_styles=[
        # 컨테이너 자체 스타일
        """{
            background: radial-gradient(circle at 1px 1px, rgba(15,23,42,0.10) 1px, transparent 0);
            background-size: 14px 14px;
            background-color: #f8fafc;
            border: 1px dashed #dbe2ea;
            border-radius: 18px;
            padding: 14px;
            overflow-x: hidden;
            max-width: 100%;
        }""",
        # stExpanderDetails div 오버라이드로 다크 배경이 덮는 것 방지
        """[data-testid="stCheckbox"] div {
            background: transparent !important;
        }""",
        # 토글 라벨: 진한 글자 + 포인터
        """[data-testid="stCheckbox"] label {
            color: #0f172a !important;
            font-weight: 600 !important;
            cursor: pointer !important;
            padding-left: 0 !important;
            margin-left: 0 !important;
        }""",
        # 호버 시 파란색
        """[data-testid="stCheckbox"]:hover label {
            color: #2563eb !important;
        }""",
        # 토글 스위치 트랙: OFF 상태
        """[role="switch"] {
            background: #cbd5e1 !important;
            border-color: #94a3b8 !important;
        }""",
        # 토글 스위치 트랙: ON 상태
        """[role="switch"][aria-checked="true"] {
            background: #2563eb !important;
            border-color: #2563eb !important;
        }""",
        # 토글 왼쪽 여백 제거
        """[data-testid="stCheckbox"] {
            padding-left: 0 !important;
            margin-left: 0 !important;
        }""",
        # 토글 첫번째 래퍼 div 패딩 제거
        """[data-testid="stCheckbox"] > div {
            padding: 0 !important;
            margin: 0 !important;
        }""",
        # 텍스트 overflow 방지
        """p, span {
            overflow-wrap: break-word !important;
            word-break: break-word !important;
            max-width: 100% !important;
        }""",
    ]):
        for node in node_order:
            node_events = node_groups[node]
            node_label = node_labels_map.get(node, node)
            is_latest = node == latest_node
            node_header = f"{'▶ ' if is_latest else '✓ '}{node_label}  ({len(node_events)}개 이벤트)"
            if nested_under_expander:
                # expander 중첩 불가로 노드별 st.toggle 사용: 기본 접힘, 펼치면 내용 표시
                expanded = st.toggle(
                    node_header,
                    value=False,
                    key=f"tl_nested_{node}",
                )
                if expanded:
                    for index, event in enumerate(node_events):
                        payload = event.get("payload") or {}
                        meta = payload.get("metadata") or {}
                        ev_type = str(payload.get("event_type") or "").upper()
                        # planner: PLAN_READY는 추론(THINKING_DONE)과 동일 내용이라 중복 표시 제거
                        if ev_type == "PLAN_READY":
                            continue
                        node_name = str(payload.get("node") or "").lower()
                        tool_name = payload.get("tool")
                        short_label = _TOOL_SHORT_LABEL.get(tool_name or "", "") or _humanize_stream_text(tool_name or "")
                        is_compact_tool = (
                            node_name == "execute"
                            and ev_type in {"TOOL_CALL", "TOOL_RESULT"}
                            and _is_generic_execute_tool_event(payload)
                        )
                        if is_compact_tool:
                            icon = EVENT_ICON_MAP.get(ev_type, "🤖")
                            cap = f"{icon} {fmt_dt_korea(event.get('at') or payload.get('timestamp')) or '-'} · {short_label}"
                            st.caption(cap, unsafe_allow_html=True)
                            if ev_type == "TOOL_CALL":
                                st.caption("_실행 중…_", unsafe_allow_html=True)
                            else:
                                obs = (payload.get("observation") or payload.get("message") or "").strip()
                                summary = _humanize_stream_text(obs)
                                line = (summary[:140] + ("…" if len(summary) > 140 else "")) if summary else "수집 완료."
                                st.write(line)
                        else:
                            icon = EVENT_ICON_MAP.get(ev_type, "🤖")
                            tool_frag = _tool_caption_fragment(ev_type, payload.get("tool"), meta.get("tool_description"), html_tooltip=True)
                            part2 = _stream_node_event_label(
                                payload.get("node") or "-", ev_type, tool_frag=tool_frag if tool_frag else None
                            )
                            cap = f"{icon} {fmt_dt_korea(event.get('at') or payload.get('timestamp')) or '-'} · {part2}"
                            st.caption(cap, unsafe_allow_html=True)
                            if ev_type == "SCORE_BREAKDOWN":
                                sb = meta.get("score_breakdown") or meta
                                policy_score = int(sb.get("policy_score") or sb.get("policy_score_raw") or 0)
                                evidence_score = int(sb.get("evidence_score") or sb.get("evidence_score_raw") or 0)
                                final_score = int(sb.get("final_score") or sb.get("score") or 0)
                                render_score_breakdown_card(policy_score, evidence_score, final_score)
                            display_message = payload.get("message")
                            if ev_type == "THINKING_DONE":
                                display_message = meta.get("reasoning") or display_message
                            if display_message:
                                display_message = _humanize_stream_text(str(display_message))
                                if ev_type == "THINKING_DONE":
                                    _html = _build_thinking_card_html(str(payload.get("node") or "agent"), str(display_message), is_complete=True)
                                    try:
                                        st.html(_html)
                                    except Exception:
                                        st.markdown(_html, unsafe_allow_html=True)
                                else:
                                    st.write(display_message)
                            thought = _humanize_stream_text((payload.get("thought") or "").strip())
                            action = _humanize_stream_text((payload.get("action") or "").strip())
                            observation = _humanize_stream_text((payload.get("observation") or "").strip())
                            blocks = []
                            blocks.append(_thinking_row_html("판단", "🧠", thought, "thought", "#3b82f6"))
                            blocks.append(_thinking_row_html("실행", "⚡", action, "action", "#22c55e"))
                            blocks.append(_thinking_row_html("발견", "🔍", observation, "observation", "#f59e0b"))
                            combined = "".join(blocks)
                            if combined:
                                _block_html = _THINKING_ROW_CSS + f'<div class="thinking-block">{combined}</div>'
                                try:
                                    st.html(_block_html)
                                except Exception:
                                    st.markdown(_block_html, unsafe_allow_html=True)
                        if view_mode == "debug":
                            st.json(payload)
            else:
                with st.expander(
                    label=node_header,
                    expanded=is_latest,
                ):
                    for index, event in enumerate(node_events):
                        payload = event.get("payload") or {}
                        meta = payload.get("metadata") or {}
                        ev_type = str(payload.get("event_type") or "").upper()
                        if ev_type == "PLAN_READY":
                            continue
                        node_name = str(payload.get("node") or "").lower()
                        tool_name = payload.get("tool")
                        short_label = _TOOL_SHORT_LABEL.get(tool_name or "", "") or _humanize_stream_text(tool_name or "")
                        is_compact_tool = (
                            node_name == "execute"
                            and ev_type in {"TOOL_CALL", "TOOL_RESULT"}
                            and _is_generic_execute_tool_event(payload)
                        )
                        if is_compact_tool:
                            icon = EVENT_ICON_MAP.get(ev_type, "🤖")
                            cap = f"{icon} {fmt_dt_korea(event.get('at') or payload.get('timestamp')) or '-'} · {short_label}"
                            st.caption(cap, unsafe_allow_html=True)
                            if ev_type == "TOOL_CALL":
                                st.caption("_실행 중…_", unsafe_allow_html=True)
                            else:
                                obs = (payload.get("observation") or payload.get("message") or "").strip()
                                summary = _humanize_stream_text(obs)
                                line = (summary[:140] + ("…" if len(summary) > 140 else "")) if summary else "수집 완료."
                                st.write(line)
                        else:
                            icon = EVENT_ICON_MAP.get(ev_type, "🤖")
                            tool_frag = _tool_caption_fragment(ev_type, payload.get("tool"), meta.get("tool_description"), html_tooltip=True)
                            part2 = _stream_node_event_label(
                                payload.get("node") or "-", ev_type, tool_frag=tool_frag if tool_frag else None
                            )
                            cap = f"{icon} {fmt_dt_korea(event.get('at') or payload.get('timestamp')) or '-'} · {part2}"
                            st.caption(cap, unsafe_allow_html=True)
                            if ev_type == "SCORE_BREAKDOWN":
                                sb = meta.get("score_breakdown") or meta
                                policy_score = int(sb.get("policy_score") or sb.get("policy_score_raw") or 0)
                                evidence_score = int(sb.get("evidence_score") or sb.get("evidence_score_raw") or 0)
                                final_score = int(sb.get("final_score") or sb.get("score") or 0)
                                render_score_breakdown_card(policy_score, evidence_score, final_score)
                            display_message = payload.get("message")
                            if ev_type == "THINKING_DONE":
                                display_message = meta.get("reasoning") or display_message
                            if display_message:
                                display_message = _humanize_stream_text(str(display_message))
                                if ev_type == "THINKING_DONE":
                                    _html = _build_thinking_card_html(str(payload.get("node") or "agent"), str(display_message), is_complete=True)
                                    try:
                                        st.html(_html)
                                    except Exception:
                                        st.markdown(_html, unsafe_allow_html=True)
                                else:
                                    st.write(display_message)
                            thought = _humanize_stream_text((payload.get("thought") or "").strip())
                            action = _humanize_stream_text((payload.get("action") or "").strip())
                            observation = _humanize_stream_text((payload.get("observation") or "").strip())
                            blocks = []
                            blocks.append(_thinking_row_html("판단", "🧠", thought, "thought", "#3b82f6"))
                            blocks.append(_thinking_row_html("실행", "⚡", action, "action", "#22c55e"))
                            blocks.append(_thinking_row_html("발견", "🔍", observation, "observation", "#f59e0b"))
                            combined = "".join(blocks)
                            if combined:
                                _block_html = _THINKING_ROW_CSS + f'<div class="thinking-block">{combined}</div>'
                                try:
                                    st.html(_block_html)
                                except Exception:
                                    st.markdown(_block_html, unsafe_allow_html=True)
                        if view_mode == "debug":
                            st.json(payload)


# 대표 메시지 선택 우선순위 (docs/work_info/langgraphPlan3.md 추가답변)
_REPR_MSG_PRIORITY = ["NODE_END", "GATE_APPLIED", "TOOL_RESULT", "PLAN_READY", "NODE_START"]


def _pick_representative_message(bucket: dict[str, Any]) -> str:
    """우선순위에 따라 대표 메시지 1개 선택. planner 노드는 계획 내용(PLAN_READY)을 우선 표시."""
    by_type = bucket.get("by_type") or {}
    node = (bucket.get("node") or "").lower()
    # planner: 조사 계획 수립된 내용을 보여주기 위해 PLAN_READY 우선
    if node == "planner":
        plan_msg = by_type.get("PLAN_READY")
        if plan_msg and str(plan_msg).strip():
            return str(plan_msg).strip()
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


def render_process_story(
    events: list[dict[str, Any]],
    *,
    debug_mode: bool = False,
    key_prefix: str = "main",
) -> None:
    rows = summarize_process_timeline(events)
    if not rows:
        render_empty_state("분석 완료 후 핵심 판단 흐름을 이 영역에서 단계별로 요약합니다.")
        return
    for idx, row in enumerate(rows, start=1):
        with stylable_container(
            key=f"process_story_{key_prefix}_{idx}_{row['node']}",
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
            st.markdown(
                f"**run_id** <span style='background:#ffffff;color:#111827;padding:0 2px;border-radius:3px;'>{item.get('run_id')}</span>",
                unsafe_allow_html=True,
            )
            lineage = item.get("lineage") or {}
            mode = str(lineage.get("mode") or "").strip()
            parent = str(lineage.get("parent_run_id") or "").strip()
            lineage_parts: list[str] = []
            # 기본값(mode=primary / parent 없음)은 정보성이 낮아 숨기고, 의미 있는 계보 정보만 노출한다.
            if mode and mode.lower() != "primary":
                lineage_parts.append(f"mode={mode}")
            if parent:
                lineage_parts.append(f"parent={parent}")
            if lineage_parts:
                st.caption(" / ".join(lineage_parts))
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
            if (meta.get("tool") or meta.get("skill")) == "policy_rulebook_probe":
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
    status = str(result.get("status") or "").upper()
    closed_statuses = {"COMPLETED", "COMPLETED_AFTER_HITL", "COMPLETED_AFTER_EVIDENCE", "RESOLVED", "OK"}
    pending_statuses = {"HITL_REQUIRED", "REVIEW_REQUIRED", "EVIDENCE_REJECTED", "HOLD_AFTER_HITL", "REVIEW_AFTER_HITL"}
    if status in closed_statuses:
        return False
    if status in pending_statuses:
        return True

    has_hitl_request = bool(latest_bundle.get("hitl_request"))
    has_hitl_response = bool(latest_bundle.get("hitl_response"))
    if has_hitl_request and not has_hitl_response:
        return True
    if has_hitl_response:
        return False
    return False


def _normalize_case_status_for_kpi(status: str | None) -> str | None:
    """
    UI KPI/목록 집계용 상태 버킷 정규화.
    - HOLD/REVIEW 계열은 모두 IN_REVIEW
    - 완료 계열은 RESOLVED
    - 실제 HITL 대기만 HITL_REQUIRED 유지
    """
    if not status:
        return status
    s = str(status).strip().upper()
    if s in {"COMPLETED", "COMPLETED_AFTER_HITL", "COMPLETED_AFTER_EVIDENCE", "OK", "RESOLVED"}:
        return "RESOLVED"
    if s in {"REVIEW_REQUIRED", "REVIEW_AFTER_HITL", "HOLD_AFTER_HITL", "EVIDENCE_REJECTED", "FAILED"}:
        return "IN_REVIEW"
    return s


def _hitl_state_key(kind: str, run_id: str | None) -> str:
    return f"mt_hitl_{kind}_{run_id or 'unknown'}"


def _prime_hitl_form_state(run_id: str, latest_bundle: dict[str, Any]) -> dict[str, str]:
    draft = latest_bundle.get("hitl_draft") or latest_bundle.get("hitl_response") or {}
    extra_facts = draft.get("extra_facts") or {}
    required_inputs = (latest_bundle.get("hitl_request") or {}).get("required_inputs") or []
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
    for req in required_inputs:
        field = (req.get("field") or "").strip()
        if field and field not in state_keys:
            state_keys[f"extra_{field}"] = _hitl_state_key(f"extra_{field}", run_id)
            if state_keys[f"extra_{field}"] not in st.session_state:
                st.session_state[state_keys[f"extra_{field}"]] = extra_facts.get(field, "")
    for field, key in state_keys.items():
        if key not in st.session_state and field in defaults:
            st.session_state[key] = defaults[field]
    return state_keys


def _format_covered_shortage(covered: int, total: int) -> str:
    """검증 대상 N개 중 M개 연결, K개 부족 문장 반환."""
    if total <= 0:
        return "규정 근거 연결이 기준보다 부족해 자동 확정을 보류했습니다. 담당자 검토가 필요합니다."
    shortage = total - covered
    return (
        f"검증 대상 {total}개 중 {covered}개만 규정 근거와 연결되어, "
        f"{shortage}개가 부족해 자동 확정을 보류했습니다. 담당자 검토가 필요합니다."
    )


def _plain_stop_reason(text: str, verification_summary: dict[str, Any] | None = None) -> str:
    """자동 확정 중단 이유를 사용자 이해하기 쉬운 문장으로 풀어서 반환."""
    t = (text or "").strip()
    if not t:
        return text
    # N개 중 M개 연결, K개 부족 형식으로 구체화 (verification_summary 또는 문장 내 숫자 사용)
    vs = verification_summary or {}
    covered = vs.get("covered")
    total = vs.get("total")
    if "근거 연결률이" in t and "자동 확정 기준에 미달" in t:
        # 백엔드 문장 "근거 연결률이 3/4 (75.0%)로 ..." 에서 숫자 추출
        m = re.search(r"근거 연결률이\s*(\d+)/(\d+)\s*", t)
        if m:
            c, tot = int(m.group(1)), int(m.group(2))
            return _format_covered_shortage(c, tot)
        if isinstance(covered, int) and isinstance(total, int):
            return _format_covered_shortage(covered, total)
        return t
    if "검증 게이트가 hold 상태로" in t or "검증 게이트 판정: hold" in t:
        if isinstance(covered, int) and isinstance(total, int):
            return _format_covered_shortage(covered, total)
        return "규정 근거 연결이 기준보다 부족해 자동 확정을 보류했습니다. 담당자 검토가 필요합니다."
    if "검증 게이트가 caution 상태로" in t or "검증 게이트 판정: caution" in t:
        if isinstance(covered, int) and isinstance(total, int):
            return _format_covered_shortage(covered, total)
        return "규정 근거 연결이 다소 부족해 주의 검토가 필요합니다."
    if "검증 게이트가 regenerate_citations 상태로" in t or "검증 게이트 판정: regenerate_citations" in t:
        return "일부 주장에 대한 규정 인용을 보완한 뒤 다시 검증하는 것이 좋습니다."
    if t.startswith("검증 신호:") or t.startswith("품질 신호:"):
        if "OK" in t:
            return "자동 판정을 할 수 없어 담당자 검토를 요청한 상태입니다."
        return "시스템 검증 결과상 담당자 검토가 필요한 상태입니다."
    return text


def _build_hitl_summary_sections(latest_bundle: dict[str, Any]) -> dict[str, list[str]]:
    result, screening_meta, policy_refs = _extract_workspace_result_context(latest_bundle)
    hitl_request = _resolve_hitl_request(latest_bundle)
    verification_summary = result.get("verification_summary") or {}

    # fallback 문구/유도 질문 생성 금지: 저장된 HITL payload만 표시
    review_reasons = [str(x) for x in (hitl_request.get("unresolved_claims") or []) if str(x).strip()]
    if not review_reasons:
        review_reasons = ["검토 필요 사유가 누락되었습니다. (run 데이터를 확인해 주세요)"]

    raw_stop: list[str] = [str(x) for x in (hitl_request.get("auto_finalize_blockers") or []) if x]
    if not raw_stop:
        gate_policy = verification_summary.get("gate_policy")
        covered = verification_summary.get("covered")
        total = verification_summary.get("total")
        if gate_policy:
            # N개 중 M개, K개 부족 구체 수치가 있으면 그 문구로 추가
            if isinstance(covered, int) and isinstance(total, int) and total > 0:
                raw_stop.append(_format_covered_shortage(covered, total))
            else:
                raw_stop.append(f"검증 게이트 판정: {gate_policy}")
        quality_codes = result.get("quality_gate_codes") or []
        # gate_policy만으로 이미 설명되면 "검증 신호: OK"는 생략 (중복 방지)
        if quality_codes and not (gate_policy and quality_codes == ["OK"]):
            raw_stop.append(f"검증 신호: {', '.join(str(x) for x in quality_codes)}")
        if not raw_stop and hitl_request.get("blocking_reason"):
            raw_stop.append(str(hitl_request.get("blocking_reason")))
    stop_reasons = [_plain_stop_reason(s, verification_summary) for s in raw_stop] if raw_stop else ["자동 확정 중단 사유 데이터가 비어 있습니다."]

    questions = [str(x) for x in (hitl_request.get("review_questions") or hitl_request.get("questions") or []) if str(x).strip()]
    if not questions:
        questions = ["검토 질문이 누락되었습니다. (run 데이터를 확인해 주세요)"]

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
            evidence_lines.append(f"검증 주장 근거 연결: {covered}/{total}건")
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


def _resolve_hitl_request(latest_bundle: dict[str, Any]) -> dict[str, Any]:
    current = latest_bundle.get("hitl_request")
    if isinstance(current, dict) and current:
        return current

    latest_result = ((latest_bundle.get("result") or {}).get("result") or {})
    if isinstance(latest_result, dict):
        result_req = latest_result.get("hitl_request")
        if isinstance(result_req, dict) and result_req:
            return result_req

    run_id = str(latest_bundle.get("run_id") or "")
    timeline = latest_bundle.get("timeline") or []
    for event in reversed(timeline):
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        event_type = str(event.get("event_type") or "").upper()

        # RUN_COMPLETED payload에도 hitl_request가 포함될 수 있어(특히 REVIEW_REQUIRED 경로) 역순으로 복원한다.
        if event_type in {"RUN_COMPLETED", "COMPLETED"}:
            direct_req = payload.get("hitl_request")
            if isinstance(direct_req, dict) and direct_req:
                return direct_req
            nested_result = payload.get("result")
            if isinstance(nested_result, dict):
                nested_req = nested_result.get("hitl_request")
                if isinstance(nested_req, dict) and nested_req:
                    return nested_req

        if event_type != "AGENT_EVENT":
            continue
        agent_ev_type = str(payload.get("event_type") or "").upper()
        if agent_ev_type == "HITL_REQUESTED":
            meta = payload.get("metadata") or {}
            if isinstance(meta, dict) and meta:
                return meta
        if agent_ev_type == "HITL_PAUSE":
            meta = payload.get("metadata") or {}
            if isinstance(meta, dict):
                pause_req = meta.get("hitl_request")
                if isinstance(pause_req, dict) and pause_req:
                    return pause_req

    history = latest_bundle.get("history") or []
    if run_id:
        for item in reversed(history):
            if str(item.get("run_id") or "") != run_id:
                continue
            req = item.get("hitl_request")
            if isinstance(req, dict) and req:
                return req
    for item in reversed(history):
        req = item.get("hitl_request")
        if isinstance(req, dict) and req:
            return req
    return {}


def _render_evidence_upload_section(
    latest_bundle: dict[str, Any],
    run_id: str,
    vkey: str,
    current_status: str,
    *,
    inside_popup: bool = False,
) -> tuple[bool, Any]:
    """
    증빙 비교 결과 표시 + (팝업이 아닐 때만) 완료 반영/재업로드 버튼 + 파일 업로더.
    inside_popup=True이면 버튼 없이 표시만 하고, 업로더는 반환용으로 둠.
    Returns: (has_evidence_result: bool, uploaded_file_or_none).
    """
    evidence_result = latest_bundle.get("evidence_document_result")
    is_rejected = str(current_status or "").upper() == "EVIDENCE_REJECTED"
    if is_rejected:
        st.caption("증빙 불일치로 반려된 케이스입니다. 새 증빙을 업로드하면 다시 검증하며, 일치 시 기존 분석에 이어 완료 처리됩니다.")
    if evidence_result is not None:
        passed = evidence_result.get("passed") is True
        st.caption("증빙 비교 결과")
        if passed:
            st.success("증빙 검증 통과. 아래 통합 버튼으로 반영 후 케이스가 완료 처리됩니다.")
        else:
            reasons = evidence_result.get("reasons") or []
            st.warning("증빙 불일치: " + ("; ".join(reasons[:5]) if reasons else "항목 확인 필요"))
            if not is_rejected:
                st.caption("불일치인 경우에도 아래 통합 버튼으로 '증빙 반려' 확정 후 케이스를 마감할 수 있습니다.")
        if not inside_popup and st.button("완료 반영 (재분석 확정)", key=f"hitl_evidence_resume_{vkey}"):
            try:
                resp = post(f"/api/v1/analysis-runs/{run_id}/evidence-resume")
                new_status = (resp or {}).get("status") or ("COMPLETED_AFTER_EVIDENCE" if passed else "EVIDENCE_REJECTED")
                st.session_state["mt_evidence_resume_done"] = {"vkey": vkey, "status": new_status, "passed": passed}
                st.rerun()
            except Exception as e:
                st.error(f"반영 실패: {e}")
    uploaded = None
    if is_rejected or evidence_result is None:
        ev_upload_key = f"hitl_evidence_file_{vkey}"
        uploaded = st.file_uploader(
            "증빙 문서 (PDF/이미지 등)" + (" — 재업로드" if is_rejected else ""),
            type=["pdf", "png", "jpg", "jpeg"],
            key=ev_upload_key,
        )
        if not inside_popup and st.button("증빙 업로드 후 재분석", key=f"hitl_evidence_upload_btn_{vkey}"):
            if not uploaded:
                st.warning("파일을 선택한 뒤 버튼을 눌러 주세요.")
            else:
                try:
                    file_bytes = uploaded.getvalue()
                    post_multipart(f"/api/v1/analysis-runs/{run_id}/evidence-upload", uploaded.name or "upload", file_bytes)
                    post(f"/api/v1/analysis-runs/{run_id}/evidence-resume")
                    st.success("증빙 업로드 및 재분석 반영이 완료되었습니다.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
    return (evidence_result is not None, uploaded)


def render_hitl_panel(latest_bundle: dict[str, Any], *, vkey: str | None = None) -> None:
    run_id = latest_bundle.get("run_id")
    result_inner = (latest_bundle.get("result") or {}).get("result") or {}
    current_status = str(result_inner.get("status") or "").upper()
    if not run_id:
        return
    # 검토 상태일 때 분기 없이 HITL·증빙 전체를 한 팝업에 표시
    hitl_request = _resolve_hitl_request(latest_bundle)
    if not hitl_request:
        st.warning("검토 요청 세부 데이터(hitl_request)를 아직 불러오지 못했습니다. 증빙/의견 제출은 계속 가능합니다.")
    bundle_for_hitl = dict(latest_bundle)
    bundle_for_hitl["hitl_request"] = hitl_request
    form_keys = _prime_hitl_form_state(run_id, bundle_for_hitl)
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
          overflow-x: hidden !important;
          overflow-y: visible !important;
          background: #ffffff !important;
        }
        div[data-testid="stDialog"] div[role="dialog"] > div {
          width: 100% !important;
          max-width: 100% !important;
          min-width: 0 !important;
          max-height: calc(90vh - 3.5rem) !important;
          overflow-x: hidden !important;
          overflow-y: auto !important;
          background: #ffffff !important;
          box-sizing: border-box !important;
        }
        div[data-testid="stDialog"] [data-testid="stVerticalBlock"] {
          max-width: 100% !important;
          min-width: 0 !important;
        }
        /* 마크다운(KPI 그리드 등)이 상위 overflow에 잘리지 않도록: min-width 0으로 수축 허용, 클립 제거 */
        div[data-testid="stDialog"] [data-testid="stMarkdown"] {
          max-width: 100% !important;
          min-width: 0 !important;
          overflow-wrap: break-word !important;
        }
        div[data-testid="stDialog"] [data-testid="stMarkdown"] > div {
          max-width: 100% !important;
          min-width: 0 !important;
          overflow: visible !important;
        }
        /* 검토 의견 text area 영역이 우측에서 잘리지 않도록 */
        div[data-testid="stDialog"] [data-testid="stTextArea"],
        div[data-testid="stDialog"] [data-testid="stTextArea"] > div {
          max-width: 100% !important;
          min-width: 0 !important;
        }
        div[data-testid="stDialog"] [data-testid="stTextArea"] textarea {
          max-width: 100% !important;
          box-sizing: border-box !important;
        }
        /* HITL 팝업 본문 내 제목+라디오 행이 박스 우측을 넘지 않도록 */
        div[data-testid="stDialog"] div[role="dialog"] > div:last-child [data-testid="stHorizontalBlock"]:first-of-type {
          max-width: 100% !important;
          min-width: 0 !important;
        }
        div[data-testid="stDialog"] div[role="dialog"] > div:last-child [data-testid="stHorizontalBlock"]:first-of-type > div:last-child {
          max-width: 27% !important;
          min-width: 0 !important;
        }
        div[data-testid="stDialog"] [data-testid="stVerticalBlock"] > div:first-child {
          margin-top: 0 !important;
          padding-top: 0 !important;
        }
        /* 제목과 본문 사이 간격 축소 — HITL 팝업 제목 밑 여백 최소화 */
        div[data-testid="stDialog"] [data-testid="stDialogHeader"],
        div[data-testid="stDialog"] div[role="dialog"] > div:first-child {
          margin-bottom: 0 !important;
          padding-bottom: 0 !important;
        }
        div[data-testid="stDialog"] div[role="dialog"] > div:last-child {
          margin-top: 0 !important;
          padding-top: 0.25rem !important;
        }
        div[data-testid="stDialog"] div[role="dialog"] > div:last-child > div {
          margin-top: 0 !important;
          padding-top: 0 !important;
        }
        div[data-testid="stDialog"] [data-testid="stVerticalBlock"] {
          padding-top: 0 !important;
          margin-top: 0.125rem !important;
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
        /* 검토 요청 원본 보기: 배경 흰색, 텍스트 검정 + 아래 여백 최소화(증빙 업로드 위 빈 공간 제거) */
        div[data-testid="stDialog"] details {
          margin-bottom: 0.1rem !important;
          background: #ffffff !important;
          color: #0f172a !important;
        }
        div[data-testid="stDialog"] details summary,
        div[data-testid="stDialog"] [data-testid="stExpanderDetails"],
        div[data-testid="stDialog"] [data-testid="stExpanderDetails"] * {
          background: #ffffff !important;
          color: #0f172a !important;
        }
        /* 구분선·증빙 업로드 제목 위아래 여백 최소화 */
        div[data-testid="stDialog"] hr {
          margin: 0.15rem 0 !important;
        }
        div[data-testid="stDialog"] h4 {
          margin-top: 0.15rem !important;
          margin-bottom: 0.2rem !important;
        }
        /* 증빙 업로드 위 빈 공간 축소: 폼 내 세로 블록 간격 */
        div[data-testid="stDialog"] [data-testid="stForm"] [data-testid="stVerticalBlock"] {
          row-gap: 0.2rem !important;
        }
        /* 파일 업로더가 다이얼로그 우측 밖으로 나가지 않도록 */
        div[data-testid="stDialog"] [data-testid="stFileUploader"],
        div[data-testid="stDialog"] [data-testid="stFileUploader"] > div,
        div[data-testid="stDialog"] [data-testid="stFileUploaderDropzone"],
        div[data-testid="stDialog"] [data-testid="stFileUploaderDropzone"] * {
          max-width: 100% !important;
          min-width: 0 !important;
          box-sizing: border-box !important;
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
          width: 100%;
          max-width: 100%;
          min-width: 0;
          box-sizing: border-box;
          overflow-x: hidden;
          overflow-y: visible;
        }
        div[data-testid="stDialog"] [data-testid="stHorizontalBlock"],
        div[data-testid="stDialog"] [data-testid="stHorizontalBlock"] > div,
        div[data-testid="stDialog"] [data-testid="stVerticalBlock"] {
          max-width: 100% !important;
          min-width: 0 !important;
          box-sizing: border-box !important;
        }
        div[data-testid="stDialog"] [data-testid="stHorizontalBlock"]:first-of-type {
          width: 100% !important;
          max-width: 100% !important;
          min-width: 0 !important;
        }
        div[data-testid="stDialog"] [data-testid="stHorizontalBlock"]:first-of-type > div:first-child {
          flex: 0 0 auto !important;
          min-width: 0 !important;
          max-width: 36% !important;
        }
        div[data-testid="stDialog"] [data-testid="stHorizontalBlock"]:first-of-type > div:nth-child(2) {
          flex: 1 1 auto !important;
          min-width: 0 !important;
          display: flex !important;
          justify-content: flex-end !important;
          align-items: center !important;
        }
        div[data-testid="stDialog"] [data-testid="stHorizontalBlock"]:first-of-type > div:nth-child(2) > div {
          display: flex !important;
          justify-content: flex-end !important;
          width: 100% !important;
        }
        div[data-testid="stDialog"] [data-testid="stHorizontalBlock"]:first-of-type > div:last-child {
          flex: 0 0 auto !important;
          min-width: 0 !important;
          max-width: 27% !important;
          display: flex !important;
          justify-content: flex-end !important;
          align-items: center !important;
        }
        div[data-testid="stDialog"] [data-testid="stHorizontalBlock"]:first-of-type > div:last-child > div {
          display: flex !important;
          justify-content: flex-end !important;
          width: 100% !important;
        }
        div[data-testid="stDialog"] [data-testid="stHorizontalBlock"]:first-of-type [role="radiogroup"] {
          justify-content: flex-end !important;
          max-width: 100% !important;
        }
        /* 증빙 파일 업로더: 박스 안에만 표시 */
        div[data-testid="stDialog"] [data-testid="stFileUploader"],
        div[data-testid="stDialog"] [data-testid="stFileUploader"] > div,
        div[data-testid="stDialog"] [data-testid="stFileUploaderDropzone"],
        div[data-testid="stDialog"] [data-testid="stFileUploaderDropzone"] * {
          max-width: 100% !important;
          min-width: 0 !important;
          box-sizing: border-box !important;
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
          max-width: 100%;
          box-sizing: border-box;
          overflow-wrap: break-word;
        }
        .mt-hitl-grid {
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: 10px;
          margin: 4px 0 10px 0;
          width: 100%;
          max-width: 100%;
          min-width: 0;
          box-sizing: border-box;
        }
        .mt-hitl-box {
          border: 1px solid #e5e7eb;
          border-radius: 16px;
          background: #ffffff;
          padding: 12px 13px;
          height: 210px;
          min-height: 210px;
          display: flex;
          flex-direction: column;
          min-width: 0;
          max-width: 100%;
          overflow: hidden;
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
          min-width: 0;
          overflow: hidden;
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
          padding-right: 8px;
          color: #334155;
          line-height: 1.45;
          font-size: 0.88rem;
          flex: 1 1 auto;
          min-height: 0;
          overflow-y: auto;
          overflow-x: hidden;
          word-break: break-word;
          overflow-wrap: break-word;
          width: 100%;
          max-width: 100%;
          box-sizing: border-box;
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
        div[data-testid="stDialog"] div[data-baseweb="radio"] label,
        div[data-testid="stDialog"] div[role="radiogroup"] label,
        div[data-testid="stDialog"] div[role="radiogroup"] * {
          color: #0f172a !important;
        }
        div[data-testid="stDialog"] [data-testid="stTextInput"] label,
        div[data-testid="stDialog"] [data-testid="stTextArea"] label,
        div[data-testid="stDialog"] [data-testid="stCheckbox"] label,
        div[data-testid="stDialog"] [data-testid="stCheckbox"] span {
          color: #0f172a !important;
        }
        div[data-testid="stDialog"] [data-testid="stTextInput"] input,
        div[data-testid="stDialog"] [data-testid="stTextArea"] textarea {
          background: #ffffff !important;
          color: #0f172a !important;
          -webkit-text-fill-color: #0f172a !important;
          caret-color: #0f172a !important;
          border: 1px solid #cbd5e1 !important;
          border-radius: 12px !important;
        }
        div[data-testid="stDialog"] [data-testid="stTextInput"] input::placeholder,
        div[data-testid="stDialog"] [data-testid="stTextArea"] textarea::placeholder {
          color: #64748b !important;
          opacity: 1 !important;
        }
        div[data-testid="stDialog"] [data-testid="stTextInput"] > div,
        div[data-testid="stDialog"] [data-testid="stTextArea"] > div {
          background: transparent !important;
        }
        div[data-testid="stDialog"] [data-testid="stCheckbox"] > div {
          background: transparent !important;
        }
        div[data-testid="stDialog"] [data-testid="stJson"],
        div[data-testid="stDialog"] [data-testid="stJson"] *,
        div[data-testid="stDialog"] [data-testid="stJson"] pre,
        div[data-testid="stDialog"] [data-testid="stJson"] code,
        div[data-testid="stDialog"] [data-testid="stJson"] pre * {
          background: #ffffff !important;
          color: #0f172a !important;
        }
        div[data-testid="stDialog"] [data-testid="stAlert"] {
          background: #fffbeb !important;
          border: 1px solid #fde68a !important;
          color: #92400e !important;
        }
        div[data-testid="stDialog"] [data-testid="stAlert"] * {
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
        # HITL 영역: 항상 표시 (대기 중이 아니면 fallback 요약으로 표시)
        summary = _build_hitl_summary_sections(bundle_for_hitl)
        # 조건은 아래 '자동 확정 중단 이유' 박스에 나열되므로, 상단은 안내만 표시
        stop_reasons = summary.get("stop_reasons") or []
        if stop_reasons:
            lead_message = "아래 **자동 확정 중단 이유**에서 해당 조건을 확인해 주세요."
        else:
            lead_message = (
                hitl_request.get("why_hitl")
                or hitl_request.get("blocking_reason")
                or "담당자 검토가 필요한 상태입니다."
            )
        title_col, radio_col, btn_col = st.columns([0.35, 0.40, 0.25])
        with title_col:
            st.markdown("#### 담당자 검토 (HITL)")
        with radio_col:
            decision_val = st.radio(
                "판단 선택",
                options=["보류/추가 검토", "승인 가능"],
                horizontal=True,
                label_visibility="collapsed",
                key=form_keys["decision"],
            )
        with btn_col:
            submit_clicked = st.button(
                "검토 반영 후 분석 이어가기",
                type="primary",
                key=f"hitl_review_submit_{vkey or run_id}",
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
        required_inputs = (hitl_request.get("required_inputs") or [])
        _req_fields = {(req.get("field") or "").strip() for req in required_inputs}
        info_cols = st.columns(3)
        with info_cols[0]:
            st.text_input("검토자", key=form_keys["reviewer"])
        with info_cols[1]:
            st.text_input("업무 목적" + (" *" if "business_purpose" in _req_fields else ""), key=form_keys["business_purpose"], placeholder="예: 주말 장애 대응 회의")
        with info_cols[2]:
            st.text_input("참석자(쉼표 구분)" + (" *" if "attendees" in _req_fields else ""), key=form_keys["attendees"], placeholder="예: 홍길동, 김민수, 외부 파트너 1명")
        for req in required_inputs:
            field = (req.get("field") or "").strip()
            if not field or field in ("business_purpose", "attendees"):
                continue
            label = (req.get("guide") or req.get("reason") or field).strip()
            key = form_keys.get(f"extra_{field}")
            if key:
                st.text_input(f"{label} *", key=key, placeholder=f"규정 요구 항목(필수): {label[:50]}")
        # 검토 의견 placeholder: LLM/규정에서 요구한 항목을 동적으로 안내
        # UI에는 LLM 생성 질문(review_questions)만 노출한다.
        # required_inputs는 서버 검증(누락 체크) 용도로 유지한다.
        must_fill: list[str] = []
        for q in (hitl_request.get("review_questions") or hitl_request.get("questions") or []):
            s = str(q or "").strip()
            if s:
                must_fill.append(s[:120])
        if must_fill:
            comment_placeholder = "반드시 작성할 내용:\n" + "\n".join(f"• {m}" for m in must_fill[:8]) + "\n\n왜 승인 또는 보류로 판단했는지 핵심 근거를 적습니다."
        else:
            comment_placeholder = "왜 승인 또는 보류로 판단했는지 핵심 근거를 적습니다.\n예: 주말 대응 프로젝트로 야간 회의 후 식대 사용. 사전 승인 메일 확인됨."
        st.text_area(
            "검토 의견",
            height=96,
            key=form_keys["comment"],
            placeholder=comment_placeholder,
        )
        with st.expander("검토 요청 원본 보기", expanded=False):
            st.json(summary["debug"])

        # 증빙 영역: 항상 표시 (팝업 내에서는 통합 버튼만 사용)
        st.markdown("---")
        st.markdown("#### 증빙 업로드")
        has_evidence_result, uploaded_file = _render_evidence_upload_section(
            latest_bundle, run_id, vkey or run_id or "ev", current_status, inside_popup=True
        )

        # 통합 제출: 버튼은 상단(담당자 검토 라인 우측)에 있음
        if submit_clicked:
            extra_facts = {}
            for k, form_key in form_keys.items():
                if k.startswith("extra_") and form_key in st.session_state:
                    extra_facts[k.replace("extra_", "", 1)] = str(st.session_state.get(form_key, "") or "").strip()
            reviewer = str(st.session_state.get(form_keys["reviewer"], "") or "").strip()
            comment = str(st.session_state.get(form_keys["comment"], "") or "").strip()
            business_purpose = str(st.session_state.get(form_keys["business_purpose"], "") or "").strip()
            attendees_raw = str(st.session_state.get(form_keys["attendees"], "") or "")
            # 위 st.radio() 반환값 사용 — session_state 기본값으로 인한 approved=False 오류 방지
            approved = decision_val == "승인 가능"
            logger.info(
                "[HITL_SUBMIT] 판단 선택: decision_val=%s approved=%s (라디오 반환값 기준)",
                decision_val,
                approved,
            )
            # 필수 입력값 검사: 누락 시 제출/팝업 닫기 불가
            missing_required: list[str] = []
            for req in required_inputs:
                field = (req.get("field") or "").strip()
                if not field:
                    continue
                label = (req.get("guide") or req.get("reason") or req.get("field") or field).strip()
                if field == "attendees":
                    if not [p.strip() for p in attendees_raw.split(",") if p.strip()]:
                        missing_required.append(label)
                elif field == "business_purpose":
                    if not business_purpose:
                        missing_required.append(label)
                else:
                    if not (extra_facts.get(field) or "").strip():
                        missing_required.append(label)
            if missing_required:
                logger.warning(
                    "[HITL_SUBMIT] 필수 입력 누락 상태로 제출 진행: run_id=%s missing=%s",
                    run_id,
                    missing_required[:8],
                )
                st.warning(
                    "필수 입력 일부가 비어 있습니다. 우선 검토 반영 후 분석을 이어가며, 필요하면 다음 단계에서 추가 입력 요청이 다시 표시됩니다."
                )
            hitl_response = {
                "reviewer": reviewer or "FINANCE_REVIEWER",
                "comment": comment or None,
                "business_purpose": business_purpose or None,
                "attendees": [p.strip() for p in attendees_raw.split(",") if p.strip()],
                "approved": approved,
                "extra_facts": extra_facts,
            }
            evidence_uploaded = has_evidence_result
            if uploaded_file is not None and getattr(uploaded_file, "getvalue", None):
                try:
                    file_bytes = uploaded_file.getvalue()
                    post_multipart(
                        f"/api/v1/analysis-runs/{run_id}/evidence-upload",
                        uploaded_file.name or "upload",
                        file_bytes,
                    )
                    evidence_uploaded = True
                except Exception as e:
                    st.error(f"증빙 업로드 실패: {e}")
                    evidence_uploaded = evidence_uploaded or False
            if not evidence_uploaded and not _has_pending_hitl(latest_bundle):
                st.warning("증빙이 필요한 경우 파일을 선택한 뒤 다시 눌러 주세요.")
            else:
                try:
                    from services.schemas import HitlSubmitRequest
                    # hitl_request 미로드 시에도 폼에서 입력한 hitl_response를 항상 전송. 백엔드는 runtime/aux에서 hitl_request를 채우므로 400을 피함.
                    payload_review = {
                        "hitl_response": HitlSubmitRequest(**hitl_response).model_dump(),
                        "evidence_uploaded": evidence_uploaded,
                    }
                    st.session_state["mt_pending_review_submit"] = {
                        "run_id": run_id,
                        "voucher_key": latest_bundle.get("voucher_key") or vkey,
                        "payload": payload_review,
                    }
                    hr = payload_review.get("hitl_response") or {}
                    _comment = str(hr.get("comment") or "")
                    _preview = (_comment[:80] + "…") if len(_comment) > 80 else _comment
                    logger.info(
                        "[RESUME_TRACE] 분석 이어가기 버튼 클릭: run_id=%s voucher_key=%s approved=%s comment_len=%s comment_preview=%s evidence_uploaded=%s — mt_pending_review_submit 설정",
                        run_id, latest_bundle.get("voucher_key") or vkey, hr.get("approved"), len(_comment), _preview or "(없음)", evidence_uploaded,
                    )
                    logger.info("[HITL_CLOSE] 버튼 클릭: run_id=%s — mt_pending_review_submit 설정, open=False, st.rerun()", run_id)
                    st.session_state.pop(_hitl_state_key("dismissed", run_id), None)
                    st.session_state.pop(_hitl_state_key("open", run_id), None)
                    st.session_state[_hitl_state_key("open", run_id)] = False
                    st.session_state.pop(_hitl_state_key("shown", run_id), None)
                    # 다음 run에서 selected_vkey가 바뀌어도 다이얼로그가 즉시 닫히도록 선제 스킵 키를 심는다.
                    st.session_state["mt_skip_hitl_dialog_run_id"] = run_id
                    st.rerun()
                except Exception as e:
                    st.error(f"제출 실패: {e}")


@st.dialog("HITL 팝업", width="large")
def render_hitl_dialog(latest_bundle: dict[str, Any], *, vkey: str | None = None) -> None:
    render_hitl_panel(latest_bundle, vkey=vkey)


def build_workspace_plan_steps(latest_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    timeline = latest_bundle.get("timeline") or []
    node_order = ["screener", "intake", "planner", "execute", "critic", "verify", "hitl_pause", "reporter", "finalizer"]
    meta = {
        "screener": ("전표 분석 / 케이스 분류", "전표 데이터(발생 시각·근태·가맹점 업종 코드(MCC)·예산 등)를 분석해 위반 유형을 식별합니다."),
        "intake": ("입력 해석", "전표 입력값과 위험 지표를 정규화합니다."),
        "planner": ("조사 계획 수립", "검증할 사실과 사용할 도구 순서를 계획합니다."),
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
    render_panel_header("케이스", "분석할 전표를 선택합니다. 우측은 실시간 실행과 판단 리뷰.")
    # 4개 KPI: items는 /api/v1/vouchers 응답(각 item.case_status = AgentCase.status).
    # 정책: HITL 대기 = 실제 사람 입력 대기(HITL_REQUIRED)만 집계.
    review_statuses = {"NEW", "IN_REVIEW", "REVIEW_REQUIRED", "EVIDENCE_REJECTED", "REVIEW_AFTER_HITL", "HOLD_AFTER_HITL"}
    completed_statuses = {"COMPLETED", "COMPLETED_AFTER_HITL", "COMPLETED_AFTER_EVIDENCE", "RESOLVED", "OK"}
    hitl_wait_statuses = {"HITL_REQUIRED"}

    review_count = len(
        [item for item in items if str(item.get("case_status") or "").upper() in review_statuses]
    )
    completed_count = len(
        [item for item in items if str(item.get("case_status") or "").upper() in completed_statuses]
    )
    hitl_count = len(
        [item for item in items if str(item.get("case_status") or "").upper() in hitl_wait_statuses]
    )
    # 상단 KPI(클릭) → 목록 필터 상태
    grouped = {
        "전체": items,
        "검토 필요": [item for item in items if str(item.get("case_status") or "").upper() in review_statuses],
        "완료": [item for item in items if str(item.get("case_status") or "").upper() in completed_statuses],
        "HITL 대기": [item for item in items if str(item.get("case_status") or "").upper() in hitl_wait_statuses],
    }
    active_filter = str(st.session_state.get("mt_case_filter") or "전체")
    if active_filter not in grouped:
        active_filter = "전체"

    st.markdown(
        """
        <style>
        /* 헤더·설명 텍스트와 KPI 행 사이 여백 (겹침 방지) */
        [class*="st-key-workspace_case_queue_card"] .mt-panel-header {
          margin-bottom: 4px !important;
        }
        [class*="st-key-workspace_case_queue_card"] .mt-panel-sub {
          margin-top: 1px !important;
          margin-bottom: 12px !important;
          line-height: 1.35 !important;
          white-space: normal !important;
          overflow-wrap: anywhere !important;
          word-break: break-word !important;
          max-width: 100% !important;
        }
        [class*="st-key-workspace_case_queue_card"] [data-testid="stElementContainer"]:has(.mt-panel-header) {
          margin-bottom: 10px !important;
        }

        /* ── KPI 4개 영역 동일 width (전체 케이스·검토 필요·완료·HITL 대기) ─────────────────────────────────────────────────── */
        /* :has 기반 부모 선택자 대신 key 기반으로 직접 고정(초기 렌더/재렌더 깜빡임 방지) */
        div[class*="st-key-case_kpi_"] {
          display: block !important;
          width: 100% !important;
          max-width: 100% !important;
          min-width: 0 !important;
          min-height: 0 !important;
          height: auto !important;
          margin: 0 !important;
          padding: 0 !important;
          box-sizing: border-box !important;
        }
        div[class*="st-key-case_kpi_"] [data-testid="element-container"],
        div[class*="st-key-case_kpi_"] [data-testid="stElementContainer"],
        div[class*="st-key-case_kpi_"] [data-testid="stVerticalBlock"] {
          width: 100% !important;
          max-width: 100% !important;
          min-width: 0 !important;
          min-height: 0 !important;
          height: auto !important;
          margin: 0 !important;
          padding: 0 !important;
          box-sizing: border-box !important;
        }
        /* KPI 카드 버튼 스타일 (tabs 제거, KPI 클릭으로 필터) */
        [class*="st-key-case_kpi_"] [data-testid="stButton"] > button {
          width: 100% !important;
          text-align: center !important;
          padding: 8px 10px !important;
          border-radius: 14px !important;
          border: 1px solid #e5e7eb !important;
          background: rgba(255,255,255,0.98) !important;
          box-shadow: 0 10px 24px rgba(15,23,42,0.05) !important;
          min-height: 52px !important;
          white-space: pre-line !important;
          font-size: 1.75rem !important; /* count */
          font-weight: 900 !important;
          font-variant-numeric: tabular-nums !important;
          line-height: 1.08 !important;
        }
        [class*="st-key-case_kpi_"] [data-testid="stButton"] > button p {
          margin: 0 !important;
          font-size: 1.75rem !important;
          font-weight: 900 !important;
          line-height: 1.08 !important;
          font-variant-numeric: tabular-nums !important;
        }
        /* 첫 줄(타이틀)은 더 작게 */
        [class*="st-key-case_kpi_"] [data-testid="stButton"] > button p::first-line {
          font-size: 0.74rem !important;
          font-weight: 800 !important;
          color: #64748b !important;
        }
        [class*="st-key-case_kpi_"] [data-testid="stButton"] > button:hover {
          border-color: #bfdbfe !important;
          box-shadow: 0 12px 28px rgba(37,99,235,0.10) !important;
        }
        [class*="st-key-case_kpi_sel_"] [data-testid="stButton"] > button {
          border: 2px solid #2563eb !important;
          box-shadow: 0 0 0 3px rgba(37,99,235,0.08), 0 12px 26px rgba(15,23,42,0.08) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with stylable_container(
        key=f"workspace_case_kpi_row_{active_filter}",
        css_styles="""{margin: 0 0 16px 0; padding: 0;}""",
    ):
        k1, k2, k3, k4 = st.columns(4)
        with k1:
            key = "case_kpi_sel_all" if active_filter == "전체" else "case_kpi_all"
            if st.button(f"전체 케이스\n{len(items)}", key=key):
                st.session_state["mt_case_filter"] = "전체"
                st.rerun()
        with k2:
            key = "case_kpi_sel_review" if active_filter == "검토 필요" else "case_kpi_review"
            if st.button(f"검토 필요\n{review_count}", key=key):
                st.session_state["mt_case_filter"] = "검토 필요"
                st.rerun()
        with k3:
            key = "case_kpi_sel_done" if active_filter == "완료" else "case_kpi_done"
            if st.button(f"완료\n{completed_count}", key=key):
                st.session_state["mt_case_filter"] = "완료"
                st.rerun()
        with k4:
            key = "case_kpi_sel_hitl" if active_filter == "HITL 대기" else "case_kpi_hitl"
            if st.button(f"HITL 대기\n{hitl_count}", key=key):
                st.session_state["mt_case_filter"] = "HITL 대기"
                st.rerun()
    filtered = grouped.get(active_filter) or []
    if not filtered:
        render_empty_state("표시할 케이스가 없습니다.")
        return

    # 배지는 st.markdown(HTML) → 시각 레이어 (pointer-events: none)
    # 버튼은 margin-top:-55px 으로 배지 영역까지 올려 클릭 영역이 카드 전체를 커버
    st.markdown("""
    <style>
    [class*="st-key-workspace_case_scroll_"] {
      display: block !important;
      max-height: 66vh !important;
      overflow-y: auto !important;
      padding-right: 6px !important;
      margin-top: 18px !important;
      min-height: 0 !important;
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
      margin-top: 0 !important;
      position: relative !important;
      z-index: 1 !important;
    }
    /* 실제 button — 투명, 카드 컨텐츠 텍스트 스타일, 배지 높이만큼만 위 패딩(초록 영역 축소) */
    [class*="st-key-case_btn_"] [data-testid="stButton"] > button {
      width: 100% !important;
      height: auto !important;
      min-height: unset !important;
      text-align: left !important;
      padding: 28px 0 10px 0 !important;
      padding-left: 0 !important;
      border: none !important;
      background: transparent !important;
      box-shadow: none !important;
      color: #0f172a !important;
      font-size: 0.9rem !important;
      white-space: pre-wrap !important;
      overflow-wrap: anywhere !important;
      word-break: break-word !important;
      line-height: 1.35 !important;
      cursor: pointer !important;
      margin: 0 !important;
    }
    /* 버튼 내부 마크다운/문단 — 왼쪽 여백 제거, 줄 간격 축소 */
    [class*="st-key-case_btn_"] [data-testid="stButton"] [data-testid="stMarkdownContainer"],
    [class*="st-key-case_btn_"] [data-testid="stButton"] [data-testid="stMarkdownContainer"] p,
    [class*="st-key-case_btn_"] [data-testid="stButton"] > button > div {
      padding-left: 0 !important;
      margin-left: 0 !important;
    }
    [class*="st-key-case_btn_"] [data-testid="stMarkdownContainer"] p {
      margin: 0 0 2px 0 !important;
      line-height: 1.35 !important;
      white-space: pre-wrap !important;
      overflow-wrap: anywhere !important;
      word-break: break-word !important;
    }
    </style>
    """, unsafe_allow_html=True)
    with st.container(key=f"workspace_case_scroll_{active_filter}"):
        for item in filtered:
            case_key = item["voucher_key"]
            is_selected = case_key == selected_key
            status = status_display_name(item.get("case_status"))
            severity = severity_display_name(item.get("severity"))
            case_type = case_type_display_name(item.get("case_type"))
            occurred_at = fmt_dt(item.get("occurred_at")) or "-"
            amount = f"{fmt_num(item.get('amount'))} {item.get('currency') or ''}".strip()
            merchant = item.get("merchant_name") or "-"
            title = item.get("demo_name") or merchant
            wrap_key = f"case_btn_sel_{active_filter}_{case_key}" if is_selected else f"case_btn_{active_filter}_{case_key}"
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
                btn_label = (
                    f"**{title}**\n\n"
                    f"{amount} · {occurred_at}\n\n"
                    f"전표키 {case_key}\n가맹점 {merchant}"
                )
                if st.button(
                    btn_label,
                    key=f"select_{active_filter}_{case_key}",
                ):
                    st.session_state["mt_selected_voucher"] = case_key
                    st.rerun()


def render_workspace_chat_panel(selected: dict[str, Any], latest_bundle: dict[str, Any]) -> None:
    result = ((latest_bundle.get("result") or {}).get("result") or {})
    timeline = latest_bundle.get("timeline") or []
    selected_vkey = selected.get("voucher_key") or ""
    render_panel_header(
        "에이전트 대화",
        "선택한 전표에 대해 LangGraph가 현재 무엇을 하고 있는지 실시간으로 보여줍니다.",
        trailing=f"분석 모델: {settings.reasoning_llm_label}",
    )

    vkey = selected_vkey
    is_unscreened = str(selected.get("case_type") or "").upper() == "UNSCREENED"
    run_id_for_hint = str(latest_bundle.get("run_id") or "")
    status_hint = st.session_state.get(f"mt_run_terminal_status_{run_id_for_hint}") if run_id_for_hint else None
    current_status = result.get("status") or status_hint or selected.get("case_status") or "-"
    current_severity = result.get("severity") or selected.get("severity")
    live_run_id = latest_bundle.get("run_id") or "-"
    strip_text = (
        "분석 시작 시 자동으로 케이스 유형을 분류합니다."
        if is_unscreened
        else f"스크리닝 완료 · {case_type_display_name(selected.get('case_type'))} · 심각도 {severity_display_name(selected.get('severity'))}"
    )
    # 카드 영역 2개 주석 처리(향후 제거 판단 시 소스에서 제거 예정). strip_text만 표시.
    # summary_html = f"""
    # <div class="mt-workspace-summary">
    #   <div class="mt-workspace-hero">
    #     <div>{status_badge(result.get("status") if result else selected.get("case_status"))}{severity_badge(result.get("severity") if result else selected.get("severity"))}{case_type_badge(selected.get("case_type"))}</div>
    #     <div class="mt-workspace-hero-title">{selected.get("demo_name") or selected.get("merchant_name") or "선택 전표"}</div>
    #     <div class="mt-workspace-hero-sub">실시간 스트림은 planning, tool 실행, 검증 게이트, HITL 요청, 최종 결론까지 공개 가능한 이벤트만 표시합니다.</div>
    #     <div class="mt-workspace-inline-meta">
    #       <div class="mt-workspace-inline-item"><span class="mt-workspace-inline-label">전표키</span>{selected.get("voucher_key") or "-"}</div>
    #       <div class="mt-workspace-inline-item"><span class="mt-workspace-inline-label">가맹점</span>{selected.get("merchant_name") or "-"}</div>
    #       <div class="mt-workspace-inline-item"><span class="mt-workspace-inline-label">금액</span>{fmt_num(selected.get('amount'))} {selected.get('currency') or ''}</div>
    #       <div class="mt-workspace-inline-item"><span class="mt-workspace-inline-label">발생일시</span>{fmt_dt(selected.get("occurred_at")) or "-"}</div>
    #     </div>
    #     <div class="mt-workspace-strip">{strip_text}</div>
    #   </div>
    #   <div class="mt-workspace-action">
    #     <div>
    #       <div class="mt-workspace-action-title">실행 상태</div>
    #       <div class="mt-workspace-action-top">이 영역은 현재 선택한 전표의 최신 run 상태와 다음 액션을 한 번에 제시합니다.</div>
    #       <div class="mt-workspace-action-meta">
    #         <div class="mt-workspace-action-key">현재 상태</div><div class="mt-workspace-action-value">{status_display_name(current_status)}</div>
    #         <div class="mt-workspace-action-key">심각도</div><div class="mt-workspace-action-value">{severity_display_name(current_severity)}</div>
    #         <div class="mt-workspace-action-key">실행 run</div><div class="mt-workspace-action-value">{str(live_run_id)[:12] + "…" if isinstance(live_run_id, str) and len(live_run_id) > 12 else live_run_id}</div>
    #         <div class="mt-workspace-action-key">다음 액션</div><div class="mt-workspace-action-value">분석 시작 또는 검토 재개</div>
    #       </div>
    #     </div>
    #   </div>
    # </div>
    # """
    # st.markdown(summary_html, unsafe_allow_html=True)
    # strip_text는 좌측, 분석 시작은 우측 끝에 배치
    # 주의: render_workspace_chat_panel는 상위 컬럼 내부에서 호출되므로 중첩 columns는 1단계까지만 허용됨.
    pending_stream: dict[str, str] | None = None
    enable_hitl = True
    with stylable_container(
        key=f"workspace_chat_cta_row_{vkey}",
        css_styles=[
            """{padding: 0; margin: 0;}""",
            """> [data-testid="stHorizontalBlock"] {align-items: center !important; min-height: 0 !important;}""",
            """> [data-testid="stHorizontalBlock"] > [data-testid="column"] {min-height: 0 !important; align-self: center !important;}""",
            """> [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {min-height: 0 !important; align-self: center !important;}""",
        ],
    ):
        strip_col, cta_btn_col = st.columns([0.72, 0.28])
        with strip_col:
            st.markdown(
                f'<div class="mt-workspace-strip-inline-wrap"><div class="mt-workspace-strip-inline">{strip_text}</div></div>',
                unsafe_allow_html=True,
            )
        with cta_btn_col:
            with stylable_container(
                key="workspace_cta_right_1",
                css_styles=[
                    """{}""",
                    """> [data-testid="stElementContainer"] {margin: 0 !important; display:flex !important; justify-content:flex-end !important; align-items:center !important;}""",
                    """> [data-testid="stElementContainer"] [data-testid="stButton"] {margin: 0 !important; width:auto !important;}""",
                    """> [data-testid="stElementContainer"] [data-testid="stButton"] > button {margin: 0 0 0 auto !important;}""",
                ],
            ):
                run_clicked = st.button("분석 시작", key=f"workspace_run_{vkey}", type="primary")
    if run_clicked:
        # 분석 시작 시 스트림/타임라인 패널을 자동으로 펼침
        st.session_state[f"agent_stream_exp_{vkey}"] = True
        response = post(f"/api/v1/cases/{vkey}/analysis-runs", json_body={"enable_hitl": enable_hitl})
        run_id = response["run_id"]
        st.session_state.pop(f"mt_run_terminal_status_{run_id}", None)
        # HITL은 항상 활성화하여 run 단위 UI 노출 여부도 True로 고정한다.
        st.session_state[f"mt_hitl_ui_enabled_{run_id}"] = True
        st.session_state.pop(_hitl_state_key("dismissed", run_id), None)
        st.session_state.pop(_hitl_state_key("open", run_id), None)
        st.session_state.pop(_hitl_state_key("shown", run_id), None)
        st.success(f"분석 시작: run_id={response['run_id']}")
        pending_stream = {"run_id": str(run_id), "stream_path": str(response["stream_path"])}
        st.session_state.pop("mt_current_stream_is_resume", None)  # 새 분석 시작이므로 재개 스트림 아님

    # 모달 닫힘을 먼저 반영하기 위한 2-step 재개:
    # review-submit 성공 직후에는 queued 키에 저장하고 rerun,
    # 다음 run에서 resume_stream으로 승격 후 다시 rerun하여 그 다음 run에서 SSE를 시작한다.
    queued_resume = st.session_state.get("mt_resume_stream_queued")
    if (
        queued_resume
        and (queued_resume.get("voucher_key") in {None, "", selected_vkey})
        and queued_resume.get("stream_path")
        and queued_resume.get("run_id")
    ):
        st.session_state["mt_resume_stream"] = {
            "run_id": str(queued_resume["run_id"]),
            "stream_path": str(queued_resume["stream_path"]),
            "voucher_key": queued_resume.get("voucher_key"),
        }
        st.session_state.pop("mt_resume_stream_queued", None)
        logger.info("[ui] queued resume 승격 run_id=%s — 모달 닫힘 반영 후 다음 rerun에서 SSE 시작", queued_resume.get("run_id"))
        st.rerun()

    resume_stream = st.session_state.get("mt_resume_stream")
    if (
        resume_stream
        and (resume_stream.get("voucher_key") in {None, "", selected_vkey})
        and resume_stream.get("stream_path")
        and resume_stream.get("run_id")
    ):
        run_id = str(resume_stream["run_id"])
        stream_path = str(resume_stream["stream_path"])
        st.session_state.pop("mt_resume_stream", None)
        st.success(f"HITL 응답 반영 후 재개: run_id={run_id}")
        pending_stream = {"run_id": run_id, "stream_path": stream_path}
        st.session_state["mt_current_stream_is_resume"] = run_id  # 재개 스트림 종료 시 팝업 자동 오픈 방지

    # HITL 팝업에서 "분석 이어가기" 제출 직후 — 2단계로 분리해 팝업을 즉시 닫음:
    # 1) mt_pending_review_submit이 있으면 API 호출 없이 open=False, skip 설정만 하고 rerun 하지 않음
    #    → 이 run이 끝까지 실행되어 "다이얼로그 미호출" 한 프레임이 그려져야 st.dialog 모달이 실제로 닫힘
    # 2) 다이얼로그 블록 직후 mt_review_submit_api_pending이 있으면 그때 st.rerun() 해서 다음 run에서 API 호출
    # 3) 다음 run에서 review-submit API 호출 → queued 저장 후 rerun
    # 4) queued -> resume 승격 rerun, 그 다음 run에서 SSE 시작
    logger.info("[HITL_CLOSE] run 시작: mt_pending_review_submit=%s mt_review_submit_api_pending=%s", bool(st.session_state.get("mt_pending_review_submit")), bool(st.session_state.get("mt_review_submit_api_pending")))
    pending_review_submit = st.session_state.get("mt_pending_review_submit")
    just_set_api_pending = False
    if pending_review_submit and (
        pending_review_submit.get("voucher_key") in {None, "", selected_vkey}
        or str(pending_review_submit.get("run_id") or "") == str(latest_bundle.get("run_id") or "")
    ):
        _run_id = str(pending_review_submit.get("run_id") or "")
        logger.info("[HITL_CLOSE] 1단계 진입: run_id=%s open=False, skip 설정 (이 run에서 한 프레임 그린 뒤 다이얼로그 블록 뒤에서 rerun)", _run_id)
        st.session_state[_hitl_state_key("open", _run_id)] = False
        st.session_state["mt_skip_hitl_dialog_run_id"] = _run_id
        st.session_state["mt_review_submit_api_pending"] = dict(pending_review_submit)
        st.session_state.pop("mt_pending_review_submit", None)
        just_set_api_pending = True

    # 2단계: 팝업이 닫힌 다음 run에서만 review-submit API 호출 (같은 run에서 1단계로 세팅한 직후에는 호출하면 안 됨)
    api_pending = st.session_state.get("mt_review_submit_api_pending")
    if api_pending and not just_set_api_pending and api_pending.get("voucher_key") in {None, "", selected_vkey}:
        _run_id = str(api_pending.get("run_id") or "")
        _payload = api_pending.get("payload") or {}
        _hr = _payload.get("hitl_response") or {}
        _cmt = str(_hr.get("comment") or "")
        _prev = (_cmt[:80] + "…") if len(_cmt) > 80 else _cmt
        logger.info(
            "[RESUME_TRACE] UI 2단계 review-submit 직전: run_id=%s payload_keys=%s approved=%s comment_len=%s comment_preview=%s evidence_uploaded=%s",
            _run_id, list(_payload.keys()) if isinstance(_payload, dict) else [], _hr.get("approved"), len(_cmt), _prev or "(없음)", _payload.get("evidence_uploaded"),
        )
        logger.info("[HITL_CLOSE] 2단계 진입: run_id=%s review-submit API 호출", _run_id)
        logger.info(
            "[RESUME_TRACE] UI → review-submit run_id=%s (백엔드에서 base_status=HITL_REQUIRED면 checkpoint 재개, 아니면 스크리닝부터 재실행)",
            _run_id,
        )
        st.session_state.pop("mt_review_submit_api_pending", None)
        try:
            response = post(f"/api/v1/analysis-runs/{_run_id}/review-submit", json_body=_payload)
            stream_path = response.get("stream_path")
            logger.info(
                "[RESUME_TRACE] UI review-submit 응답: run_id=%s stream_path=%s response_keys=%s",
                _run_id, stream_path, list(response.keys()) if isinstance(response, dict) else [],
            )
            if stream_path:
                st.session_state["mt_resume_stream_queued"] = {
                    "run_id": _run_id,
                    "stream_path": stream_path,
                    "voucher_key": selected_vkey,
                }
                logger.info("[ui] review-submit 성공 run_id=%s stream_path=%s — queued 저장 후 rerun", _run_id, stream_path)
                st.rerun()
            else:
                logger.info("[ui] review-submit 응답에 stream_path 없음 run_id=%s", _run_id)
                st.success("검토 반영이 제출되었습니다.")
        except Exception as e:
            logger.exception("[ui] review-submit API 실패 run_id=%s", _run_id)
            st.error(f"제출 실패: {e}")
            st.session_state[_hitl_state_key("open", _run_id)] = True

    skip_dialog_run_id = st.session_state.pop("mt_skip_hitl_dialog_run_id", None)
    if skip_dialog_run_id is not None:
        logger.info("[HITL_CLOSE] skip_dialog_run_id popped = %s (이 run_id에 대해서는 다이얼로그 미렌더)", skip_dialog_run_id)

    # HITL 확인 체크 해제 시: 해당 run은 검토 팝업/배너를 자동 노출하지 않는다.
    ui_run_id = str(latest_bundle.get("run_id") or "")
    hitl_ui_enabled = st.session_state.get(f"mt_hitl_ui_enabled_{ui_run_id}", True) if ui_run_id else True

    # 검토 상태면(HITL 체크와 무관) 통합 검토 배너 + 단일 다이얼로그를 노출한다.
    review_statuses = {"REVIEW_REQUIRED", "EVIDENCE_REJECTED", "IN_REVIEW", "HOLD_AFTER_HITL", "REVIEW_AFTER_HITL"}
    _is_review_state = str(current_status or "").upper() in review_statuses
    _need_hitl = _has_pending_hitl(latest_bundle)
    _need_review_ui = _is_review_state and bool(latest_bundle.get("run_id"))
    if _need_review_ui or (hitl_ui_enabled and _need_hitl):
        hitl_msg_col, hitl_btn_col = st.columns([0.72, 0.28])
        run_id = latest_bundle.get("run_id")
        open_key = _hitl_state_key("open", run_id)
        dismissed_key = _hitl_state_key("dismissed", run_id)
        with hitl_msg_col:
            hitl_req = latest_bundle.get("hitl_request") or {}
            why = (hitl_req.get("why_hitl") or hitl_req.get("blocking_reason") or "").strip()
            if why and _need_hitl:
                banner_text = f"검토 필요: {why[:100]}{'…' if len(why) > 100 else ''} 확인 후 이어서 진행하세요."
            else:
                banner_text = "검토 필요 상태입니다. 확인 후 이어서 진행하세요."
            st.markdown(
                f'<div class="mt-hitl-banner">{banner_text}</div>',
                unsafe_allow_html=True,
            )
        with hitl_btn_col:
            with stylable_container(
                key="workspace_cta_right_2",
                css_styles=[
                    """{}""",
                    """> [data-testid="stElementContainer"] {margin: 0 !important; display:flex !important; justify-content:flex-end !important; align-items:center !important;}""",
                    """> [data-testid="stElementContainer"] [data-testid="stButton"] {margin: 0 !important; width:auto !important;}""",
                    """> [data-testid="stElementContainer"] [data-testid="stButton"] > button {margin: 0 0 0 auto !important;}""",
                ],
            ):
                if st.button("HITL 팝업 열기", key=f"workspace_hitl_open_{vkey}"):
                    st.session_state[dismissed_key] = False
                    st.session_state[open_key] = True
                    st.rerun()
        if run_id and str(run_id) != str(skip_dialog_run_id):
            st.session_state.setdefault(dismissed_key, False)
            open_val = st.session_state.get(open_key)
            logger.info("[HITL_CLOSE] 다이얼로그 분기: run_id=%s skip_dialog_run_id=%s open_key=%s", run_id, skip_dialog_run_id, open_val)
            if open_val:
                st.session_state[open_key] = False
                logger.info("[HITL_CLOSE] 다이얼로그 렌더 함 run_id=%s", run_id)
                render_hitl_dialog(latest_bundle, vkey=vkey)
            else:
                logger.info("[HITL_CLOSE] 다이얼로그 미렌더 run_id=%s (open_key=False)", run_id)
        elif run_id and str(run_id) == str(skip_dialog_run_id):
            logger.info("[HITL_CLOSE] 다이얼로그 스킵 run_id=%s (skip과 동일 — 분석 이어가기 직후)", run_id)

    # 1단계 직후: "다이얼로그 미호출" 한 프레임을 브라우저에 보냈으면 rerun 하지 않음(just_set_api_pending).
    # api_pending만 있고 방금 세팅한 run이 아니면 rerun.
    if st.session_state.get("mt_review_submit_api_pending") and not just_set_api_pending:
        logger.info("[HITL_CLOSE] api_pending 있음 → st.rerun() (다음 run에서 2단계 API 호출)")
        st.rerun()

    # 팝업 방금 닫은 run(just_set_api_pending): fragment 대신 "한 번 더 클릭"으로 2단계 트리거 (fragment 제거 시 run_every 오류 무한 반복 방지)
    if st.session_state.get("mt_review_submit_api_pending"):
        _pending_run_id = (st.session_state.get("mt_review_submit_api_pending") or {}).get("run_id") or ""
        _msg_col, _btn_col = st.columns([0.72, 0.28])
        with _msg_col:
            st.info("검토가 반영되었습니다. 오른쪽 버튼을 눌러 분석을 이어가세요.")
        with _btn_col:
            with stylable_container(
                key="workspace_cta_right_3",
                css_styles=[
                    """{}""",
                    """> [data-testid="stElementContainer"] {margin: 0 !important; display:flex !important; justify-content:flex-end !important; align-items:center !important;}""",
                    """> [data-testid="stElementContainer"] [data-testid="stButton"] {margin: 0 !important; width:auto !important;}""",
                    """> [data-testid="stElementContainer"] [data-testid="stButton"] > button {margin: 0 0 0 auto !important;}""",
                ],
            ):
                if st.button("분석 이어가기 실행", key=f"hitl_resume_trigger_{_pending_run_id}_{selected_vkey or ''}"):
                    st.rerun()

    # 증빙 확정(완료 반영) 직후 rerun 시 성공 메시지 표시
    evidence_done = st.session_state.pop("mt_evidence_resume_done", None)
    if evidence_done and evidence_done.get("vkey") == selected_vkey:
        status_label = "증빙 검증 통과로 완료" if evidence_done.get("passed") else "증빙 불일치로 반려"
        st.success(f"증빙 확정이 반영되었습니다. 케이스가 **{status_label}** 처리되었습니다.")

    # 증빙 업로드·완료 반영은 위 통합 배너에서 "HITL 팝업 열기"로 열리는 다이얼로그 내에서만 표시

    # 분석 시작 버튼 아래 영역(실시간 스트림/타임라인)을 접었다 펼 수 있도록 expander로 감싼다.
    # 스트리밍 중에는 기본으로 펼쳐진 상태 유지. (st.expander는 key 미지원·내부에 다른 expander 불가)
    latest_run_id = str(latest_bundle.get("run_id") or "")
    cached_stream_text = st.session_state.get(f"mt_last_stream_content_{latest_run_id}", "") if latest_run_id else ""
    _stream_expanded_default = True if pending_stream else bool(cached_stream_text)
    with st.expander(
        "실시간 스트림/타임라인 보기",
        expanded=_stream_expanded_default,
    ):
        # 고정 높이 컨테이너 + 테두리 — stylable_container로 안정적으로 적용
        _STREAM_PANEL_HEIGHT = 300
        with stylable_container(
            key=f"stream_border_{selected_vkey}",
            css_styles=[
                """
                {
                    border: 1px solid #e5e7eb !important;
                    border-radius: 18px !important;
                    background: #f8fafc !important;
                    padding: 0 !important;
                    overflow: hidden !important;
                }
                """,
                """
                > div {
                    background: transparent !important;
                }
                """,
                """
                > div > div {
                    background: transparent !important;
                }
                """,
                """
                /* 스트림 텍스트 첫 글자가 라운드 경계에 걸리지 않도록 좌우 내부 여백 보정 */
                [data-testid="stMarkdownContainer"] {
                    padding-left: 10px !important;
                    padding-right: 10px !important;
                    box-sizing: border-box !important;
                }
                """,
            ],
        ):
            stream_container = st.container(height=_STREAM_PANEL_HEIGHT, border=False)
        # 스트리밍 시작 전에 자동 스크롤 스크립트를 먼저 주입한다.
        # (기존에는 스트림 종료 후 주입되어 실시간 타이핑 구간에서 추적이 늦었다)
        _stream_auto_scroll_script = """
        <script>
        (function() {
            var doc = window.parent && window.parent.document ? window.parent.document : document;
            var panels = doc.querySelectorAll('[class*="st-key-stream_border_"]');
            if (!panels || !panels.length) return;
            var panel = panels[panels.length - 1];

            function isScrollable(el) {
                if (!el) return false;
                try {
                    var s = window.getComputedStyle(el);
                    var oy = (s && s.overflowY) ? s.overflowY : '';
                    return (oy === 'auto' || oy === 'scroll');
                } catch (e) { return false; }
            }

            function findScrollEl(root) {
                if (!root) return null;
                var nodes = [root].concat(Array.from(root.querySelectorAll('*')));
                for (var i = 0; i < nodes.length; i++) {
                    var el = nodes[i];
                    try {
                        // 1) 실제 스크롤 컨테이너 우선
                        if (isScrollable(el) && el.clientHeight > 0) {
                            return el;
                        }
                        // 2) overflow 스타일이 없어도 높이 차가 있는 경우(브라우저별 렌더링 차이) 보조 선택
                        if (el.scrollHeight > el.clientHeight + 2 && el.clientHeight > 0) {
                            return el;
                        }
                    } catch (e) {}
                }
                return root;
            }

            function scrollToBottom() {
                var scrollEl = findScrollEl(panel);
                if (!scrollEl) return;
                var target = Math.max(0, scrollEl.scrollHeight - scrollEl.clientHeight);
                if (scrollEl.scrollTop !== target) scrollEl.scrollTop = target;
            }

            scrollToBottom();
            var obs = new MutationObserver(function() { scrollToBottom(); });
            obs.observe(panel, { childList: true, subtree: true, characterData: true });
            var timer = setInterval(scrollToBottom, 120);
            setTimeout(function() {
                try { obs.disconnect(); } catch (e) {}
                try { clearInterval(timer); } catch (e) {}
            }, 300000);
        })();
        </script>
        """
        try:
            import streamlit.components.v1 as components
            components.html(_stream_auto_scroll_script, height=0)
        except Exception:
            pass
        with stream_container:
            stream_placeholder = st.empty()
            stream_wait_placeholder = st.empty()
            if pending_stream:
                stream_url = f"{API}{pending_stream['stream_path']}"
                run_id = pending_stream["run_id"]
                logger.info("[ui] 스트림 구독 시작 vkey=%s run_id=%s url=%s", vkey, run_id, stream_url)
                logger.info("[RESUME_TRACE] UI SSE 스트림 구독 시작 (분석 이어가기 후 재개 스트림일 수 있음) vkey=%s run_id=%s", vkey, run_id)
                for stream_ev in sse_node_block_generator_with_idle(stream_url, run_id=run_id, idle_after_sec=1.0):
                    ev_type = stream_ev.get("type")
                    if ev_type == "block":
                        stream_wait_placeholder.empty()
                        stream_placeholder.write_stream(
                            _prefix_with_typed_append(
                                str(stream_ev.get("prefix") or ""),
                                str(stream_ev.get("addition") or ""),
                            )
                        )
                    elif ev_type == "idle":
                        _render_stream_waiting_indicator(
                            stream_wait_placeholder,
                            str(stream_ev.get("status_text") or ""),
                            node_name=str(stream_ev.get("node_name") or "agent"),
                        )
                    elif ev_type == "error":
                        msg = str(stream_ev.get("message") or "스트림 연결 중 오류가 발생했습니다.")
                        logger.warning("[ui] SSE worker error run_id=%s msg=%s", run_id, msg)
                        st.warning(f"실시간 스트림 연결 오류: {msg}")
                stream_wait_placeholder.empty()
                logger.info("[ui] 스트림 for 루프 종료 vkey=%s — fetch_case_bundle 후 mt_post_stream_bundle 저장 및 st.rerun() 예정", vkey)
                try:
                    # 재개 스트림 여부는 스트림 종료 시 한 번만 사용 후 제거 (다음 run에 남기지 않음)
                    _stream_was_resume = st.session_state.pop("mt_current_stream_is_resume", None)
                    latest_bundle = fetch_case_bundle(vkey)
                    result = ((latest_bundle.get("result") or {}).get("result") or {})
                    timeline = latest_bundle.get("timeline") or []
                    latest_run_id = str(latest_bundle.get("run_id") or "")
                    has_result = bool((latest_bundle.get("result") or {}).get("result"))
                    logger.info(
                        "[ui] fetch_case_bundle 완료 vkey=%s run_id=%s has_result=%s result.status=%s",
                        vkey,
                        latest_run_id,
                        has_result,
                        result.get("status"),
                    )
                    cached_stream_text = st.session_state.get(f"mt_last_stream_content_{latest_run_id}", "") if latest_run_id else ""
                    # 스트림 종료 직후 가져온 번들을 rerun 후 판단요약 탭이 반드시 그 데이터로 그려지도록 세션에 보관
                    st.session_state["mt_post_stream_bundle"] = {"voucher_key": vkey, "bundle": latest_bundle}
                    logger.info("[ui] mt_post_stream_bundle 저장 vkey=%s — rerun 시 판단요약 탭이 이 번들로 그려짐", vkey)
                    if _has_pending_hitl(latest_bundle) and latest_bundle.get("run_id"):
                        _rid = latest_bundle.get("run_id")
                        st.session_state[_hitl_state_key("dismissed", _rid)] = False
                        # 재개 스트림이 끝난 경우에는 open_key 설정하지 않음 (이미 제출 후 재개한 것이므로 팝업 다시 띄우지 않음)
                        if _stream_was_resume == run_id:
                            logger.info("[ui] 재개 스트림 종료 run_id=%s → open_key 설정 생략 (팝업 재오픈 방지)", run_id)
                        else:
                            # HITL은 항상 활성화 상태이므로 최초 인터럽트 시 팝업을 자동 오픈한다.
                            if st.session_state.get(f"mt_hitl_ui_enabled_{_rid}", True):
                                st.session_state[_hitl_state_key("open", _rid)] = True
                                logger.info("[ui] HITL 인터럽트 + mt_hitl_ui_enabled → open_key=True 설정 run_id=%s", _rid)
                except Exception as e:
                    # fetch 실패 시에도 rerun은 수행해 기존 화면을 갱신
                    logger.exception("[ui] fetch_case_bundle 실패 vkey=%s — mt_post_stream_bundle 미저장, rerun만 수행", vkey)
                    st.session_state.pop("mt_post_stream_bundle", None)
                finally:
                    # 스트림 종료 후 항상 rerun하여 판단요약·근거맵·실행내역 등 탭이 최신 결과로 갱신되도록 함
                    logger.info("[ui] 스트림 종료 후 st.rerun() 호출 직전 vkey=%s", vkey)
                    st.rerun()
            elif cached_stream_text:
                stream_placeholder.markdown(cached_stream_text)
                stream_wait_placeholder.empty()
            else:
                stream_placeholder.markdown("분석을 시작하면 이 영역에 실시간 스트림이 표시됩니다.")
                stream_wait_placeholder.empty()

        ag = [e for e in timeline if e.get("event_type") == "AGENT_EVENT"]
        if ag:
            with st.container():
                st.caption("이전 타임라인 카드 보기")
                render_timeline_cards(ag, nested_under_expander=True)


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
            if (meta.get("tool") or meta.get("skill")) == "policy_rulebook_probe":
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
    raw_status = result.get("status")
    status_label = status_display_name(raw_status)
    hero_title = "분석 실패" if failed else (raw_status or "결과 없음")
    if not failed and raw_status and status_label != str(raw_status):
        hero_title = f"{raw_status} ({status_label})"
    elif not failed and not raw_status:
        hero_title = "결과 없음"
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
            <div class="mt-result-metric-value">{"FAILED" if failed else (status_label or "-")}</div>
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
    sb = result.get("score_breakdown") if isinstance(result.get("score_breakdown"), dict) else {}
    if not sb:
        sb = _extract_score_breakdown_from_timeline(timeline)
    if sb:
        st.caption(f"정책점수 {sb.get('policy_score', '-')} · 근거점수 {sb.get('evidence_score', '-')} · 최종점수 {sb.get('final_score', '-')}")
        render_score_breakdown_detail(sb)
    if result.get("hitl_request"):
        st.markdown("#### 담당자 검토 상태")
        st.caption("이 run은 자동 확정이 아니라 담당자 검토 이후 재개를 전제로 진행되었습니다.")
    if result.get("critique") and debug_mode:
        st.markdown("#### 검증 메모 (debug)")
        st.json(result.get("critique"))


_UNSUPPORTED_TAXONOMY_LABELS: dict[str, str] = {
    "no_citation": "근거 없음",
    "weak_citation": "약한 근거",
    "wrong_scope_citation": "범위 불일치",
    "contradictory_evidence": "증거 충돌",
    "missing_mandatory_evidence": "필수 증빙 누락",
    "low_retrieval_confidence": "검색 신뢰도 낮음",
}


def _taxonomy_label(taxonomy: str) -> str:
    token = str(taxonomy or "").strip().lower()
    return _UNSUPPORTED_TAXONOMY_LABELS.get(token, token or "unknown")


def _claim_display_label(claim_text: str, supporting_articles: list[str] | None = None) -> str:
    """검증 주장 원문을 사용자 친화 라벨로 축약한다(검증 로직과 분리)."""
    text = str(claim_text or "").strip()
    compact = text.replace(" ", "").lower()
    supporting = [str(v).strip() for v in (supporting_articles or []) if str(v).strip()]
    article_hint = f" ({', '.join(supporting[:2])})" if supporting else ""

    if "policy_rulebook_probe" in compact:
        return f"정책 검색 채택 조항 검증{article_hint}"
    if "심야" in text or "23:00" in text or "06:00" in text:
        return f"심야 시간대 규정 적용 검증{article_hint}"
    if "휴일" in text or "주말" in text or "근태" in text or "LEAVE" in text:
        return f"휴일/근태 충돌 규정 적용 검증{article_hint}"
    if "MCC" in text or "업종" in text:
        return f"업종 위험도 규정 적용 검증{article_hint}"
    if "예산" in text or "한도" in text:
        return f"예산/한도 규정 적용 검증{article_hint}"
    if "승인" in text:
        return f"승인 기준 규정 적용 검증{article_hint}"
    return f"규정 적용 검증{article_hint}"


def _claim_user_facing_text(claim_text: str, supporting_articles: list[str] | None = None) -> str:
    """원문 claim을 사용자 관점 설명 문구로 정리한다."""
    text = str(claim_text or "").strip()
    if not text:
        return "-"
    supporting = [str(v).strip() for v in (supporting_articles or []) if str(v).strip()]

    # 내부 템플릿 문구를 사용자용 문장으로 치환
    if "policy_rulebook_probe 채택 조항" in text:
        extracted_articles = re.findall(r"제\s*\d+\s*조", text)
        article_candidates = supporting or extracted_articles
        dedup_articles: list[str] = []
        seen: set[str] = set()
        for raw in article_candidates:
            norm = re.sub(r"\s+", "", str(raw))
            if norm and norm not in seen:
                seen.add(norm)
                dedup_articles.append(norm)
        if dedup_articles:
            joined = ", ".join(dedup_articles[:2])
            return f"정책 검색에서 채택된 {joined} 조항이 이 전표 판단 근거로 연결되었습니다."
        return "정책 검색에서 채택된 조항이 이 전표 판단 근거로 연결되었습니다."

    # 과도한 내부 표현 정리
    cleaned = text.replace("직접 적용 가능한 위반 근거를 갖는다.", "판단 근거로 확인되었습니다.")
    cleaned = cleaned.replace("policy_rulebook_probe", "정책 검색")
    return cleaned


def _extract_article_token(text: str) -> str:
    m = re.search(r"제\s*(\d+)\s*조", str(text or ""))
    return f"제{m.group(1)}조" if m else ""


def _policy_label_compact(text: str) -> str:
    token = _extract_article_token(text)
    if token:
        return token
    return str(text or "").strip()[:24]


def _graph_claim_label(claim_text: str, supporting_articles: list[str] | None = None) -> str:
    label = _claim_display_label(claim_text, supporting_articles)
    return label.replace("규정 적용 검증", "규정 검증").strip()


def _normalize_unsupported_claims(raw_claims: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in (raw_claims or []):
        item = raw if isinstance(raw, dict) else (raw.model_dump() if hasattr(raw, "model_dump") else {})
        if not isinstance(item, dict):
            continue
        taxonomy = str(item.get("taxonomy") or "").strip().lower()
        claim = str(item.get("claim") or "").strip()
        reason = str(item.get("reason") or "").strip()
        severity = str(item.get("severity") or "MEDIUM").strip().upper()
        supporting = [str(v).strip() for v in (item.get("supporting_articles") or []) if str(v).strip()]
        key = (taxonomy, claim, reason, severity)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "taxonomy": taxonomy,
                "taxonomy_label": _taxonomy_label(taxonomy),
                "claim": claim,
                "reason": reason,
                "severity": severity,
                "covered": bool(item.get("covered")),
                "citation_count": int(item.get("citation_count") or 0),
                "supporting_articles": supporting,
            }
        )
    return out


def _extract_unsupported_claims_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    verifier_output = result.get("verifier_output") or {}
    review_audit = result.get("review_audit") or verifier_output.get("review_audit") or {}
    hitl_request = result.get("hitl_request") or {}
    raw_claims: list[Any] = []
    raw_claims.extend(verifier_output.get("unsupported_claims") or [])
    raw_claims.extend(review_audit.get("unsupported_claims") or [])
    raw_claims.extend(hitl_request.get("unsupported_claims") or [])
    return _normalize_unsupported_claims(raw_claims)


def _render_unsupported_claims_panel(result: dict[str, Any]) -> None:
    unsupported_claims = _extract_unsupported_claims_from_result(result)
    if not unsupported_claims:
        return

    taxonomy_counts: dict[str, int] = defaultdict(int)
    for item in unsupported_claims:
        taxonomy_counts[item["taxonomy_label"]] += 1

    badges_html = "".join(
        f'<span class="mt-badge mt-badge-red">{html.escape(label)} {count}</span>'
        for label, count in sorted(taxonomy_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    st.markdown(
        f'<div class="mt-section-inline"><span class="mt-section-inline-title">Unsupported claim</span> <span class="mt-section-inline-content">{badges_html}</span></div>',
        unsafe_allow_html=True,
    )

    with st.expander("Unsupported claim taxonomy 보기", expanded=False):
        st.caption("리뷰 단계에서 감지된 근거 부족/범위 불일치/필수 증빙 누락 유형입니다.")
        for i, item in enumerate(unsupported_claims, start=1):
            sev = str(item.get("severity") or "MEDIUM").upper()
            sev_badge = "mt-badge-red" if sev in {"HIGH", "CRITICAL"} else ("mt-badge-amber" if sev == "MEDIUM" else "mt-badge-blue")
            covered_badge = "mt-badge-green" if item.get("covered") else "mt-badge-amber"
            st.markdown(
                (
                    f"**{i}. {item.get('claim') or '(주장 없음)'}**  \n"
                    f'<span class="mt-badge mt-badge-red">{html.escape(item.get("taxonomy_label") or "-")}</span> '
                    f'<span class="mt-badge {sev_badge}">{html.escape(sev)}</span> '
                    f'<span class="mt-badge {covered_badge}">citation {int(item.get("citation_count") or 0)}건</span>'
                ),
                unsafe_allow_html=True,
            )
            if item.get("reason"):
                st.caption(f"사유: {item['reason']}")
            supporting = item.get("supporting_articles") or []
            if supporting:
                st.caption("연결 조항: " + ", ".join(supporting[:6]))
            if i < len(unsupported_claims):
                st.markdown("<hr style='margin:0.35rem 0 0.55rem 0; border:none; border-top:1px solid #e5e7eb;'>", unsafe_allow_html=True)


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
                if (meta.get("tool") or meta.get("skill")) == "policy_rulebook_probe":
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
            with stylable_container(
                key=f"process_story_evidence_ref_{latest_bundle.get('run_id') or 'none'}_{idx}",
                css_styles="""{
                    padding: 0.2rem 0.15rem;
                    border-radius: 12px;
                }""",
            ):
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
        st.caption("검증 대상 **주장(문장)** 중 규정 청크와 연결된 비율입니다. 규정 개수(C1,C2,C3…)와 별개로, ‘검증할 문장 몇 개 중 몇 개가 근거와 연결됐는지’를 나타냅니다. **높을수록** 자동 확정 가능, 낮으면 담당자 검토가 필요합니다.")
        v1, v2, v3 = st.columns(3)
        with v1:
            ratio = verification_summary.get("coverage_ratio")
            covered = verification_summary.get("covered", 0)
            total = verification_summary.get("total", 0)
            st.metric("검증 주장 근거 연결률", f"{(ratio or 0) * 100:.0f}%" if ratio is not None else "-", f"주장 {covered}/{total}건 연결")
        with v2:
            missing = verification_summary.get("missing_citations") or []
            st.metric("근거 미연결 주장 수", str(len(missing)))
        with v3:
            gate = verification_summary.get("gate_policy") or "-"
            gate_label = {"hold": "보류", "caution": "주의", "regenerate_citations": "인용 보완 유도"}.get(str(gate).lower(), str(gate))
            st.metric("자동 확정 여부", gate_label, "담당자 검토 필요" if str(gate).lower() == "hold" else ("주의 검토" if str(gate).lower() == "caution" else None))

    # 검증 주장 ↔ 규정 매핑: 어떤 주장이 어떤 규정 조항으로 뒷받침됐는지 표시
    verifier_output = result.get("verifier_output") or {}
    claim_results = verifier_output.get("claim_results") or []
    if claim_results:
        st.markdown("#### 검증 주장 ↔ 규정 매핑")
        st.caption("각 검증 주장이 어떤 규정 조항으로 연결되었는지 확인할 수 있습니다.")
        for i, cr in enumerate(claim_results, start=1):
            item = cr if isinstance(cr, dict) else (cr.model_dump() if hasattr(cr, "model_dump") else {})
            claim_text = (item.get("claim") or "").strip()
            is_covered = bool(item.get("covered"))
            supporting = item.get("supporting_articles") or []
            gap = (item.get("gap") or "").strip()
            icon = "✅" if is_covered else "❌"
            claim_label = _claim_display_label(claim_text, supporting)
            with st.expander(f"**{icon} 주장 {i}** — {claim_label}", expanded=(i == 1)):
                with stylable_container(
                    key=f"process_story_evidence_claim_{latest_bundle.get('run_id') or 'none'}_{i}",
                    css_styles="""{
                        padding: 0.2rem 0.15rem;
                        border-radius: 12px;
                    }""",
                ):
                    st.markdown("**검증 주장**")
                    display_text = str(item.get("display_text") or "").strip()
                    st.caption(display_text or _claim_user_facing_text(claim_text, supporting))
                    if is_covered and supporting:
                        st.markdown("**연결된 규정**  \n" + ", ".join(f"**{a}**" for a in supporting))
                    elif not is_covered and gap:
                        st.markdown("**미연결 사유**  \n" + gap)
                    elif not is_covered:
                        st.caption("규정 청크와 매칭되지 않았습니다.")

    if verification_summary.get("missing_citations"):
        with st.expander("근거 미연결 검증 주장", expanded=False):
            with stylable_container(
                key=f"process_story_evidence_missing_{latest_bundle.get('run_id') or 'none'}",
                css_styles="""{
                    padding: 0.2rem 0.15rem;
                    border-radius: 12px;
                }""",
            ):
                for i, s in enumerate(verification_summary["missing_citations"], 1):
                    st.caption(f"{i}. {(s or '')[:160]}{'…' if len(str(s or '')) > 160 else ''}")

    if retrieval_snapshot:
        with st.expander("Retrieval 인용 현황", expanded=False):
            with stylable_container(
                key=f"process_story_evidence_retrieval_{latest_bundle.get('run_id') or 'none'}",
                css_styles="""{
                    padding: 0.2rem 0.15rem;
                    border-radius: 12px;
                }""",
            ):
                candidates = retrieval_snapshot.get("candidates_after_rerank") or []
                adopted = retrieval_snapshot.get("adopted_citations") or []
                st.caption("after rerank 후보, 최종 보고서에 반영된 근거 조항, 채택 이유를 함께 표시합니다.")
                st.caption(f"후보 청크 {len(candidates)}건 · 채택 인용 {len(adopted)}건")
                if adopted:
                    st.markdown("**최종 보고서에 반영된 근거 조항**")
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

    st.markdown("#### 도구 실행 요약")
    render_tool_trace_summary(tool_results)

    if plan_steps:
        with st.expander("작업 계획", expanded=False):
            for step in plan_steps:
                header = f"**{step['order']}. {step['title']}**"
                with stylable_container(
                    key=f"plan_review_{latest_bundle.get('run_id')}_{step['order']}",
                    css_styles="""{background:#fff; border:1px solid #e5e7eb; border-radius:16px; padding:0.9rem 1rem; margin-bottom:0.6rem;}""",
                ):
                    left_step, right_step = st.columns([0.78, 0.22])
                    with left_step:
                        st.markdown(header)
                        st.caption(step["description"])
                    with right_step:
                        st.markdown(
                            status_badge(step["status"] if step["status"] != "진행중" else "IN_REVIEW"),
                            unsafe_allow_html=True,
                        )

    if exec_logs:
        with st.expander("주요 실행 이벤트", expanded=False):
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
            render_process_story(timeline, debug_mode=True, key_prefix="timeline_raw")


def render_workspace_review_history(latest_bundle: dict[str, Any]) -> None:
    render_panel_header("검토 이력", "HITL 요청, 담당자 검토 응답, 재개 이력을 run 단위로 확인합니다.")
    render_hitl_history(latest_bundle.get("history") or [])


def _render_explain_graph_plotly(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], *, key: str) -> None:
    if not nodes:
        render_empty_state("표시할 그래프 노드가 없습니다.")
        return

    type_order = {"Case": 0, "Run": 1, "Claim": 2, "Policy": 3}
    type_color = {
        "Case": "#2563eb",
        "Run": "#0ea5e9",
        "Claim": "#f59e0b",
        "Policy": "#16a34a",
    }

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        groups[str(n.get("type") or "Etc")].append(n)

    pos: dict[str, tuple[float, float]] = {}
    for t, arr in sorted(groups.items(), key=lambda kv: type_order.get(kv[0], 99)):
        x = float(type_order.get(t, 4))
        total = len(arr)
        for i, n in enumerate(arr):
            y = 0.0 if total == 1 else (i - (total - 1) / 2) * 1.2
            pos[str(n.get("id"))] = (x, y)

    edge_x: list[float] = []
    edge_y: list[float] = []
    for e in edges:
        f = str(e.get("from") or "")
        t = str(e.get("to") or "")
        if f not in pos or t not in pos:
            continue
        x0, y0 = pos[f]
        x1, y1 = pos[t]
        edge_x.extend([x0, x1, None])
        edge_y.extend([y0, y1, None])

    edge_trace = go.Scatter(
        x=edge_x,
        y=edge_y,
        mode="lines",
        line=dict(width=1.5, color="#94a3b8"),
        hoverinfo="none",
        showlegend=False,
    )

    policy_by_id: dict[str, dict[str, Any]] = {
        str(n.get("id") or ""): n for n in nodes if str(n.get("type") or "") == "Policy"
    }
    claim_support_articles: dict[str, list[str]] = {}
    for e in edges:
        if str(e.get("type") or "") != "SUPPORTED_BY":
            continue
        frm = str(e.get("from") or "")
        to = str(e.get("to") or "")
        if not frm or not to:
            continue
        p = policy_by_id.get(to) or {}
        token = _extract_article_token(str(p.get("label") or ""))
        if not token:
            continue
        claim_support_articles.setdefault(frm, [])
        if token not in claim_support_articles[frm]:
            claim_support_articles[frm].append(token)

    node_x: list[float] = []
    node_y: list[float] = []
    node_text: list[str] = []
    node_color: list[str] = []
    node_hover: list[str] = []
    for n in nodes:
        nid = str(n.get("id") or "")
        if nid not in pos:
            continue
        x, y = pos[nid]
        typ = str(n.get("type") or "Etc")
        raw_lbl = str(n.get("label") or nid)
        if typ == "Claim":
            lbl = _graph_claim_label(raw_lbl, claim_support_articles.get(nid) or [])
        elif typ == "Policy":
            lbl = _policy_label_compact(raw_lbl)
        elif typ == "Run":
            short = raw_lbl[:8] + "…" if len(raw_lbl) > 8 else raw_lbl
            lbl = f"실행 {short}"
        elif typ == "Case":
            lbl = f"케이스 {raw_lbl}"
        else:
            lbl = raw_lbl
        node_x.append(x)
        node_y.append(y)
        node_color.append(type_color.get(typ, "#64748b"))
        node_text.append(lbl)
        node_hover.append(f"[{typ}] {raw_lbl}")

    node_trace = go.Scatter(
        x=node_x,
        y=node_y,
        mode="markers+text",
        text=[t if len(t) <= 34 else t[:31] + "..." for t in node_text],
        textposition="top center",
        textfont=dict(size=12, color="#111827"),
        hovertext=node_hover,
        hoverinfo="text",
        marker=dict(size=16, color=node_color, line=dict(width=1, color="#0f172a")),
        showlegend=False,
    )

    fig = go.Figure(data=[edge_trace, node_trace])
    fig.update_layout(
        height=420,
        margin=dict(l=20, r=20, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
    )
    st.plotly_chart(fig, use_container_width=True, key=key)


def _render_related_graph_plotly(selected_key: str, items: list[dict[str, Any]], *, key: str) -> None:
    if not items:
        render_empty_state("연결 규칙에 매칭되는 연관 케이스가 없습니다.")
        return

    center_x, center_y = 0.0, 0.0
    radius = 2.5
    n = len(items)
    pos: dict[str, tuple[float, float]] = {selected_key: (center_x, center_y)}
    for idx, row in enumerate(items):
        theta = (2 * math.pi * idx) / max(n, 1)
        x = radius * math.cos(theta)
        y = radius * math.sin(theta)
        pos[str(row.get("voucher_key") or f"node-{idx}")] = (x, y)

    edge_x: list[float] = []
    edge_y: list[float] = []
    edge_text_x: list[float] = []
    edge_text_y: list[float] = []
    edge_text: list[str] = []
    for row in items:
        vk = str(row.get("voucher_key") or "")
        if vk not in pos:
            continue
        x0, y0 = pos[selected_key]
        x1, y1 = pos[vk]
        edge_x.extend([x0, x1, None])
        edge_y.extend([y0, y1, None])
        edge_text_x.append((x0 + x1) / 2)
        edge_text_y.append((y0 + y1) / 2)
        edge_text.append(f"{int(row.get('link_score') or 0)}")

    edge_trace = go.Scatter(
        x=edge_x,
        y=edge_y,
        mode="lines",
        line=dict(width=1.5, color="#94a3b8"),
        hoverinfo="none",
        showlegend=False,
    )
    edge_label_trace = go.Scatter(
        x=edge_text_x,
        y=edge_text_y,
        mode="text",
        text=edge_text,
        textfont=dict(size=11, color="#111827"),
        hoverinfo="none",
        showlegend=False,
    )

    node_x: list[float] = []
    node_y: list[float] = []
    node_text: list[str] = []
    node_size: list[int] = []
    node_color: list[str] = []
    for vk, (x, y) in pos.items():
        node_x.append(x)
        node_y.append(y)
        if vk == selected_key:
            node_text.append(f"[선택] {vk}")
            node_size.append(24)
            node_color.append("#2563eb")
        else:
            row = next((r for r in items if str(r.get("voucher_key") or "") == vk), {})
            node_text.append(
                f"{vk}<br>{case_type_display_name(row.get('case_type'))} · {status_display_name(row.get('status'))}"
            )
            node_size.append(16)
            node_color.append("#14b8a6")

    node_trace = go.Scatter(
        x=node_x,
        y=node_y,
        mode="markers+text",
        text=[t if len(t) <= 42 else t[:39] + "..." for t in node_text],
        textposition="top center",
        textfont=dict(size=12, color="#111827"),
        hovertext=node_text,
        hoverinfo="text",
        marker=dict(size=node_size, color=node_color, line=dict(width=1, color="#0f172a")),
        showlegend=False,
    )

    fig = go.Figure(data=[edge_trace, edge_label_trace, node_trace])
    fig.update_layout(
        height=420,
        margin=dict(l=20, r=20, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
    )
    st.plotly_chart(fig, use_container_width=True, key=key)


def render_workspace_graph_insights(selected_key: str | None, latest_bundle: dict[str, Any]) -> None:
    st.markdown(
        """
        <style>
        .mt-graph-note { color: #111827 !important; font-size: 0.92rem; line-height: 1.5; }
        .mt-graph-note b { color: #0f172a !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    if not selected_key:
        render_empty_state("선택된 케이스가 없습니다.")
        return

    run_id = latest_bundle.get("run_id")
    try:
        enabled_info = get("/api/v1/graph/enabled") or {}
    except Exception as e:
        st.warning(f"그래프 기능 상태를 조회하지 못했습니다: {e}")
        return

    if not enabled_info.get("enabled"):
        render_panel_header("그래프 인사이트", "근거 경로(Explainability)와 연관 케이스 탐지 결과를 확인합니다.")
        st.info("Graph DB(Neo4j)가 비활성화 상태입니다. `.env`의 `ENABLE_GRAPH_DB=true`로 활성화하세요.")
        return

    uri = enabled_info.get("uri") or "-"
    db_name = enabled_info.get("database") or "-"
    render_panel_header(
        "그래프 인사이트",
        f"근거 경로(Explainability)와 연관 케이스 탐지 결과를 확인합니다. (Neo4j 연결: {uri} · DB: {db_name})",
    )

    st.markdown("#### 근거 경로")
    try:
        path = f"/api/v1/graph/cases/{selected_key}/explain"
        if run_id:
            path = f"{path}?run_id={run_id}"
        explain = get(path) or {}
    except Exception as e:
        st.warning(f"근거 경로 조회 실패: {e}")
        explain = {}

    summary = explain.get("summary") or {}
    nodes = explain.get("nodes") or []
    edges = explain.get("edges") or []
    if not nodes:
        render_empty_state("아직 그래프 데이터가 없습니다. 분석을 1회 실행해 주세요.")
    else:
        st.markdown(
            "<div class='mt-graph-note'>"
            f"<b>run_id</b>={summary.get('run_id') or '-'} · "
            f"<b>status</b>={summary.get('status') or '-'} · "
            f"<b>case_type</b>={summary.get('case_type') or '-'}"
            "</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            "<div class='mt-graph-note'>"
            + " · ".join(
                [
                    f"노드 {len(nodes)}",
                    f"관계 {len(edges)}",
                    f"Claim {len([n for n in nodes if n.get('type') == 'Claim'])}",
                ]
            )
            + "</div>",
            unsafe_allow_html=True,
        )

        claim_nodes = [n for n in nodes if n.get("type") == "Claim"]
        policy_nodes = {str(n.get("id")): n for n in nodes if n.get("type") == "Policy"}
        claim_support: dict[str, list[str]] = {}
        for e in edges:
            if e.get("type") != "SUPPORTED_BY":
                continue
            frm = str(e.get("from") or "")
            to = str(e.get("to") or "")
            if not frm or not to:
                continue
            claim_support.setdefault(frm, []).append(to)

        if claim_nodes:
            st.markdown("**핵심 근거 경로**")
            shown = 0
            for c in claim_nodes:
                claim_id = str(c.get("id") or "")
                claim_label = str(c.get("label") or "").strip()
                if not claim_label:
                    continue
                policies = claim_support.get(claim_id) or []
                policy_labels: list[str] = []
                support_articles: list[str] = []
                for pid in policies[:3]:
                    p = policy_nodes.get(pid) or {}
                    pl = str(p.get("label") or pid).strip()
                    if pl:
                        policy_labels.append(_policy_label_compact(pl))
                        token = _extract_article_token(pl)
                        if token and token not in support_articles:
                            support_articles.append(token)
                claim_text_ui = _claim_user_facing_text(claim_label, support_articles)
                if policy_labels:
                    st.markdown(
                        "<div class='mt-graph-note'>"
                        f"• 주장: {claim_text_ui[:90]}{'…' if len(claim_text_ui) > 90 else ''}  →  "
                        + ", ".join(policy_labels)
                        + "</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"<div class='mt-graph-note'>• 주장: {claim_text_ui[:90]}{'…' if len(claim_text_ui) > 90 else ''}</div>",
                        unsafe_allow_html=True,
                    )
                shown += 1
                if shown >= 4:
                    break

        _render_explain_graph_plotly(
            nodes,
            edges,
            key=f"mt_explain_graph_{selected_key}_{summary.get('run_id') or 'none'}",
        )

        with st.expander("근거 경로 상세(노드/관계)", expanded=False):
            st.markdown("**노드**")
            for n in nodes[:30]:
                nid = str(n.get("id") or "-")[:48]
                lbl = str(n.get("label") or "-")[:110]
                body = f"- [{n.get('type')}] {nid}" if nid == lbl else f"- [{n.get('type')}] {nid} · {lbl}"
                st.caption(body)
            if len(nodes) > 30:
                st.caption(f"... 외 {len(nodes) - 30}건")
            st.markdown("**관계**")
            for e in edges[:40]:
                st.caption(
                    f"- {str(e.get('from') or '-')[:28]} --{e.get('type') or '-'}--> {str(e.get('to') or '-')[:28]}"
                )
            if len(edges) > 40:
                st.caption(f"... 외 {len(edges) - 40}건")

    # NOTE:
    # 하단 "연관 케이스" 영역은 사용자 요청으로 임시 비노출 처리.
    # 필요 시 아래 블록을 복구하면 기존 동작(related API 조회 + 그래프/리스트 렌더)이 그대로 동작한다.
    # st.markdown("---")
    # st.markdown("#### 연관 케이스")
    # try:
    #     related = get(f"/api/v1/graph/cases/{selected_key}/related?limit=10") or {}
    # except Exception as e:
    #     st.warning(f"연관 케이스 조회 실패: {e}")
    #     related = {}
    #
    # items = related.get("items") or []
    # if not items:
    #     render_empty_state("연결 규칙에 매칭되는 연관 케이스가 없습니다.")
    # else:
    #     st.metric("연관 케이스 수", str(len(items)))
    #     _render_related_graph_plotly(
    #         selected_key,
    #         items[:10],
    #         key=f"mt_related_graph_{selected_key}_{run_id or 'none'}",
    #     )
    #     for i, row in enumerate(items[:10], start=1):
    #         st.markdown(
    #             f"**{i}. {row.get('voucher_key') or '-'}**  \n"
    #             f"유형: {case_type_display_name(row.get('case_type'))} · "
    #             f"상태: {status_display_name(row.get('status'))} · "
    #             f"심각도: {severity_display_name(row.get('severity'))} · "
    #             f"연결점수: {row.get('link_score') or 0}  \n"
    #             f"사유: {', '.join(row.get('reasons') or []) or '-'}"
    #         )
    #         if i < min(len(items), 10):
    #             st.markdown("---")


def render_ai_workspace_page() -> None:
    render_page_header("AI 워크스페이스", "전표 기반 자율형 에이전트가 실제로 추론하고, 도구를 호출하고, 규정 근거를 바탕으로 판단하는 메인 시연 화면입니다.")
    items = get("/api/v1/vouchers?queue=all&limit=50").get("items") or []
    debug_mode = bool(st.session_state.get("mt_debug_mode", False))
    item_keys = {str(item.get("voucher_key") or "") for item in items if item.get("voucher_key")}
    selected_key = str(st.session_state.get("mt_selected_voucher") or "")
    # 삭제/필터 변경 후에도 stale 선택키가 남지 않도록 현재 목록 기준으로 보정한다.
    if selected_key and selected_key not in item_keys:
        st.session_state.pop("mt_selected_voucher", None)
        selected_key = ""
    if not selected_key and items:
        selected_key = str(items[0].get("voucher_key") or "")
        if selected_key:
            st.session_state["mt_selected_voucher"] = selected_key
    if not items:
        st.session_state.pop("mt_selected_voucher", None)
    selected_key = selected_key or None
    # 스트림 종료 직후 rerun인 경우, 그때 가져둔 번들로 판단요약 등 하단 탭을 즉시 갱신
    post_stream = st.session_state.pop("mt_post_stream_bundle", None)
    if selected_key and post_stream and post_stream.get("voucher_key") == selected_key:
        latest_bundle = post_stream.get("bundle") or fetch_case_bundle(selected_key)
        logger.info(
            "[ui] render_ai_workspace_page mt_post_stream_bundle 사용 selected_key=%s run_id=%s — 하단 탭(판단요약 등) 이 번들로 렌더",
            selected_key,
            (latest_bundle or {}).get("run_id"),
        )
    else:
        latest_bundle = fetch_case_bundle(selected_key) if selected_key else {"timeline": [], "history": []}
        if post_stream is not None:
            logger.info(
                "[ui] render_ai_workspace_page mt_post_stream_bundle 있으나 voucher 불일치 또는 selected_key 없음 — fetch_case_bundle 사용 post_stream.voucher_key=%s selected_key=%s",
                post_stream.get("voucher_key"),
                selected_key,
            )
    # 우측(최신 run 결과)과 좌측(목록 case_status) 간 표기 어긋남을 줄이되,
    # KPI가 흔들리지 않도록 버킷 상태로 정규화해 동기화한다.
    if selected_key:
        latest_result = (latest_bundle.get("result") or {}).get("result") or {}
        run_status = latest_result.get("status")
        if run_status:
            normalized_status = _normalize_case_status_for_kpi(run_status)
            for item in items:
                if item.get("voucher_key") == selected_key:
                    item["case_status"] = normalized_status
                    break

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

    # 상단 좌/우 레이아웃은 key 기반 CSS로 고정해 초기 렌더 후 재렌더 깜빡임을 방지
    st.markdown(
        """
        <style>
        [class*="st-key-workspace_main_split"] > [data-testid="stHorizontalBlock"] {
          align-items: stretch !important;
          flex-wrap: nowrap !important;
        }
        [class*="st-key-workspace_main_split"] > [data-testid="stHorizontalBlock"] > [data-testid="column"],
        [class*="st-key-workspace_main_split"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
          min-width: 0 !important;
        }
        [class*="st-key-workspace_case_queue_card"] {
          min-height: 540px !important;
          display: block !important;
        }
        @media (max-width: 1200px) {
          [class*="st-key-workspace_main_split"] > [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap !important;
          }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.container(key="workspace_main_split"):
        left, right = st.columns([0.78, 1.67], gap="large")
        with left:
            with stylable_container(key="workspace_case_queue_card", css_styles="""{padding: 18px 18px; border-radius: 20px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.96); box-shadow: 0 12px 30px rgba(15,23,42,0.05); min-height: 540px; overflow-x: hidden; overflow-y: visible; max-width: 100%; box-sizing: border-box;}"""):
                render_workspace_case_queue(items, selected_key)
        with right:
            with stylable_container(key="workspace_chat_card", css_styles="""{padding: 18px 20px; border-radius: 20px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.96); box-shadow: 0 12px 30px rgba(15,23,42,0.05); margin-bottom: 12px; overflow-x: hidden; overflow-y: visible; max-width: 100%;}"""):
                if not selected_key:
                    with stylable_container(key="workspace_chat_empty_state", css_styles="""{max-width: 100%; overflow: hidden;}"""):
                        render_empty_state("선택된 케이스가 없습니다.")
                else:
                    selected = next((item for item in items if item["voucher_key"] == selected_key), None) or {}
                    render_workspace_chat_panel(selected, latest_bundle)
            with stylable_container(key="workspace_result_card", css_styles="""{padding: 18px 20px; border-radius: 20px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.96); box-shadow: 0 12px 30px rgba(15,23,42,0.05); min-height: 520px; overflow-x: hidden; overflow-y: visible; max-width: 100%; box-sizing: border-box;}"""):
                if not selected_key:
                    with stylable_container(key="workspace_result_empty_state", css_styles="""{max-width: 100%; overflow: hidden;}"""):
                        render_empty_state("케이스를 선택하면 AI 워크스페이스가 표시됩니다.")
                else:
                    tabs = st.tabs(["판단 요약", "근거 맵", "실행 내역", "검토 이력"])
                    with tabs[0]:
                        render_workspace_results(latest_bundle, debug_mode)
                        timeline = latest_bundle.get("timeline") or []
                        if timeline:
                            with st.expander("판단 흐름 요약", expanded=False):
                                render_process_story(timeline, debug_mode=debug_mode, key_prefix="summary")
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
                                        diag_tax_a = diag.get("unsupported_taxonomy_counts") or {}
                                        diag_tax_b = diag2.get("unsupported_taxonomy_counts") or {}
                                        tax_preview_a = ", ".join(f"{k}:{v}" for k, v in sorted(diag_tax_a.items(), key=lambda kv: (-kv[1], kv[0]))[:3]) if diag_tax_a else "-"
                                        tax_preview_b = ", ".join(f"{k}:{v}" for k, v in sorted(diag_tax_b.items(), key=lambda kv: (-kv[1], kv[0]))[:3]) if diag_tax_b else "-"
                                        with col_a:
                                            st.metric("Tool 성공률", f"{(diag.get('tool_call_success_rate') or 0) * 100:.1f}%" if diag.get("tool_call_success_rate") is not None else "-", f"{diag.get('tool_call_ok', 0)}/{diag.get('tool_call_total', 0)}")
                                            st.metric("Citation coverage", f"{(diag.get('citation_coverage') or 0) * 100:.1f}%" if diag.get("citation_coverage") is not None else "-", "")
                                            st.metric("HITL 요청", "예" if diag.get("hitl_requested") else "아니오", "재개 성공" if diag.get("resume_success") else "")
                                            st.metric("Fallback 비율", f"{(diag.get('fallback_usage_rate') or 0) * 100:.1f}%" if diag.get("fallback_usage_rate") is not None else "-", f"이벤트 {diag.get('event_count', 0)}건")
                                            st.metric("Unsupported claim", str(diag.get("unsupported_claim_count") or 0), "fail-closed" if diag.get("fail_closed_unsupported") else "")
                                            st.caption(f"taxonomy: {tax_preview_a}")
                                        with col_b:
                                            st.metric("Tool 성공률", f"{(diag2.get('tool_call_success_rate') or 0) * 100:.1f}%" if diag2.get("tool_call_success_rate") is not None else "-", f"{diag2.get('tool_call_ok', 0)}/{diag2.get('tool_call_total', 0)}")
                                            st.metric("Citation coverage", f"{(diag2.get('citation_coverage') or 0) * 100:.1f}%" if diag2.get("citation_coverage") is not None else "-", "")
                                            st.metric("HITL 요청", "예" if diag2.get("hitl_requested") else "아니오", "재개 성공" if diag2.get("resume_success") else "")
                                            st.metric("Fallback 비율", f"{(diag2.get('fallback_usage_rate') or 0) * 100:.1f}%" if diag2.get("fallback_usage_rate") is not None else "-", f"이벤트 {diag2.get('event_count', 0)}건")
                                            st.metric("Unsupported claim", str(diag2.get("unsupported_claim_count") or 0), "fail-closed" if diag2.get("fail_closed_unsupported") else "")
                                            st.caption(f"taxonomy: {tax_preview_b}")
                                    else:
                                        c1, c2, c3, c4, c5 = st.columns(5)
                                        diag_tax = diag.get("unsupported_taxonomy_counts") or {}
                                        tax_preview = ", ".join(f"{k}:{v}" for k, v in sorted(diag_tax.items(), key=lambda kv: (-kv[1], kv[0]))[:3]) if diag_tax else "-"
                                        c1.metric("Tool 성공률", f"{(diag.get('tool_call_success_rate') or 0) * 100:.1f}%" if diag.get("tool_call_success_rate") is not None else "-", f"{diag.get('tool_call_ok', 0)}/{diag.get('tool_call_total', 0)}")
                                        c2.metric("Citation coverage", f"{(diag.get('citation_coverage') or 0) * 100:.1f}%" if diag.get("citation_coverage") is not None else "-", "")
                                        c3.metric("HITL 요청", "예" if diag.get("hitl_requested") else "아니오", "재개 성공" if diag.get("resume_success") else "")
                                        c4.metric("Fallback 비율", f"{(diag.get('fallback_usage_rate') or 0) * 100:.1f}%" if diag.get("fallback_usage_rate") is not None else "-", f"이벤트 {diag.get('event_count', 0)}건")
                                        c5.metric("Unsupported claim", str(diag.get("unsupported_claim_count") or 0), "fail-closed" if diag.get("fail_closed_unsupported") else "")
                                        st.caption(f"taxonomy: {tax_preview}")
                                except Exception:
                                    st.caption("진단 API를 불러올 수 없습니다.")
