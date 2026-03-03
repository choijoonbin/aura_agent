from __future__ import annotations

from datetime import datetime
from typing import Any

import streamlit as st
from graphviz import Digraph
from streamlit_extras.stylable_container import stylable_container


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
        [data-testid="stAppViewContainer"] > .main {{ background: transparent !important; }}
        [data-testid="block-container"] {{ padding-top: 1.25rem !important; padding-bottom: 1.25rem !important; max-width: 100% !important; }}
        .stApp, .stApp p, .stApp li, .stApp label, .stApp span, .stApp div, .stApp small, .stApp strong, .stApp em, .stApp code {{ color: var(--mt-text-strong); }}
        .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6 {{ color: var(--mt-text-strong); }}
        header[data-testid="stHeader"] {{ background: transparent !important; height: 0 !important; min-height: 0 !important; border: none !important; box-shadow: none !important; }}
        header[data-testid="stHeader"] * {{ display: none !important; }}
        section[data-testid="stSidebar"] {{
          background: linear-gradient(180deg, #0f172a 0%, #111827 100%) !important;
          border-right: 1px solid rgba(255,255,255,0.08) !important;
          top: 0 !important;
          padding-top: 0 !important;
          z-index: 999900 !important;
          min-width: {sidebar_width} !important;
          max-width: {sidebar_width} !important;
        }}
        section[data-testid="stSidebar"] > div:first-child {{ padding-top: {sidebar_padding_top} !important; }}
        section[data-testid="stSidebar"] * {{ color: #e5e7eb; }}
        section[data-testid="stSidebar"] .stSelectbox [data-baseweb="select"] > div,
        section[data-testid="stSidebar"] .stSelectbox [data-baseweb="select"] {{
          background-color: rgba(255,255,255,0.07) !important;
          border-color: rgba(255,255,255,0.18) !important;
          border-radius: 10px !important;
        }}
        section[data-testid="stSidebar"] .stSelectbox [data-baseweb="select"] *, section[data-testid="stSidebar"] .stSelectbox svg path {{ color: #e5e7eb !important; fill:#e5e7eb !important; background-color: transparent !important; }}
        section[data-testid="stSidebar"] code {{ color: #93c5fd !important; background: rgba(255,255,255,0.08) !important; }}
        section[data-testid="stSidebar"] button[data-testid="baseButton-headerNoPadding"], [data-testid="collapsedControl"], [data-testid="stSidebarCollapsedControl"], [data-testid="stSidebarCollapseButton"], [data-testid="stToolbar"], #MainMenu {{ display: none !important; }}
        .mt-sidebar-title {{ font-size: 1.15rem; font-weight: 800; color: white; margin-bottom: 0.2rem; }}
        .mt-sidebar-sub {{ font-size: 0.82rem; color: #94a3b8; margin-bottom: 1rem; }}
        .mt-sidebar-title-row {{ display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; margin-bottom: 0.35rem; }}
        .nav-link, .nav-link * {{ color: #e5e7eb !important; }}
        .nav-link-selected, .nav-link-selected * {{ color: #ffffff !important; }}
        .mt-page-title {{ font-size: 1.8rem; font-weight: 800; color: #0f172a; }}
        .mt-page-sub {{ font-size: 0.96rem; color: var(--mt-text-soft); margin-top: 4px; line-height: 1.55; }}
        .mt-card {{ padding: 16px 18px; border-radius: 18px; border: 1px solid var(--mt-border); background: rgba(255,255,255,0.95); box-shadow: 0 12px 30px rgba(15,23,42,0.04); height: 100%; }}
        .mt-card-quiet {{ padding: 14px 16px; border-radius: 16px; border: 1px solid var(--mt-border); background: #fff; box-shadow: 0 8px 22px rgba(15,23,42,0.04); }}
        .mt-kpi {{ padding: 16px 18px; border-radius: 18px; border: 1px solid var(--mt-border); background: rgba(255,255,255,0.96); box-shadow: 0 10px 24px rgba(15,23,42,0.04); }}
        .mt-kpi-label {{ font-size: 0.82rem; color: var(--mt-text-soft); font-weight: 700; }}
        .mt-kpi-value {{ font-size: 2rem; font-weight: 800; color: #0f172a; line-height: 1.15; }}
        .mt-kpi-foot {{ font-size: 0.82rem; color: var(--mt-text-soft); }}
        .mt-section-title {{ font-size: 1.02rem; font-weight: 800; color: #0f172a; margin-bottom: 10px; }}
        .mt-case-title {{ font-size: 1rem; font-weight: 800; color: var(--mt-text-strong); line-height: 1.35; }}
        .mt-case-sub {{ font-size: 0.84rem; color: var(--mt-text-soft); margin-top: 4px; }}
        .mt-badge {{ display:inline-block; padding:4px 10px; border-radius:999px; font-size:0.72rem; font-weight:700; border:1px solid var(--mt-border); background:#fff; color:#334155; margin-right:6px; margin-bottom:6px; }}
        .mt-badge-blue {{ background:#eff6ff; color:#1d4ed8; border-color:#bfdbfe; }}
        .mt-badge-red {{ background:#fef2f2; color:#dc2626; border-color:#fecaca; }}
        .mt-badge-amber {{ background:#fffbeb; color:#d97706; border-color:#fde68a; }}
        .mt-badge-green {{ background:#ecfdf5; color:#059669; border-color:#a7f3d0; }}
        .mt-stream-shell {{ background: radial-gradient(circle at 1px 1px, rgba(15,23,42,0.10) 1px, transparent 0); background-size: 14px 14px; background-color:#f8fafc; border:1px dashed #dbe2ea; border-radius:18px; padding:14px; }}
        .mt-stream-card {{ padding: 14px 16px; border-radius: 16px; border: 1px solid var(--mt-border); background: #fff; margin-bottom: 10px; }}
        .mt-mini {{ font-size: 0.78rem; color: var(--mt-text-soft); }}
        div[data-testid="stTabs"] button[role="tab"] {{ font-weight: 700; }}
        .stButton button[kind="primary"], .stButton button[kind="primary"] * {{ color: #ffffff !important; background: #2563eb !important; border-color: #2563eb !important; }}
        .stButton button[kind="secondary"], .stButton button[kind="secondary"] * {{ color: #0f172a !important; background: #ffffff !important; border-color: #cbd5e1 !important; }}
        .stButton button, .stDownloadButton button {{ border-radius: 12px !important; font-weight: 700 !important; box-shadow: none !important; }}
        </style>
    """
    st.markdown(css_template.format(sidebar_width=sidebar_width, sidebar_padding_top=sidebar_padding_top), unsafe_allow_html=True)


def fmt_num(value: Any) -> str:
    try:
        return f"{float(value):,.0f}"
    except Exception:
        return "-"


def fmt_dt(value: Any) -> str:
    if not value:
        return "-"
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return text


def status_badge(status: str | None) -> str:
    value = str(status or "").upper() or "NEW"
    if value in {"NEW", "READY"}:
        return '<span class="mt-badge mt-badge-blue">신규</span>'
    if value in {"IN_REVIEW", "REVIEW_REQUIRED", "REVIEW_AFTER_HITL"}:
        return '<span class="mt-badge mt-badge-amber">검토중</span>'
    if value in {"HITL_REQUIRED", "FAILED"}:
        return '<span class="mt-badge mt-badge-red">주의</span>'
    if value in {"RESOLVED", "COMPLETED", "OK"}:
        return '<span class="mt-badge mt-badge-green">완료</span>'
    return f'<span class="mt-badge">{value}</span>'


def severity_badge(severity: str | None) -> str:
    value = str(severity or "").upper() or "LOW"
    if value in {"CRITICAL", "HIGH"}:
        return '<span class="mt-badge mt-badge-red">높음</span>'
    if value == "MEDIUM":
        return '<span class="mt-badge mt-badge-blue">중간</span>'
    if value == "LOW":
        return '<span class="mt-badge mt-badge-green">낮음</span>'
    return f'<span class="mt-badge">{value}</span>'


def case_type_badge(case_type: str | None) -> str:
    value = str(case_type or "").upper() or "NORMAL_BASELINE"
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


def render_page_header(title: str, subtitle: str, right_html: str | None = None) -> None:
    with stylable_container(key=f"page_header_{title}", css_styles="""{padding: 20px 24px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.92); border-radius: 20px; box-shadow: 0 12px 40px rgba(15,23,42,0.05); margin-bottom: 16px;}"""):
        cols = st.columns([0.78, 0.22])
        with cols[0]:
            st.markdown(f'<div class="mt-page-title">{title}</div>', unsafe_allow_html=True)
            if subtitle:
                st.markdown(f'<div class="mt-page-sub">{subtitle}</div>', unsafe_allow_html=True)
        with cols[1]:
            if right_html:
                st.markdown(right_html, unsafe_allow_html=True)


def render_kpi_card(label: str, value: str, foot: str = "") -> None:
    st.markdown(f'<div class="mt-kpi"><div class="mt-kpi-label">{label}</div><div class="mt-kpi-value">{value}</div><div class="mt-kpi-foot">{foot}</div></div>', unsafe_allow_html=True)


def draw_agent_graph() -> Digraph:
    g = Digraph("finance_aura_agentic")
    g.attr(rankdir="LR")
    g.attr("graph", bgcolor="transparent", pad="0.25", nodesep="0.34", ranksep="0.42")
    g.attr("node", shape="box", style="rounded,filled", color="#a78bfa", fillcolor="#f5f3ff", fontname="Helvetica", fontsize="12")
    g.attr("edge", color="#94a3b8", penwidth="1.2")
    for key, label in [("start", "START"), ("intake", "Intake Agent"), ("planner", "Planner Agent"), ("executor", "Execute Agent"), ("critic", "Critic Agent"), ("verifier", "Verifier Agent"), ("reporter", "Reporter Agent"), ("finalizer", "Finalizer"), ("end", "END")]:
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
    for node in [("execute", "execute"), ("holiday", "holiday_compliance_probe"), ("budget", "budget_risk_probe"), ("merchant", "merchant_risk_probe"), ("document", "document_evidence_probe"), ("policy", "policy_rulebook_probe"), ("legacy", "legacy_aura_deep_audit"), ("score", "score_breakdown")]:
        g.node(*node)
    for target in ["holiday", "budget", "merchant", "document", "policy"]:
        g.edge("execute", target)
    g.edge("execute", "legacy", label="conditional")
    for source in ["holiday", "budget", "merchant", "document", "policy", "legacy"]:
        g.edge(source, "score")
    return g


def render_graph_image(title: str, image_bytes: bytes | None, fallback_graph: Digraph, caption: str) -> None:
    st.markdown(f"**{title}**")
    _, center, _ = st.columns([0.08, 0.84, 0.08])
    with center:
        if image_bytes:
            st.image(image_bytes, use_container_width=False, width=520)
        else:
            st.graphviz_chart(fallback_graph, use_container_width=True)
        st.caption(caption)
