from __future__ import annotations

import json
from typing import Any, Iterator

import requests
import streamlit as st
from graphviz import Digraph
from streamlit_option_menu import option_menu
from streamlit_extras.stylable_container import stylable_container

from agent.langgraph_agent import build_agent_graph
from agent.skills import SKILL_REGISTRY
from services.rag_chunk_lab_service import list_rulebook_files, load_rulebook_text, preview_chunks, save_uploaded_rulebook
from utils.config import settings


st.set_page_config(page_title="Aura Agent AI", layout="wide", initial_sidebar_state="expanded")

API = settings.api_base_url.rstrip("/")
MENU_OPTIONS = [
    ("AI 워크스페이스", ""),
    ("에이전트 스튜디오", "Agent model, prompt, tool, knowledge"),
    ("규정문서 라이브러리", "RAG document governance and quality"),
    ("시연 데이터 제어", "Scenario data generator"),
]


def inject_css() -> None:
    sidebar_width = "280px"
    sidebar_padding_top = "1rem"
    css_template = """
        <style>
        :root {{
          --mt-primary: #2563eb;
          --mt-primary-soft: #dbeafe;
          --mt-border: #e5e7eb;
          --mt-bg-soft: #f8fafc;
          --mt-text-soft: #64748b;
          --mt-text-strong: #0f172a;
          --mt-danger-soft: #fef2f2;
          --mt-success-soft: #ecfdf5;
          --mt-warning-soft: #fffbeb;
        }}
        .stApp {{ background: linear-gradient(180deg, #f8fafc 0%, #eef2ff 100%); }}
        [data-testid="stAppViewContainer"] > .main {{
          background: transparent !important;
        }}
        [data-testid="block-container"] {{
          padding-top: 1.25rem !important;
          padding-bottom: 1.25rem !important;
          max-width: 100% !important;
        }}
        .stApp, .stApp p, .stApp li, .stApp label, .stApp span, .stApp div, .stApp small,
        .stApp strong, .stApp em, .stApp code {{
          color: var(--mt-text-strong);
        }}
        .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6 {{
          color: var(--mt-text-strong);
        }}
        header[data-testid="stHeader"] {{
          background: transparent !important;
          height: 0 !important;
          min-height: 0 !important;
          border: none !important;
          box-shadow: none !important;
        }}
        header[data-testid="stHeader"] * {{
          display: none !important;
        }}

        section[data-testid="stSidebar"] {{
          background: #111827 !important;
          border-right: 1px solid rgba(255,255,255,0.08) !important;
          top: 0 !important;
          padding-top: 0 !important;
          z-index: 999900 !important;
          min-width: {sidebar_width} !important;
          max-width: {sidebar_width} !important;
        }}
        section[data-testid="stSidebar"] > div:first-child {{
          padding-top: {sidebar_padding_top} !important;
        }}
        section[data-testid="stSidebar"] * {{ color: #e5e7eb; }}

        section[data-testid="stSidebar"] .stSelectbox [data-baseweb="select"] > div,
        section[data-testid="stSidebar"] .stSelectbox [data-baseweb="select"] {{
          background-color: rgba(255,255,255,0.07) !important;
          border-color: rgba(255,255,255,0.18) !important;
          border-radius: 10px !important;
        }}
        section[data-testid="stSidebar"] .stSelectbox [data-baseweb="select"] * {{
          color: #e5e7eb !important;
          background-color: transparent !important;
        }}
        section[data-testid="stSidebar"] .stSelectbox svg,
        section[data-testid="stSidebar"] .stSelectbox svg path {{
          fill: #e5e7eb !important;
        }}

        section[data-testid="stSidebar"] .stCheckbox label,
        section[data-testid="stSidebar"] .stCheckbox span,
        section[data-testid="stSidebar"] .stExpander summary,
        section[data-testid="stSidebar"] .stExpander summary *,
        section[data-testid="stSidebar"] .stExpander [data-testid="stExpanderDetails"] * {{
          color: #e5e7eb !important;
        }}
        section[data-testid="stSidebar"] code {{
          color: #93c5fd !important;
          background: rgba(255,255,255,0.08) !important;
        }}

        section[data-testid="stSidebar"] button[data-testid="baseButton-headerNoPadding"] {{
          display: none !important;
        }}
        [data-testid="collapsedControl"],
        [data-testid="stSidebarCollapsedControl"],
        [data-testid="stSidebarCollapseButton"] {{
          display: none !important;
        }}
        [data-testid="stToolbar"],
        #MainMenu {{
          display: none !important;
        }}
        .mt-sidebar-title {{ font-size: 1.15rem; font-weight: 800; color: white; margin-bottom: 0.2rem; }}
        .mt-sidebar-sub {{ font-size: 0.82rem; color: #94a3b8; margin-bottom: 1rem; }}
        .mt-sidebar-title-row {{ display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; margin-bottom: 0.35rem; }}
        .nav-link, .nav-link * {{ color: #e5e7eb !important; }}
        .nav-link-selected, .nav-link-selected * {{ color: #ffffff !important; }}
        .mt-page-header {{
          padding: 20px 24px; border: 1px solid var(--mt-border); background: rgba(255,255,255,0.92);
          border-radius: 20px; box-shadow: 0 12px 40px rgba(15,23,42,0.05); margin-bottom: 16px;
        }}
        .mt-page-title {{ font-size: 1.8rem; font-weight: 800; color: #0f172a; }}
        .mt-page-sub {{ font-size: 0.95rem; color: var(--mt-text-soft); margin-top: 4px; }}
        .mt-card {{
          padding: 16px 18px; border-radius: 18px; border: 1px solid var(--mt-border);
          background: rgba(255,255,255,0.95); box-shadow: 0 12px 30px rgba(15,23,42,0.04);
          height: 100%;
        }}
        .mt-case-card {{
          padding: 14px 16px; border-radius: 18px; border: 1px solid var(--mt-border);
          background: rgba(255,255,255,0.98); box-shadow: 0 8px 22px rgba(15,23,42,0.04);
          margin-bottom: 12px;
        }}
        .mt-clickable {{ cursor: pointer; }}
        .mt-case-title {{ font-size: 0.96rem; font-weight: 800; color: var(--mt-text-strong); }}
        .mt-case-sub {{ font-size: 0.84rem; color: var(--mt-text-soft); margin-top: 4px; }}
        .mt-kpi {{
          padding: 16px 18px; border-radius: 20px; border: 1px solid var(--mt-border);
          background: rgba(255,255,255,0.96); box-shadow: 0 10px 24px rgba(15,23,42,0.04);
        }}
        .mt-kpi-label {{ font-size: 0.82rem; color: var(--mt-text-soft); font-weight: 700; }}
        .mt-kpi-value {{ font-size: 2rem; font-weight: 800; color: #0f172a; line-height: 1.15; }}
        .mt-kpi-foot {{ font-size: 0.82rem; color: var(--mt-text-soft); }}
        .mt-section-title {{ font-size: 1.02rem; font-weight: 800; color: #0f172a; margin-bottom: 10px; }}
        .mt-case-btn {{ width: 100%; text-align: left; }}
        .mt-badge {{
          display:inline-block; padding:4px 10px; border-radius:999px; font-size:0.72rem; font-weight:700;
          border:1px solid var(--mt-border); background:#fff; color:#334155; margin-right:6px; margin-bottom:6px;
        }}
        .mt-badge-blue {{ background:#eff6ff; color:#1d4ed8; border-color:#bfdbfe; }}
        .mt-badge-red {{ background:#fef2f2; color:#dc2626; border-color:#fecaca; }}
        .mt-badge-amber {{ background:#fffbeb; color:#d97706; border-color:#fde68a; }}
        .mt-badge-green {{ background:#ecfdf5; color:#059669; border-color:#a7f3d0; }}
        .mt-muted {{ color: var(--mt-text-soft); }}
        div[data-testid="stTabs"] button[role="tab"] {{ font-weight: 700; }}
        .mt-stream-card {{
          padding: 14px 16px; border-radius: 16px; border: 1px solid var(--mt-border); background: #fff;
          margin-bottom: 10px;
        }}
        .mt-scenario-card, .mt-seeded-card {{
          overflow: visible;
          word-break: break-word;
          width: 100%;
          box-sizing: border-box;
        }}
        .mt-scenario-meta {{
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
          margin-top: 8px;
          margin-bottom: 10px;
        }}
        .mt-meta-pill {{
          display: inline-flex;
          align-items: center;
          padding: 6px 10px;
          border-radius: 999px;
          background: #f8fafc;
          border: 1px solid #e2e8f0;
          color: #475569;
          font-size: 0.78rem;
          font-weight: 700;
          line-height: 1.1;
        }}
        .mt-scenario-desc {{
          min-height: 54px;
        }}
        .mt-scenario-title {{
          min-height: 52px;
        }}
        .mt-scenario-card p, .mt-scenario-card div, .mt-scenario-card span,
        .mt-seeded-card p, .mt-seeded-card div, .mt-seeded-card span,
        .mt-scenario-card [data-testid="stMarkdownContainer"],
        .mt-seeded-card [data-testid="stMarkdownContainer"] {{
          overflow-wrap: anywhere;
          word-break: break-word;
        }}
        .mt-scenario-card .stButton,
        .mt-scenario-card div[data-testid="stButton"] {{
          width: 100%;
          max-width: 100%;
          padding: 0;
          box-sizing: border-box;
          overflow: hidden;
        }}
        .mt-scenario-card .stButton > button {{
          display: block !important;
          width: 100% !important;
          max-width: 100% !important;
          margin: 0 !important;
          box-sizing: border-box !important;
        }}
        .mt-stream-card * {{
          color: var(--mt-text-strong) !important;
        }}
        .mt-mini {{ font-size: 0.78rem; color: var(--mt-text-soft); }}
        div[data-baseweb="tab-list"] button,
        div[data-baseweb="tab-list"] button p,
        div[data-baseweb="tab-list"] button span {{
          color: var(--mt-text-strong) !important;
        }}
        div[data-baseweb="button"] *,
        button[kind] *,
        button[data-testid="baseButton-secondary"] *,
        button[data-testid="baseButton-primary"] *,
        .stButton button,
        .stButton button *,
        .stDownloadButton button,
        .stDownloadButton button * {{
          color: var(--mt-text-strong) !important;
        }}
        .stButton button[kind="primary"],
        .stButton button[kind="primary"] *,
        button[data-testid="baseButton-primary"],
        button[data-testid="baseButton-primary"] * {{
          color: #ffffff !important;
          background: #2563eb !important;
          border-color: #2563eb !important;
        }}
        .stButton button[kind="secondary"],
        .stButton button[kind="secondary"] *,
        button[data-testid="baseButton-secondary"],
        button[data-testid="baseButton-secondary"] * {{
          color: #0f172a !important;
          background: #ffffff !important;
          border-color: #cbd5e1 !important;
        }}
        .stButton button,
        .stDownloadButton button {{
          border-radius: 12px !important;
          font-weight: 700 !important;
          box-shadow: none !important;
        }}
        div[role="radiogroup"] label,
        div[role="radiogroup"] p,
        div[role="radiogroup"] span {{
          color: var(--mt-text-strong) !important;
        }}
        div[role="listbox"] *,
        div[data-baseweb="select"] *,
        div[data-baseweb="input"] *,
        input, textarea {{
          color: var(--mt-text-strong) !important;
        }}
        [data-testid="stMetricLabel"],
        [data-testid="stMetricValue"],
        [data-testid="stMetricDelta"],
        [data-testid="stMarkdownContainer"],
        [data-testid="stText"],
        [data-testid="stCaptionContainer"],
        [data-testid="stHeading"],
        [data-testid="stVerticalBlock"],
        [data-testid="stHorizontalBlock"] {{
          color: var(--mt-text-strong) !important;
        }}
        [data-testid="stTabs"] button[role="tab"] {{
          color: var(--mt-text-strong) !important;
        }}
        [data-testid="stTabs"] button[role="tab"][aria-selected="true"] {{
          color: var(--mt-primary) !important;
        }}
        [data-testid="stExpander"] summary,
        [data-testid="stExpander"] summary * {{
          color: var(--mt-text-strong) !important;
        }}
        [data-testid="stAlert"] *, 
        [data-testid="stInfo"] *,
        [data-testid="stNotification"] * {{
          color: var(--mt-text-strong) !important;
        }}
        .stSelectbox label,
        .stTextInput label,
        .stNumberInput label,
        .stSlider label,
        .stRadio label,
        .stCheckbox label,
        .stTextArea label {{
          color: var(--mt-text-strong) !important;
        }}
        /* textarea 배경: 탭/본문과 동일한 밝은 배경 (프롬프트 탭 등) */
        .stTextArea,
        [data-testid="stTextArea"],
        .stTextArea textarea,
        [data-testid="stTextArea"] textarea,
        div[data-baseweb="textarea"],
        .stTextArea [data-baseweb="textarea"],
        [data-testid="stTextArea"] [data-baseweb="textarea"] {{
          background: #f8fafc !important;
          background-color: #f8fafc !important;
        }}
        .stTextArea textarea,
        [data-testid="stTextArea"] textarea,
        div[data-baseweb="textarea"] textarea {{
          color: #0f172a !important;
          border: 1px solid #e2e8f0 !important;
        }}

        [data-testid="stJson"],
        div[data-testid="stJson"],
        .mt-card [data-testid="stJson"],
        .mt-card div[data-testid="stJson"] {{
          background: #f8fafc !important;
          border: 1px solid #e2e8f0 !important;
          border-radius: 10px !important;
          padding: 1rem !important;
        }}
        [data-testid="stJson"] *,
        .mt-card [data-testid="stJson"] *,
        .mt-card [data-testid="stJson"] span,
        .mt-card [data-testid="stJson"] div,
        .mt-card [data-testid="stJson"] pre {{
          color: #0f172a !important;
          background: transparent !important;
        }}
        .mt-card [data-testid="stTabs"] [role="tabpanel"],
        .mt-card [data-testid="stTabs"] > div > div:last-child {{
          background: #ffffff !important;
        }}
        /* AI 워크스페이스 케이스 큐: 목록이 많아도 브라우저 스크롤 없이 박스 내 스크롤 */
        [data-testid="stHorizontalBlock"] > div:first-child [data-testid="stTabs"] [role="tabpanel"] {{
          max-height: min(50vh, 420px);
          overflow-y: auto;
        }}
        .mt-card [data-testid="stTabs"] [role="tabpanel"] *,
        .mt-card [data-testid="stTabs"] [role="tabpanel"] span,
        .mt-card [data-testid="stTabs"] [role="tabpanel"] div,
        .mt-card [data-testid="stTabs"] [role="tabpanel"] pre {{
          color: #0f172a !important;
        }}
        .mt-card [data-testid="stVerticalBlock"] pre,
        .mt-card pre {{
          background: #f8fafc !important;
          color: #0f172a !important;
          border: 1px solid #e2e8f0 !important;
          padding: 1rem !important;
          border-radius: 10px !important;
        }}
        .mt-card pre *,
        .mt-card [data-testid="stVerticalBlock"] pre * {{
          color: #0f172a !important;
        }}
        </style>
    """
    st.markdown(
        css_template.format(
            sidebar_width=sidebar_width,
            sidebar_padding_top=sidebar_padding_top,
        ),
        unsafe_allow_html=True,
    )


inject_css()


def _get(path: str) -> dict[str, Any]:
    r = requests.get(f"{API}{path}", timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path: str, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
    r = requests.post(f"{API}{path}", params=params or {}, json=json_body, timeout=30)
    r.raise_for_status()
    return r.json()


def _delete(path: str) -> dict[str, Any]:
    r = requests.delete(f"{API}{path}", timeout=30)
    r.raise_for_status()
    return r.json()


def _status_badge(status: str | None) -> str:
    value = str(status or "").upper()
    if not value:
        value = "NEW"
    if value in {"NEW", "READY"}:
        return '<span class="mt-badge mt-badge-blue">신규</span>'
    if value in {"IN_REVIEW", "REVIEW_REQUIRED", "REVIEW_AFTER_HITL"}:
        return '<span class="mt-badge mt-badge-amber">검토중</span>'
    if value in {"HITL_REQUIRED", "FAILED"}:
        return '<span class="mt-badge mt-badge-red">주의</span>'
    if value in {"RESOLVED", "COMPLETED", "OK"}:
        return '<span class="mt-badge mt-badge-green">완료</span>'
    return f'<span class="mt-badge">{value}</span>'


def _severity_badge(severity: str | None) -> str:
    value = str(severity or "").upper()
    if not value:
        value = "LOW"
    if value in {"CRITICAL", "HIGH"}:
        return '<span class="mt-badge mt-badge-red">높음</span>'
    if value == "MEDIUM":
        return '<span class="mt-badge mt-badge-blue">중간</span>'
    if value == "LOW":
        return '<span class="mt-badge mt-badge-green">낮음</span>'
    return f'<span class="mt-badge">{value or "낮음"}</span>'


def _case_type_badge(case_type: str | None) -> str:
    value = str(case_type or "").upper()
    if not value:
        value = "NORMAL_BASELINE"
    labels = {
        "HOLIDAY_USAGE": "휴일 사용 의심",
        "LIMIT_EXCEED": "한도 초과 의심",
        "PRIVATE_USE_RISK": "사적 사용 위험",
        "SPLIT_PAYMENT": "분할 결제 의심",
        "DUPLICATE_SUSPECT": "중복 결제 의심",
        "UNUSUAL_PATTERN": "비정상 패턴",
        "NORMAL_BASELINE": "정상 기준선",
        "DEFAULT": "기본 분류",
    }
    label = labels.get(value, value)
    if value in {"HOLIDAY_USAGE", "LIMIT_EXCEED", "PRIVATE_USE_RISK", "SPLIT_PAYMENT", "DUPLICATE_SUSPECT", "UNUSUAL_PATTERN"}:
        return f'<span class="mt-badge mt-badge-blue">{label}</span>'
    if value == "NORMAL_BASELINE":
        return f'<span class="mt-badge mt-badge-green">{label}</span>'
    return f'<span class="mt-badge">{label}</span>'


def _fmt_num(value: Any) -> str:
    try:
        num = float(value)
        return f"{num:,.0f}"
    except Exception:
        return "-"


def _fmt_dt(value: Any) -> str:
    if not value:
        return "-"
    text = str(value)
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return text


def _format_agent_event_line(obj: dict[str, Any]) -> str:
    node = obj.get("node") or "agent"
    et = obj.get("event_type") or "event"
    thought = obj.get("thought")
    action = obj.get("action")
    observation = obj.get("observation")
    message = obj.get("message") or ""
    parts = [f"[{node}/{et}] {message}"]
    if thought:
        parts.append(f"  - 생각: {thought}")
    if action:
        parts.append(f"  - 행동: {action}")
    if observation:
        parts.append(f"  - 관찰: {observation}")
    return "\n".join(parts) + "\n"


def sse_text_stream(stream_url: str) -> Iterator[str]:
    with requests.get(stream_url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        event = None
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
                continue
            if line.startswith("data:"):
                payload = line.split(":", 1)[1].strip()
                if payload == "[DONE]":
                    yield "\n분석 스트림 종료\n"
                    break
                try:
                    obj = json.loads(payload)
                    if event == "AGENT_EVENT":
                        yield _format_agent_event_line(obj)
                    elif event in {"step", "evidence", "confidence", "proposal"}:
                        label = obj.get("label") or event
                        detail = obj.get("detail") or obj.get("message") or ""
                        yield f"[{label}] {detail}\n"
                    elif event in {"AGENT_STREAM", "thought_pending"}:
                        txt = obj.get("content") or obj.get("message") or ""
                        if txt:
                            yield f"{txt}\n"
                    elif event == "completed":
                        rt = obj.get("reasonText") or obj.get("summary") or "완료"
                        yield f"\n[최종] {rt}\n"
                    elif event == "failed":
                        yield f"\n[실패] {obj.get('error', 'unknown error')}\n"
                    else:
                        yield f"[{event}] {json.dumps(obj, ensure_ascii=False)}\n"
                except Exception:
                    yield f"[{event}] {payload}\n"


def draw_agent_graph() -> Digraph:
    g = Digraph("finance_aura_agentic")
    g.attr(rankdir="LR")
    g.attr("graph", bgcolor="transparent", pad="0.25", nodesep="0.34", ranksep="0.42")
    g.attr("node", shape="box", style="rounded,filled", color="#a78bfa", fillcolor="#f5f3ff", fontname="Helvetica", fontsize="12")
    g.attr("edge", color="#94a3b8", penwidth="1.2")
    nodes = [
        ("start", "START"),
        ("intake", "Intake Agent"),
        ("planner", "Planner Agent"),
        ("executor", "Execute Agent"),
        ("critic", "Critic Agent"),
        ("verifier", "Verifier Agent"),
        ("reporter", "Reporter Agent"),
        ("finalizer", "Finalizer"),
        ("end", "END"),
    ]
    for key, label in nodes:
        g.node(key, label)
    g.node("hitl", "HITL Review", shape="box", style="rounded,dashed", color="#f59e0b", fillcolor="#fffbeb")
    g.edge("start", "intake")
    g.edge("intake", "planner")
    g.edge("planner", "executor")
    g.edge("executor", "critic")
    g.edge("critic", "verifier")
    g.edge("verifier", "hitl", label="if needed")
    g.edge("verifier", "reporter", label="or continue")
    g.edge("hitl", "reporter", label="resume with human input")
    g.edge("reporter", "finalizer")
    g.edge("finalizer", "end")
    return g


def draw_skill_execution_graph() -> Digraph:
    g = Digraph("finance_aura_skill_flow")
    g.attr(rankdir="TB")
    g.attr("graph", bgcolor="transparent", pad="0.25", nodesep="0.28", ranksep="0.38")
    g.attr("node", shape="box", style="rounded,filled", color="#93c5fd", fillcolor="#eff6ff", fontname="Helvetica", fontsize="12")
    g.attr("edge", color="#94a3b8", penwidth="1.2")
    g.node("execute", "execute")
    g.node("holiday", "holiday_compliance_probe")
    g.node("budget", "budget_risk_probe")
    g.node("merchant", "merchant_risk_probe")
    g.node("document", "document_evidence_probe")
    g.node("policy", "policy_rulebook_probe")
    g.node("legacy", "legacy_aura_deep_audit")
    g.node("score", "score_breakdown")
    g.edge("execute", "holiday")
    g.edge("execute", "budget")
    g.edge("execute", "merchant")
    g.edge("execute", "document")
    g.edge("execute", "policy")
    g.edge("execute", "legacy", label="conditional")
    g.edge("holiday", "score")
    g.edge("budget", "score")
    g.edge("merchant", "score")
    g.edge("document", "score")
    g.edge("policy", "score")
    g.edge("legacy", "score")
    return g


@st.cache_resource
def get_agent_graph_mermaid_png() -> bytes | None:
    try:
        graph = build_agent_graph()
        compiled_graph = graph.get_graph()
        return compiled_graph.draw_mermaid_png()
    except Exception:
        return None


def summarize_tool_results(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for tool in tool_results:
        skill = tool.get("skill") or "unknown"
        facts = tool.get("facts") or {}
        entry = {"skill": skill, "summary": tool.get("summary") or "-", "ok": tool.get("ok")}
        if skill == "policy_rulebook_probe":
            refs = facts.get("policy_refs") or []
            entry["metric_label"] = "규정 근거"
            entry["metric_value"] = f"{len(refs)}건"
            entry["detail"] = ", ".join(filter(None, [ref.get("article") for ref in refs[:3]])) or "-"
        elif skill == "document_evidence_probe":
            entry["metric_label"] = "전표 라인"
            entry["metric_value"] = f"{facts.get('lineItemCount', 0)}건"
            entry["detail"] = "-"
        elif skill == "legacy_aura_deep_audit":
            entry["metric_label"] = "전문감사"
            entry["metric_value"] = "실행"
            entry["detail"] = ((facts.get("reasonText") or facts.get("summary") or "-")[:80])
        else:
            detail_items = []
            for key in ("holidayRisk", "budgetExceeded", "merchantRisk"):
                if key in facts:
                    detail_items.append(f"{key}={facts.get(key)}")
            entry["metric_label"] = "확인 결과"
            entry["metric_value"] = "OK" if tool.get("ok") else "CHECK"
            entry["detail"] = ", ".join(detail_items) or "-"
        summary.append(entry)
    return summary


def render_tool_trace_summary(tool_results: list[dict[str, Any]]) -> None:
    cards = summarize_tool_results(tool_results)
    if not cards:
        st.info("도구 실행 요약이 없습니다.")
        return
    cols = st.columns(min(3, len(cards)))
    for idx, card in enumerate(cards):
        with cols[idx % len(cols)]:
            st.markdown('<div class="mt-card">', unsafe_allow_html=True)
            st.caption(card["skill"])
            st.markdown(f"**{card['metric_label']}**")
            st.subheader(card["metric_value"])
            st.write(card["detail"])
            st.markdown('</div>', unsafe_allow_html=True)


def render_timeline_cards(events: list[dict[str, Any]], *, view_mode: str = "business") -> None:
    if not events:
        st.info("표시할 스트림 이벤트가 없습니다.")
        return
    for ev in events:
        payload = ev.get("payload") or {}
        if ev.get("event_type") != "AGENT_EVENT":
            continue
        title = f"{payload.get('node') or '-'} / {payload.get('event_type') or '-'}"
        st.markdown('<div class="mt-stream-card">', unsafe_allow_html=True)
        st.caption(ev.get("at") or payload.get("timestamp") or "")
        st.markdown(f"**{title}**")
        st.write(payload.get("message") or "")
        if payload.get("thought"):
            st.markdown(f"- 생각: {payload['thought']}")
        if payload.get("action"):
            st.markdown(f"- 행동: {payload['action']}")
        if payload.get("observation"):
            st.markdown(f"- 관찰: {payload['observation']}")
        if view_mode == "debug":
            st.json(payload)
        st.markdown('</div>', unsafe_allow_html=True)


def render_hitl_history(history: list[dict[str, Any]]) -> None:
    rows = [item for item in history if item.get("hitl_request") or item.get("hitl_response")]
    if not rows:
        st.info("HITL 이력이 없습니다.")
        return
    for item in rows:
        st.markdown('<div class="mt-card">', unsafe_allow_html=True)
        st.markdown(f"**run_id** `{item.get('run_id')}`")
        lineage = item.get("lineage") or {}
        st.caption(f"mode={lineage.get('mode') or '-'} / parent={lineage.get('parent_run_id') or '-'}")
        if item.get("hitl_request"):
            st.markdown("**요청**")
            st.json(item["hitl_request"])
        if item.get("hitl_response"):
            st.markdown("**응답**")
            st.json(item["hitl_response"])
        st.markdown('</div>', unsafe_allow_html=True)


def render_agent_stream_panel(timeline: list[dict[str, Any]]) -> None:
    st.markdown("### 에이전트 스트림")

    latest_payload = (timeline[-1].get("payload") if timeline else {}) or {}
    latest_node = latest_payload.get("node") or "-"
    latest_event = latest_payload.get("event_type") or "-"
    latest_message = latest_payload.get("message") or "대기 중"

    status_label = f"현재 노드: {latest_node} · 이벤트: {latest_event}"
    state = "running" if timeline else "complete"
    with st.status(status_label, expanded=True, state=state):
        st.write(latest_message)
        if latest_payload.get("thought"):
            st.markdown(f"**생각**  \n{latest_payload.get('thought')}")
        if latest_payload.get("action"):
            st.markdown(f"**행동**  \n{latest_payload.get('action')}")
        if latest_payload.get("observation"):
            st.markdown(f"**관찰**  \n{latest_payload.get('observation')}")

    if not timeline:
        st.info("케이스 분석 중 LangGraph 실행 로그가 여기에 표시됩니다.")
        return

    with st.expander("에이전트 사고 과정 보기", expanded=True):
        for ev in timeline[-12:]:
            payload = ev.get("payload") or {}
            role = "assistant"
            if payload.get("event_type") in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED"}:
                role = "user"
            with st.chat_message(role):
                header = f"{payload.get('node') or '-'} · {payload.get('event_type') or ev.get('event_type') or '-'}"
                st.caption(f"{ev.get('at') or ''} · {header}")
                if payload.get("message"):
                    st.write(payload.get("message"))
                if payload.get("thought"):
                    st.markdown(f"**생각**: {payload.get('thought')}")
                if payload.get("action"):
                    st.markdown(f"**행동**: {payload.get('action')}")
                if payload.get("observation"):
                    st.markdown(f"**관찰**: {payload.get('observation')}")
                if payload.get("tool"):
                    st.caption(f"tool={payload.get('tool')}")

    with st.expander("State 객체(raw)", expanded=False):
        st.json(latest_payload)


def render_graph_image(title: str, image_bytes: bytes | None, fallback_graph: Digraph, caption: str) -> None:
    st.markdown(f"**{title}**")
    inner_left, inner_center, inner_right = st.columns([0.08, 0.84, 0.08])
    with inner_center:
        if image_bytes:
            st.image(image_bytes, use_container_width=False, width=520)
        else:
            st.graphviz_chart(fallback_graph, use_container_width=True)
        st.caption(caption)


def render_analysis_artifacts(latest_after: dict[str, Any], *, debug_mode: bool = False) -> None:
    result_wrap = latest_after.get("result") or {}
    result = result_wrap.get("result") or {}
    policy_refs = result.get("policy_refs") or []
    tool_results = result.get("tool_results") or []
    critique = result.get("critique") or {}
    hitl_response = latest_after.get("hitl_response") or result.get("hitl_response")
    timeline = latest_after.get("timeline") or []
    history = latest_after.get("history") or []
    is_debug = debug_mode
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["분석 단계", "최종 판단", "근거 맵", "도구 결과", "분석 이력"])
    with tab1:
        render_timeline_cards(timeline, view_mode="debug" if is_debug else "business")
    with tab2:
        st.subheader("Tool Trace 요약")
        render_tool_trace_summary(tool_results)
        st.write(result.get("reasonText") or "결과 없음")
        payload = {
            "status": result.get("status"),
            "severity": result.get("severity"),
            "score": result.get("score"),
            "score_breakdown": result.get("score_breakdown"),
            "quality_gate_codes": result.get("quality_gate_codes"),
            "critique": critique,
        }
        if is_debug:
            st.json(payload)
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("상태", str(payload["status"] or "-"))
            c2.metric("심각도", str(payload["severity"] or "-"))
            c3.metric("점수", str(payload["score"] or "-"))
            if payload["score_breakdown"]:
                sb = payload["score_breakdown"]
                st.caption(f"정책점수 {sb.get('policy_score', '-')} / 근거점수 {sb.get('evidence_score', '-')} / 최종점수 {sb.get('final_score', '-')}")
    with tab3:
        if policy_refs:
            for idx, ref in enumerate(policy_refs, start=1):
                title = f"C{idx}. {ref.get('article') or '-'} / {ref.get('parent_title') or '-'}"
                score = ref.get("retrieval_score")
                strategy = ref.get("source_strategy")
                if score is not None:
                    title += f"  · score={score}"
                if strategy:
                    title += f"  · {strategy}"
                st.markdown(f"**{title}**")
                st.caption(ref.get("chunk_text") or "")
                if is_debug:
                    st.json(ref)
        else:
            st.info("연결된 규정 근거가 없습니다.")
    with tab4:
        render_tool_trace_summary(tool_results)
        if tool_results and is_debug:
            for tool in tool_results:
                with st.expander(tool.get("skill") or "tool"):
                    st.json(tool)
        elif not tool_results:
            st.info("도구 결과가 없습니다.")
        if hitl_response:
            st.subheader("최근 HITL 응답")
            st.json(hitl_response)
    with tab5:
        if history:
            render_hitl_history(history)
            if is_debug:
                st.subheader("원본 분석 이력")
                st.json(history)
        else:
            st.info("분석 이력이 없습니다.")


def render_hitl_panel(latest_after: dict[str, Any]) -> None:
    run_id = latest_after.get("run_id")
    hitl_request = latest_after.get("hitl_request")
    if not run_id or not hitl_request:
        return
    st.subheader("HITL 검토 요청")
    st.warning("이 케이스는 사람 검토가 필요합니다.")
    st.json(hitl_request)
    with st.form(key=f"hitl_form_{run_id}"):
        reviewer = st.text_input("검토자", value="FINANCE_REVIEWER")
        comment = st.text_area("검토 의견")
        business_purpose = st.text_input("업무 목적")
        attendees_raw = st.text_input("참석자(쉼표 구분)")
        approved = st.checkbox("승인/정상 가능성 있음")
        submitted = st.form_submit_button("검토 응답 제출 후 재분석")
    if submitted:
        attendees = [x.strip() for x in attendees_raw.split(",") if x.strip()]
        out = _post(
            f"/api/v1/analysis-runs/{run_id}/hitl",
            json_body={
                "reviewer": reviewer,
                "comment": comment,
                "business_purpose": business_purpose,
                "attendees": attendees,
                "approved": approved,
                "extra_facts": {},
            },
        )
        st.success(f"재분석 시작: resumed_run_id={out['resumed_run_id']}")
        stream_url = f"{API}{out['stream_path']}"
        st.write_stream(sse_text_stream(stream_url))
        resumed_events = _get(f"/api/v1/analysis-runs/{out['resumed_run_id']}/events")
        with st.expander("재분석 이벤트(raw)", expanded=True):
            st.json(resumed_events)


def render_page_header(title: str, subtitle: str, right_html: str | None = None) -> None:
    with stylable_container(
        key=f"page_header_{title}",
        css_styles="""
        {
          padding: 20px 24px;
          border: 1px solid #e5e7eb;
          background: rgba(255,255,255,0.92);
          border-radius: 20px;
          box-shadow: 0 12px 40px rgba(15,23,42,0.05);
          margin-bottom: 16px;
        }
        """,
    ):
        cols = st.columns([0.75, 0.25])
        with cols[0]:
            st.markdown(f'<div class="mt-page-title">{title}</div>', unsafe_allow_html=True)
            if subtitle:
                st.markdown(f'<div class="mt-page-sub">{subtitle}</div>', unsafe_allow_html=True)
        with cols[1]:
            if right_html:
                st.markdown(right_html, unsafe_allow_html=True)

def render_kpi_card(label: str, value: str, foot: str = "") -> None:
    st.markdown(
        f'''<div class="mt-kpi"><div class="mt-kpi-label">{label}</div><div class="mt-kpi-value">{value}</div><div class="mt-kpi-foot">{foot}</div></div>''',
        unsafe_allow_html=True,
    )


def fetch_case_bundle(voucher_key: str) -> dict[str, Any]:
    latest = _get(f"/api/v1/cases/{voucher_key}/analysis/latest")
    history = _get(f"/api/v1/cases/{voucher_key}/analysis/history")
    if latest.get("run_id"):
        events = _get(f"/api/v1/analysis-runs/{latest['run_id']}/events")
        latest["timeline"] = events.get("events") or []
    else:
        latest["timeline"] = []
    latest["history"] = history.get("items") or []
    return latest


def render_queue_list(items: list[dict[str, Any]], selected_key: str | None, queue_key: str) -> str | None:
    tabs = st.tabs(["전체", "검토중"])
    selected = selected_key
    grouped = {
        "전체": items,
        "검토중": [item for item in items if str(item.get("case_status") or "").upper() in {"IN_REVIEW", "NEW"}],
    }
    for tab, label in zip(tabs, ["전체", "검토중"]):
        with tab:
            for item in grouped[label]:
                case_key = item["voucher_key"]
                with stylable_container(
                    key=f"case_card_{queue_key}_{label}_{case_key}",
                    css_styles="""
                    {
                      background: rgba(255,255,255,0.98);
                      border: 1px solid #e5e7eb;
                      border-radius: 18px;
                      padding: 0.15rem 0.2rem 0.6rem 0.2rem;
                      margin-bottom: 0.65rem;
                      box-shadow: 0 8px 22px rgba(15,23,42,0.04);
                    }
                    """,
                ):
                    top_cols = st.columns([0.72, 0.28])
                    with top_cols[0]:
                        st.markdown(
                            _status_badge(item.get("case_status")) + _case_type_badge(item.get("case_type")),
                            unsafe_allow_html=True,
                        )
                        st.markdown(f'<div class="mt-case-title">{item.get("merchant_name") or "-"}</div>', unsafe_allow_html=True)
                        st.markdown(
                            f'<div class="mt-case-sub">{_fmt_num(item.get("amount"))} {item.get("currency") or ""} · {case_key}</div>',
                            unsafe_allow_html=True,
                        )
                    with top_cols[1]:
                        if st.button(
                            "열기",
                            key=f"queue_{queue_key}_{label}_{case_key}",
                            use_container_width=True,
                            type="primary" if case_key == selected_key else "secondary",
                        ):
                            st.session_state["mt_case_preview"] = item
                            selected = case_key
    return selected


@st.dialog("케이스 정보")
def render_case_preview_dialog(case_item: dict[str, Any]) -> None:
    st.markdown(f"**{case_item.get('voucher_key') or '-'}**")
    st.write(case_item.get("merchant_name") or "-")
    c1, c2 = st.columns(2)
    with c1:
        st.metric("금액", f"{_fmt_num(case_item.get('amount'))} {case_item.get('currency') or ''}")
    with c2:
        st.metric("발생시각", _fmt_dt(case_item.get("occurred_at")))
    c3, c4 = st.columns(2)
    with c3:
        st.metric("상태", str(case_item.get("case_status") or "-"))
    with c4:
        st.metric("유형", str(case_item.get("case_type") or "-"))
    st.caption(f"voucher_key={case_item.get('voucher_key') or '-'}")
    if st.button("이 케이스 열기", use_container_width=True, type="primary"):
        st.session_state["mt_selected_voucher"] = case_item.get("voucher_key")
        st.session_state["mt_case_preview"] = None
        st.rerun()
    if st.button("닫기", use_container_width=True):
        st.session_state["mt_case_preview"] = None
        st.rerun()


def build_workspace_plan_steps(latest_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    timeline = latest_bundle.get("timeline") or []
    node_order = ["intake", "planner", "execute", "critic", "verify", "reporter", "finalizer"]
    node_meta = {
        "intake": ("입력 해석", "전표 입력값과 위험 신호를 정규화합니다."),
        "planner": ("조사 계획 수립", "검증할 사실과 사용할 skill 순서를 계획합니다."),
        "execute": ("근거 수집 실행", "휴일/예산/업종/전표/규정 근거를 실제로 조회합니다."),
        "critic": ("비판적 검토", "과잉 주장과 반례 가능성을 다시 점검합니다."),
        "verify": ("검증 및 HITL 판단", "자동 판정 가능 여부와 사람 검토 필요 여부를 결정합니다."),
        "reporter": ("보고 문장 생성", "근거 중심의 설명 문장과 최종 요약을 만듭니다."),
        "finalizer": ("결과 확정", "상태, 점수, 이력, 저장 payload를 최종 확정합니다."),
    }
    seen: set[str] = set()
    completed: set[str] = set()
    running = None
    for ev in timeline:
        payload = ev.get("payload") or {}
        if ev.get("event_type") != "AGENT_EVENT":
            continue
        node = str(payload.get("node") or "").lower()
        event_type = str(payload.get("event_type") or "").upper()
        if node in node_meta:
            seen.add(node)
            if event_type in {"NODE_END", "COMPLETE", "REPORT_READY", "RESULT_FINALIZED"}:
                completed.add(node)
            if event_type in {"NODE_START", "PLAN_READY", "TOOL_CALL", "TOOL_RESULT"}:
                running = node
    steps: list[dict[str, Any]] = []
    for idx, node in enumerate(node_order, start=1):
        title, desc = node_meta[node]
        if node in completed:
            status = "완료"
        elif node == running:
            status = "진행중"
        elif node in seen:
            status = "수행"
        else:
            status = "대기"
        steps.append({"order": idx, "title": title, "description": desc, "status": status})
    return steps


def build_workspace_execution_logs(latest_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    timeline = latest_bundle.get("timeline") or []
    rows: list[dict[str, Any]] = []
    for ev in timeline:
        payload = ev.get("payload") or {}
        if ev.get("event_type") != "AGENT_EVENT":
            continue
        event_type = str(payload.get("event_type") or "").upper()
        if event_type not in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED", "HITL_REQUESTED"}:
            continue
        rows.append(
            {
                "at": ev.get("at") or payload.get("timestamp") or "-",
                "node": payload.get("node") or "-",
                "event_type": event_type,
                "tool": payload.get("tool") or "-",
                "message": payload.get("message") or "-",
                "observation": payload.get("observation") or "",
            }
        )
    return rows


def render_workspace_case_queue(items: list[dict[str, Any]], selected_key: str | None) -> str | None:
    st.markdown("#### 케이스 큐")
    selected = selected_key
    tabs = st.tabs(["전체", "검토 필요"])
    grouped = {
        "전체": items,
        "검토 필요": [
            item
            for item in items
            if str(item.get("case_status") or "").upper() in {"NEW", "IN_REVIEW", "REVIEW_REQUIRED", "HITL_REQUIRED"}
        ],
    }
    for tab, label in zip(tabs, ["전체", "검토 필요"]):
        with tab:
            for item in grouped[label]:
                case_key = item["voucher_key"]
                selected_css = (
                    "border: 2px solid #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,0.08), 0 12px 26px rgba(15,23,42,0.08);"
                    if case_key == selected_key
                    else "border: 1px solid #e5e7eb; box-shadow: 0 8px 22px rgba(15,23,42,0.04);"
                )
                with stylable_container(
                    key=f"workspace_case_{label}_{case_key}",
                    css_styles=f"""
                    {{
                      background: rgba(255,255,255,0.98);
                      {selected_css}
                      border-radius: 18px;
                      padding: 0.25rem 0.35rem 0.75rem 0.35rem;
                      margin-bottom: 0.65rem;
                      cursor: pointer;
                    }}
                    """,
                ):
                    st.markdown(
                        _status_badge(item.get("case_status"))
                        + _severity_badge(item.get("severity"))
                        + _case_type_badge(item.get("case_type")),
                        unsafe_allow_html=True,
                    )
                    st.markdown(f"**{item.get('merchant_name') or '-'}**")
                    st.caption(f"{_fmt_num(item.get('amount'))} {item.get('currency') or ''} · {case_key}")
                    action_cols = st.columns([0.5, 0.5])
                    with action_cols[0]:
                        if st.button("상세", key=f"preview_{label}_{case_key}", use_container_width=True):
                            st.session_state["mt_case_preview"] = item
                            st.rerun()
                    with action_cols[1]:
                        if st.button(
                            "선택",
                            key=f"select_{label}_{case_key}",
                            use_container_width=True,
                            type="primary" if case_key == selected_key else "secondary",
                        ):
                            st.session_state["mt_selected_voucher"] = case_key
                            st.session_state["mt_case_preview"] = None
                            st.rerun()
    return selected


def render_workspace_chat_panel(selected: dict[str, Any], latest_bundle: dict[str, Any]) -> None:
    result_wrap = latest_bundle.get("result") or {}
    result = result_wrap.get("result") or {}
    timeline = latest_bundle.get("timeline") or []
    st.markdown("#### 에이전트 대화")
    st.markdown(
        _status_badge(result.get("status") if result else selected.get("case_status"))
        + _severity_badge(result.get("severity") if result else selected.get("severity"))
        + _case_type_badge(selected.get("case_type")),
        unsafe_allow_html=True,
    )
    st.markdown(f"**{selected.get('voucher_key') or '-'}** · {selected.get('merchant_name') or '-'}")
    st.caption(
        f"{_fmt_num(selected.get('amount'))} {selected.get('currency') or ''} · 발생시각 {_fmt_dt(selected.get('occurred_at'))}"
    )
    if st.button("분석 시작", key=f"workspace_run_{selected.get('voucher_key')}", use_container_width=True, type="primary"):
        res = _post(f"/api/v1/cases/{selected.get('voucher_key')}/analysis-runs")
        st.success(f"분석 시작: run_id={res['run_id']}")
        stream_url = f"{API}{res['stream_path']}"
        st.write_stream(sse_text_stream(stream_url))
        st.rerun()
    if not timeline:
        st.info("분석을 시작하면 LangGraph 실행 로그와 보고 문장이 여기에 실시간으로 표시됩니다.")
        return
    for ev in timeline[-14:]:
        payload = ev.get("payload") or {}
        if ev.get("event_type") != "AGENT_EVENT":
            continue
        role = "assistant"
        event_type = str(payload.get("event_type") or "").upper()
        if event_type in {"TOOL_CALL", "TOOL_RESULT", "TOOL_SKIPPED"}:
            role = "user"
        with st.chat_message(role):
            st.caption(f"{ev.get('at') or ''} · {payload.get('node') or '-'} / {payload.get('event_type') or '-'}")
            if payload.get("message"):
                st.write(payload.get("message"))
            if payload.get("thought"):
                st.markdown(f"**생각**: {payload.get('thought')}")
            if payload.get("action"):
                st.markdown(f"**행동**: {payload.get('action')}")
            if payload.get("observation"):
                st.markdown(f"**관찰**: {payload.get('observation')}")


def render_workspace_results(latest_bundle: dict[str, Any], debug_mode: bool) -> None:
    result_wrap = latest_bundle.get("result") or {}
    result = result_wrap.get("result") or {}
    critique = result.get("critique") or {}
    policy_refs = result.get("policy_refs") or []
    st.markdown("#### 최종 판단")
    c1, c2, c3 = st.columns(3)
    c1.metric("상태", str(result.get("status") or "-"))
    c2.metric("심각도", str(result.get("severity") or "-"))
    c3.metric("점수", str(result.get("score") or "-"))
    if result.get("score_breakdown"):
        sb = result["score_breakdown"]
        st.caption(
            f"정책점수 {sb.get('policy_score', '-')} · 근거점수 {sb.get('evidence_score', '-')} · 최종점수 {sb.get('final_score', '-')}"
        )
    st.write(result.get("reasonText") or "결과 없음")
    st.markdown("#### 규정 근거")
    if policy_refs:
        for idx, ref in enumerate(policy_refs, start=1):
            with st.expander(f"C{idx}. {ref.get('article') or '-'} / {ref.get('parent_title') or '-'}", expanded=(idx == 1)):
                meta = []
                if ref.get("retrieval_score") is not None:
                    meta.append(f"score={ref.get('retrieval_score')}")
                if ref.get("source_strategy"):
                    meta.append(str(ref.get("source_strategy")))
                if meta:
                    st.caption(" · ".join(meta))
                st.write(ref.get("chunk_text") or "")
                if debug_mode:
                    st.json(ref)
    else:
        st.info("연결된 규정 근거가 없습니다.")
    if critique:
        st.markdown("#### 검증 메모")
        st.json(critique if debug_mode else {"quality_gate_codes": critique.get("quality_gate_codes") or result.get("quality_gate_codes")})


def render_ai_workspace_page() -> None:
    render_page_header(
        "AI 워크스페이스",
        "전표 기반 자율형 에이전트가 실제로 추론하고, 도구를 호출하고, 규정 근거를 바탕으로 판단하는 메인 시연 화면입니다.",
    )
    all_data = _get("/api/v1/vouchers?queue=all&limit=50")
    items = all_data.get("items") or []
    debug_mode = bool(st.session_state.get("mt_debug_mode", False))
    selected_key = st.session_state.get("mt_selected_voucher") or (items[0]["voucher_key"] if items else None)
    latest_bundle = fetch_case_bundle(selected_key) if selected_key else {"timeline": [], "history": []}

    left, right = st.columns([0.38, 0.62])
    with left:
        with stylable_container(
            key="workspace_case_queue_card",
            css_styles="""
            {
              padding: 16px 18px;
              border-radius: 18px;
              border: 1px solid #e5e7eb;
              background: rgba(255,255,255,0.95);
              box-shadow: 0 12px 30px rgba(15,23,42,0.04);
              max-height: min(70vh, 560px);
              display: flex;
              flex-direction: column;
              overflow: hidden;
            }
            """,
        ):
            new_selected = render_workspace_case_queue(items, selected_key)
        if new_selected != selected_key:
            st.session_state["mt_selected_voucher"] = new_selected
            selected_key = new_selected
            st.rerun()
        preview = st.session_state.get("mt_case_preview")
        if preview:
            render_case_preview_dialog(preview)
        with stylable_container(
            key="workspace_chat_card",
            css_styles="""
            {
              padding: 16px 18px;
              border-radius: 18px;
              border: 1px solid #e5e7eb;
              background: rgba(255,255,255,0.95);
              box-shadow: 0 12px 30px rgba(15,23,42,0.04);
              margin-top: 12px;
            }
            """,
        ):
            if not selected_key:
                st.info("선택된 케이스가 없습니다.")
            else:
                selected = next((item for item in items if item["voucher_key"] == selected_key), None) or {}
                latest_bundle = fetch_case_bundle(selected_key)
                render_workspace_chat_panel(selected, latest_bundle)
    with right:
        with stylable_container(
            key="workspace_result_card",
            css_styles="""
            {
              padding: 16px 18px;
              border-radius: 18px;
              border: 1px solid #e5e7eb;
              background: rgba(255,255,255,0.95);
              box-shadow: 0 12px 30px rgba(15,23,42,0.04);
            }
            """,
        ):
            if not selected_key:
                st.info("케이스를 선택하면 AI 워크스페이스가 표시됩니다.")
            else:
                latest_bundle = fetch_case_bundle(selected_key)
                timeline = latest_bundle.get("timeline") or []
                plan_steps = build_workspace_plan_steps(latest_bundle)
                exec_logs = build_workspace_execution_logs(latest_bundle)
                tab1, tab2, tab3, tab4 = st.tabs(["사고 과정", "작업 계획", "실행 로그", "결과"])
                with tab1:
                    render_timeline_cards(timeline, view_mode="debug" if debug_mode else "business")
                with tab2:
                    for step in plan_steps:
                        with stylable_container(
                            key=f"plan_{selected_key}_{step['order']}",
                            css_styles="""
                            {
                              background: rgba(255,255,255,0.98);
                              border: 1px solid #e5e7eb;
                              border-radius: 16px;
                              padding: 0.85rem 1rem;
                              margin-bottom: 0.7rem;
                              box-shadow: 0 8px 22px rgba(15,23,42,0.04);
                            }
                            """,
                        ):
                            st.markdown(f"**{step['order']}. {step['title']}**")
                            st.caption(step["description"])
                            st.caption(f"상태: {step['status']}")
                with tab3:
                    tool_results = ((latest_bundle.get("result") or {}).get("result") or {}).get("tool_results") or []
                    render_tool_trace_summary(tool_results)
                    if exec_logs:
                        st.markdown("#### 실행 이벤트")
                        for log in exec_logs:
                            with stylable_container(
                                key=f"log_{selected_key}_{log['at']}_{log['tool']}",
                                css_styles="""
                                {
                                  background: #f8fafc;
                                  border: 1px solid #e5e7eb;
                                  border-radius: 14px;
                                  padding: 0.8rem 0.95rem;
                                  margin-bottom: 0.55rem;
                                }
                                """,
                            ):
                                st.caption(f"{log['at']} · {log['node']} / {log['event_type']}")
                                st.markdown(f"**{log['tool']}**")
                                st.write(log["message"])
                                if log["observation"]:
                                    st.caption(log["observation"])
                    else:
                        st.info("표시할 실행 로그가 없습니다.")
                with tab4:
                    render_workspace_results(latest_bundle, debug_mode)
                    render_hitl_panel(latest_bundle)
                    st.markdown("#### 분석 이력")
                    render_hitl_history(latest_bundle.get("history") or [])


def render_agent_studio_page() -> None:
    render_page_header("에이전트 스튜디오", "Agent model, prompt, tool, knowledge")
    data = _get("/api/v1/agents")
    agents = data.get("items") or []
    if not agents:
        st.info("에이전트 데이터가 없습니다.")
        return
    selected_id = st.session_state.get("mt_selected_agent_id") or agents[0]["agent_id"]
    left, right = st.columns([0.28, 0.72])
    with left:
        st.markdown('<div class="mt-card">', unsafe_allow_html=True)
        st.markdown('<div class="mt-section-title">에이전트 목록</div>', unsafe_allow_html=True)
        for agent in agents:
            label = f"{agent.get('name') or '-'} ({agent.get('agent_key') or '-'})"
            if st.button(label, key=f"agent_{agent['agent_id']}", use_container_width=True, type="primary" if int(agent['agent_id']) == int(selected_id) else "secondary"):
                st.session_state["mt_selected_agent_id"] = agent["agent_id"]
                st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    with right:
        detail = _get(f"/api/v1/agents/{selected_id}")
        st.markdown('<div class="mt-card">', unsafe_allow_html=True)
        st.markdown(f"## {detail.get('name') or '-'}")
        st.caption(f"agent_key={detail.get('agent_key') or '-'} / domain={detail.get('domain') or '-'}")
        tabs = st.tabs(["모델", "프롬프트", "도구", "지식", "그래프"])
        with tabs[0]:
            c1, c2, c3 = st.columns(3)
            c1.metric("모델", str(detail.get("model_name") or "-"))
            c2.metric("temperature", str(detail.get("temperature") or "-"))
            c3.metric("max_tokens", str(detail.get("max_tokens") or "-"))
            st.caption(f"active={detail.get('is_active')} / updated_at={_fmt_dt(detail.get('updated_at'))}")
        with tabs[1]:
            current_prompt = detail.get("current_prompt") or {}
            st.text_area("Current System Prompt", value=str(current_prompt.get("system_instruction") or ""), height=300)
            with st.expander("Prompt History"):
                st.json(detail.get("prompt_history") or [])
        with tabs[2]:
            for skill_name, skill in SKILL_REGISTRY.items():
                with st.expander(skill_name):
                    st.write(skill.description or "-")
        with tabs[3]:
            docs = detail.get("documents") or []
            if docs:
                for doc in docs:
                    st.markdown(f"- **{doc.get('title')}** · status={doc.get('status')} · doc_id={doc.get('doc_id')}")
            else:
                st.info("연결된 지식 문서가 없습니다.")
        with tabs[4]:
            graph_tabs = st.tabs(["메인 오케스트레이션", "스킬 실행 흐름"])
            with graph_tabs[0]:
                render_graph_image(
                    "메인 오케스트레이션 그래프",
                    None,
                    draw_agent_graph(),
                    "현재 PoC에서 실제 실행되는 메인 에이전트 오케스트레이션입니다.",
                )
                st.markdown(
                    """
                    **단계별 설명**

                    1. **START**
                    - 분석 요청이 들어오면 LangGraph 실행이 시작됩니다.

                    2. **Intake Agent**
                    - 입력 전표를 정규화합니다.
                    - 발생시각, 금액, 휴일 여부, 근태 상태, MCC, 예산 초과 여부 같은 핵심 신호를 추출합니다.

                    3. **Planner Agent**
                    - 어떤 사실을 먼저 확인할지 조사 계획을 세웁니다.
                    - 휴일 검증, 업종 위험, 예산 초과, 전표 증거 수집, 규정 조회, 필요 시 심층 감사 호출 순서를 결정합니다.

                    4. **Execute Agent**
                    - Planner가 만든 계획에 따라 실제 skill/tool을 실행합니다.
                    - 이 단계에서 규정집 조회, 전표 라인아이템 수집, 업종 위험 판별, 예산 초과 확인이 수행됩니다.
                    - 근거가 충분하면 일부 specialist tool은 생략할 수도 있습니다.

                    5. **Critic Agent**
                    - 수집된 결과를 비판적으로 다시 봅니다.
                    - 입력 누락, 과잉 주장 위험, 근거 부족 여부를 확인합니다.

                    6. **Verifier Agent**
                    - 현재 근거만으로 자동 결론이 가능한지 최종 검증합니다.
                    - 검증이 충분하면 바로 다음 단계로 진행하고, 부족하면 HITL 검토로 분기합니다.

                    7. **HITL Review**
                    - 사람이 직접 검토하고 보완 의견이나 소명 내용을 입력하는 단계입니다.
                    - 보완된 정보는 다시 Reporter 단계로 전달됩니다.

                    8. **Reporter Agent**
                    - 실행 과정에서 모은 사실과 규정 근거를 사람이 읽을 수 있는 보고 문장으로 정리합니다.
                    - 최종 reason text와 설명 가능한 요약이 이 단계에서 생성됩니다.

                    9. **Finalizer**
                    - 최종 상태, 점수, 심각도, 근거, HITL 여부를 묶어서 결과 payload를 완성합니다.

                    10. **END**
                    - 분석 런이 종료되고, UI/저장소/이력 조회에 사용할 최종 결과가 확정됩니다.
                    """
                )
            with graph_tabs[1]:
                render_graph_image(
                    "실행 스킬 그래프",
                    None,
                    draw_skill_execution_graph(),
                    "execute 노드 내부에서 호출되는 런타임 skill 흐름입니다. 규정 조회, 전표 증거 수집, 업종/예산/휴일 검증, 필요 시 legacy specialist 호출까지 포함합니다.",
                )
                st.markdown(
                    """
                    **단계별 설명**

                    1. **execute**
                    - Planner가 만든 조사 계획을 실제 실행하는 중심 노드입니다.
                    - 이 노드가 각 skill/tool 호출 순서를 관리합니다.

                    2. **holiday_compliance_probe**
                    - 휴일 여부, 근태 상태, 심야 시간대가 충돌하는지 확인합니다.
                    - 휴무일 사용, 휴가 중 사용 같은 기본 정책 위반 신호를 먼저 확인합니다.

                    3. **budget_risk_probe**
                    - 예산 초과 여부와 금액 신호를 확인합니다.
                    - 단순 초과인지, 추가 검토가 필요한 수준인지 점수 산정에 반영합니다.

                    4. **merchant_risk_probe**
                    - 거래처명과 MCC를 기준으로 업종 리스크를 판별합니다.
                    - 유흥, 고위험 업종, 제한 업종 같은 신호를 식별합니다.

                    5. **document_evidence_probe**
                    - 전표 라인아이템, 문서 구조, 증빙 데이터를 수집합니다.
                    - 실제 전표 증거가 얼마나 확보됐는지 확인하는 핵심 단계입니다.

                    6. **policy_rulebook_probe**
                    - 내부 규정집에서 현재 전표와 직접 연결되는 조항을 검색합니다.
                    - 휴일, 식대, 공통 제약, 검토/보류 근거가 되는 규정 조항을 확보합니다.

                    7. **legacy_aura_deep_audit**
                    - 필요할 때만 호출되는 specialist 도구입니다.
                    - 규정 근거와 전표 증거가 충분하면 생략될 수 있습니다.
                    - 즉, 항상 실행되는 것이 아니라 조건부(`conditional`) 실행입니다.

                    8. **score_breakdown**
                    - 각 skill에서 확보한 신호를 종합해 정책점수, 근거점수, 최종점수를 계산합니다.
                    - 이후 Critic/Verifier/Reporter 단계가 사용할 정량 근거를 만드는 역할입니다.
                    """
                )
        st.markdown('</div>', unsafe_allow_html=True)


def render_rag_library_page() -> None:
    render_page_header("규정문서 라이브러리", "Compliance knowledge governance and indexing status")
    data = _get("/api/v1/rag/documents")
    items = data.get("items") or []
    total = data.get("total") or len(items)
    indexed = len([item for item in items if str(item.get("status") or "").upper() == "COMPLETED"])
    attention = len([item for item in items if str(item.get("status") or "").upper() in {"PROCESSING", "FAILED", "VECTORIZING"}])
    passed = [item for item in items if item.get("quality_gate_passed") is True or item.get("quality_report_passed") is True]
    pass_rate = (len(passed) / total * 100) if total else 0
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        render_kpi_card("문서", str(total), "전체 등록")
    with k2:
        render_kpi_card("인덱싱됨", str(indexed), "인용 준비 완료")
    with k3:
        render_kpi_card("주의 필요", str(attention), "인덱싱/오류")
    with k4:
        render_kpi_card("청킹 합격률", f"{pass_rate:.1f}%", "quality_report 기준")
    top_tabs = st.tabs(["DB 라이브러리", "청킹 실험실"])
    with top_tabs[0]:
        left, right = st.columns([0.48, 0.52])
        selected_doc_id = st.session_state.get("mt_selected_doc_id") or (items[0]["doc_id"] if items else None)
        with left:
            st.markdown('<div class="mt-card">', unsafe_allow_html=True)
            st.markdown('<div class="mt-section-title">문서 목록</div>', unsafe_allow_html=True)
            for item in items:
                label = f"{item.get('title')} · doc_id={item.get('doc_id')}"
                if st.button(label, key=f"doc_{item['doc_id']}", use_container_width=True, type="primary" if int(item['doc_id']) == int(selected_doc_id) else "secondary"):
                    st.session_state["mt_selected_doc_id"] = item["doc_id"]
                    st.rerun()
                st.markdown(_status_badge(item.get("status")) + _severity_badge("LOW" if item.get("quality_gate_passed") else "MEDIUM"), unsafe_allow_html=True)
            st.markdown('</div>', unsafe_allow_html=True)
        with right:
            if selected_doc_id is None:
                st.info("선택된 문서가 없습니다.")
            else:
                detail = _get(f"/api/v1/rag/documents/{selected_doc_id}")
                st.markdown('<div class="mt-card">', unsafe_allow_html=True)
                st.markdown(f"## {detail.get('title')}")
                st.caption(f"doc_id={detail.get('doc_id')} / type={detail.get('doc_type')} / source={detail.get('source_type')}")
                t1, t2, t3 = st.tabs(["문서 메타", "품질 리포트", "청크 목록"])
                with t1:
                    meta = {
                        "status": detail.get("status"),
                        "version": detail.get("version"),
                        "effective_from": detail.get("effective_from"),
                        "effective_to": detail.get("effective_to"),
                        "lifecycle_status": detail.get("lifecycle_status"),
                        "active_from": detail.get("active_from"),
                        "active_to": detail.get("active_to"),
                        "quality_gate_passed": detail.get("quality_gate_passed"),
                        "updated_at": detail.get("updated_at"),
                    }
                    st.json(meta)
                with t2:
                    report = {
                        "quality_gate_passed": detail.get("quality_report_passed", detail.get("quality_gate_passed")),
                        "quality_run_id": detail.get("quality_run_id"),
                        "input_chunks": detail.get("input_chunks"),
                        "final_chunks": detail.get("final_chunks"),
                        "article_coverage": detail.get("article_coverage"),
                        "noise_rate": detail.get("noise_rate"),
                        "duplicate_rate": detail.get("duplicate_rate"),
                        "short_chunk_rate": detail.get("short_chunk_rate"),
                        "missing_required": detail.get("missing_required"),
                        "errors": detail.get("errors"),
                    }
                    st.json(report)
                with t3:
                    chunks = detail.get("chunks") or []
                    if chunks:
                        for chunk in chunks[:50]:
                            with st.expander(f"{chunk.get('regulation_article') or '-'} / {chunk.get('parent_title') or '-'} / chunk_id={chunk.get('chunk_id')}"):
                                st.caption(f"page={chunk.get('page_no')} / index={chunk.get('chunk_index')} / version={chunk.get('version')}")
                                st.write(chunk.get("chunk_text") or "")
                    else:
                        st.info("청크가 없습니다.")
                st.markdown('</div>', unsafe_allow_html=True)
    with top_tabs[1]:
        local_files = list_rulebook_files()
        st.markdown('<div class="mt-card">', unsafe_allow_html=True)
        st.markdown("#### 청킹 실험실")
        upload = st.file_uploader("규정 TXT 업로드", type=["txt"])
        if upload is not None:
            save_uploaded_rulebook(upload.name, upload.getvalue())
            st.success(f"업로드 완료: {upload.name}")
            st.rerun()
        left, right = st.columns([0.38, 0.62])
        with left:
            selected_file = None
            strategy = "hybrid_policy"
            if local_files:
                selected_file = st.selectbox(
                    "대상 문서",
                    options=local_files,
                    format_func=lambda x: f"{x['name']} ({x['source']})",
                    index=0,
                )
                strategy = st.selectbox(
                    "청킹 전략",
                    options=["hybrid_policy", "article_first", "sliding_window"],
                    format_func=lambda x: {
                        "hybrid_policy": "하이브리드 정책형",
                        "article_first": "조항 우선",
                        "sliding_window": "슬라이딩 윈도우",
                    }[x],
                )
                st.caption("하이브리드 정책형은 조항 경계를 먼저 지키고, 긴 조항만 내부 윈도우로 다시 나눕니다.")
            else:
                st.info("업로드된 규정 파일이 없습니다.")
        with right:
            if local_files and selected_file:
                file_path = selected_file["path"]
                text = load_rulebook_text(file_path)
                chunks = preview_chunks(text, strategy)
                c1, c2, c3 = st.columns(3)
                c1.metric("원문 길이", f"{len(text):,}")
                c2.metric("예상 청크 수", len(chunks))
                c3.metric("평균 길이", f"{(sum(c['length'] for c in chunks) / len(chunks)):.0f}" if chunks else "0")
                st.markdown("#### 청크 미리보기")
                for idx, chunk in enumerate(chunks[:12], start=1):
                    with st.expander(f"{idx}. {chunk['title']} · {chunk['length']} chars", expanded=(idx == 1)):
                        st.write(chunk["content"])
            else:
                st.info("사용 가능한 규정 파일이 없습니다.")
        st.markdown('</div>', unsafe_allow_html=True)


def render_demo_control_page() -> None:
    render_page_header("시연 데이터 제어", "대표 시나리오를 즉시 생성하고, 생성된 전표를 새로고침 이후에도 다시 확인할 수 있도록 DB에 저장합니다.")
    scenario_data = _get("/api/v1/demo/scenarios").get("items") or []
    seeded_data = _get("/api/v1/demo/seeded").get("items") or []

    top, bottom = st.columns([0.60, 0.40])
    with top:
        st.markdown('<div class="mt-card">', unsafe_allow_html=True)
        st.markdown("#### 대표 시나리오")
        if not scenario_data:
            st.info("시나리오 데이터가 없습니다.")
        else:
            count = st.slider("생성 건수", min_value=1, max_value=20, value=5)
            card_cols = st.columns(2)
            for idx, scenario in enumerate(scenario_data):
                with card_cols[idx % 2]:
                    with stylable_container(
                        key=f"scenario_card_{scenario['scenario']}",
                        css_styles="""
                        {
                          background: rgba(255,255,255,0.98);
                          border: 1px solid #e5e7eb;
                          border-radius: 18px;
                          padding: 0.9rem 1rem 1rem 1rem;
                          margin-bottom: 0.8rem;
                          box-shadow: 0 8px 22px rgba(15,23,42,0.04);
                          overflow: hidden;
                          box-sizing: border-box;
                        }
                        div[data-testid="stButton"] {
                          width: 100%;
                          padding: 0 0.1rem;
                          box-sizing: border-box;
                        }
                        div[data-testid="stButton"] > button {
                          width: 100% !important;
                          max-width: 100% !important;
                          box-sizing: border-box !important;
                          margin: 0 !important;
                        }
                        """,
                    ):
                        st.markdown('<div class="mt-scenario-card">', unsafe_allow_html=True)
                        st.markdown(f'<div class="mt-scenario-title"><strong>{scenario["label"]}</strong></div>', unsafe_allow_html=True)
                        st.markdown(f'<div class="mt-scenario-desc">{scenario["description"]}</div>', unsafe_allow_html=True)
                        st.markdown(
                            f"""
                            <div class="mt-scenario-meta">
                              <span class="mt-meta-pill">위험유형 {scenario["risk_type"]}</span>
                              <span class="mt-meta-pill">금액 {scenario["amount_range"][0]:,}~{scenario["amount_range"][1]:,} KRW</span>
                              <span class="mt-meta-pill">기준 {scenario["day_mode"]}</span>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                        if st.button(f"{scenario['label']} 생성", key=f"seed_{scenario['scenario']}", use_container_width=True, type="primary"):
                            out = _post("/api/v1/demo/seed", params={"scenario": scenario["scenario"], "count": int(count)})
                            st.success(f"생성 완료: {out['scenario']} / {out['inserted']}건")
                            st.rerun()
                        st.markdown('</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
    with bottom:
        st.markdown('<div class="mt-card">', unsafe_allow_html=True)
        st.markdown("#### 일괄 제어")
        st.caption("모든 시연 데이터는 `DEMO-*` 키로 저장됩니다.")
        if st.button("시연 데이터 전체 삭제", use_container_width=True):
            out = _delete("/api/v1/demo/seed")
            st.warning(f"삭제 완료: {out.get('deleted', 0)}건")
            st.rerun()
        st.markdown("#### 현재 적재 현황")
        st.metric("저장된 시연 전표", len(seeded_data))
        grouped = {}
        for row in seeded_data:
            grouped[row.get("scenario") or "-"] = grouped.get(row.get("scenario") or "-", 0) + 1
        for key, value in grouped.items():
            st.caption(f"{key}: {value}건")
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="mt-card" style="margin-top:12px;">', unsafe_allow_html=True)
    st.markdown("#### 생성된 시연 전표")
    if seeded_data:
        for item in seeded_data[:50]:
            with stylable_container(
                key=f"seeded_{item['voucher_key']}",
                css_styles="""
                {
                  background: rgba(255,255,255,0.98);
                  border: 1px solid #e5e7eb;
                  border-radius: 16px;
                  padding: 0.85rem 1rem;
                  margin-bottom: 0.6rem;
                  box-shadow: 0 8px 22px rgba(15,23,42,0.04);
                  overflow: hidden;
                }
                """,
            ):
                st.markdown('<div class="mt-seeded-card">', unsafe_allow_html=True)
                st.markdown(_case_type_badge(item.get("risk_type")) + _status_badge("READY"), unsafe_allow_html=True)
                st.markdown(f"**{item.get('title') or '-'}**")
                st.caption(
                    f"{item.get('voucher_key')} · {item.get('scenario')} · {_fmt_num(item.get('amount'))} {item.get('currency') or ''}"
                )
                st.caption(
                    f"hr={item.get('hr_status') or '-'} · mcc={item.get('mcc_code') or '-'} · budgetExceeded={item.get('budget_exceeded')}"
                )
                st.markdown('</div>', unsafe_allow_html=True)
    else:
        st.info("저장된 시연 전표가 없습니다.")
    st.markdown('</div>', unsafe_allow_html=True)


def render_sidebar() -> str:
    with st.sidebar:
        st.markdown(
            '<div class="mt-sidebar-title-row"><div class="mt-sidebar-title">Arua Agent AI POC</div></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="mt-sidebar-sub">Agentic AI workspace, studio, policy library, and demo controls.</div>',
            unsafe_allow_html=True,
        )
        options = [item[0] for item in MENU_OPTIONS]
        icons = ["grid", "cpu", "journal-text", "sliders"]
        default_index = options.index(st.session_state.get("mt_menu", options[0])) if st.session_state.get("mt_menu", options[0]) in options else 0
        selected = option_menu(
            menu_title=None,
            options=options,
            icons=icons,
            default_index=default_index,
            key="mt_menu_option",
            styles={
                "container": {
                    "padding": "0!important",
                    "background-color": "transparent",
                },
                "icon": {"color": "#93c5fd", "font-size": "18px"},
                "nav-link": {
                    "font-size": "14px",
                    "font-weight": "700",
                    "text-align": "left",
                    "margin": "4px 0",
                    "padding": "12px 14px",
                    "border-radius": "12px",
                    "color": "#e5e7eb",
                    "background-color": "transparent",
                },
                "nav-link-selected": {
                    "background-color": "#2563eb",
                    "color": "#ffffff",
                    "font-weight": "800",
                },
            },
        )
        st.session_state["mt_menu"] = selected
        st.divider()
        with st.expander("개발 옵션", expanded=False):
            st.checkbox("디버그 보기 표시", key="mt_debug_mode")
            st.caption("워크벤치 본문에서는 표시 모드를 제거했습니다. 디버그 표시는 여기서만 켭니다.")
            st.code(
                f"AGENT_RUNTIME_MODE={settings.agent_runtime_mode}\nENABLE_MULTI_AGENT={settings.enable_multi_agent}\nAPI_BASE_URL={settings.api_base_url}",
                language="bash",
            )
        return selected


selected_menu = render_sidebar()

if selected_menu == "AI 워크스페이스":
    render_ai_workspace_page()
elif selected_menu == "에이전트 스튜디오":
    render_agent_studio_page()
elif selected_menu == "규정문서 라이브러리":
    render_rag_library_page()
else:
    render_demo_control_page()
