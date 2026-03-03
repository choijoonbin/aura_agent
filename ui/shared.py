from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st
from graphviz import Digraph
from streamlit_extras.stylable_container import stylable_container


def inject_css() -> None:
    sidebar_width = "280px"
    sidebar_padding_top = "0.4rem"
    css_template = """
        <style>
        :root {{
          --mt-primary: #2563eb;
          --mt-primary-soft: #dbeafe;
          --mt-border: #e5e7eb;
          --mt-border-strong: #cbd5e1;
          --mt-bg-soft: #f8fafc;
          --mt-bg-page: #f1f5f9;
          --mt-surface: #ffffff;
          --mt-text-soft: #64748b;
          --mt-text-strong: #0f172a;
          --mt-danger-soft: #fef2f2;
          --mt-success-soft: #ecfdf5;
          --mt-warning-soft: #fffbeb;
        }}
        .stApp {{ background: linear-gradient(180deg, #f8fafc 0%, #eef2ff 100%); padding-top: 52px !important; }}
        [data-testid="stAppViewContainer"] > .main {{ background: transparent !important; }}
        [data-testid="block-container"] {{ padding-top: 0.75rem !important; padding-bottom: 1.25rem !important; max-width: 100% !important; }}
        .stApp, .stApp p, .stApp li, .stApp label, .stApp span, .stApp div, .stApp small, .stApp strong, .stApp em, .stApp code {{ color: var(--mt-text-strong); }}
        .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6 {{ color: var(--mt-text-strong); }}
        /* 헤더·사이드바 동일 색상 */
        --mt-header-bg: linear-gradient(180deg, #0f172a 0%, #111827 100%);
        header[data-testid="stHeader"] {{ background: var(--mt-header-bg) !important; border: none !important; box-shadow: none !important; padding-top: 0 !important; z-index: 999999 !important; }}
        header[data-testid="stHeader"] *, header[data-testid="stHeader"] a, header[data-testid="stHeader"] button, header[data-testid="stHeader"] span {{ color: #e5e7eb !important; fill: #e5e7eb !important; }}
        header[data-testid="stHeader"] a:hover {{ color: #fff !important; }}
        header[data-testid="stHeader"] button {{ background: transparent !important; border-color: rgba(255,255,255,0.25) !important; }}
        header[data-testid="stHeader"] button:hover {{ background: rgba(255,255,255,0.1) !important; color: #fff !important; }}
        header[data-testid="stHeader"] svg {{ fill: #e5e7eb !important; }}
        .mt-app-header {{ position: fixed !important; top: 0 !important; left: 0 !important; right: 140px !important; height: 52px !important; display: flex !important; align-items: center !important; padding: 0 24px !important; background: var(--mt-header-bg) !important; color: #fff !important; font-weight: 800 !important; font-size: 1.15rem !important; letter-spacing: -0.02em !important; z-index: 1000001 !important; box-shadow: 0 1px 0 rgba(255,255,255,0.08) !important; pointer-events: auto !important; }}
        section[data-testid="stSidebar"] {{
          background: linear-gradient(180deg, #0f172a 0%, #111827 100%) !important;
          border-right: 1px solid rgba(255,255,255,0.08) !important;
          top: 52px !important;
          padding-top: 0 !important;
          z-index: 999900 !important;
          min-width: {sidebar_width} !important;
          max-width: {sidebar_width} !important;
        }}
        section[data-testid="stSidebar"] > div {{ padding-top: {sidebar_padding_top} !important; }}
        section[data-testid="stSidebar"] div {{ background: transparent !important; }}
        section[data-testid="stSidebar"] * {{ color: #e5e7eb !important; }}
        [data-testid="stSidebarCollapseButton"], [data-testid="stSidebarHeader"], [data-testid="collapsedControl"] {{ display: none !important; }}
        button[data-testid="baseButton-headerNoPadding"], button[kind="headerNoPadding"] {{ display: none !important; }}
        .mt-page-title {{ font-size: 1.6rem; font-weight: 800; color: #0f172a; letter-spacing: -0.02em; line-height: 1.2; }}
        .mt-page-sub {{ font-size: 0.9rem; color: var(--mt-text-soft); margin-top: 4px; line-height: 1.45; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }}
        .mt-card {{ padding: 18px 20px; border-radius: 20px; border: 1px solid var(--mt-border); background: rgba(255,255,255,0.96); box-shadow: 0 12px 30px rgba(15,23,42,0.05); height: 100%; }}
        .mt-card-quiet {{ padding: 14px 16px; border-radius: 16px; border: 1px solid var(--mt-border); background: #fff; box-shadow: 0 8px 22px rgba(15,23,42,0.04); }}
        .mt-panel-header {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:14px; }}
        .mt-panel-title {{ font-size:1.02rem; font-weight:800; color:#0f172a; letter-spacing:-0.01em; }}
        .mt-panel-sub {{ font-size:0.84rem; color:#64748b; line-height:1.55; margin-top:4px; }}
        .mt-kpi {{ padding: 18px 20px; border-radius: 18px; border: 1px solid var(--mt-border); background: rgba(255,255,255,0.98); box-shadow: 0 10px 24px rgba(15,23,42,0.05); min-height: 132px; }}
        .mt-kpi-label {{ font-size: 0.82rem; color: var(--mt-text-soft); font-weight: 800; text-transform: uppercase; letter-spacing: 0.04em; }}
        .mt-kpi-value {{ font-size: 2.15rem; font-weight: 800; color: #0f172a; line-height: 1.1; margin-top: 10px; }}
        .mt-kpi-foot {{ font-size: 0.84rem; color: var(--mt-text-soft); margin-top: 8px; line-height: 1.5; }}
        .mt-section-card {{ padding: 18px 20px; border-radius: 22px; border: 1px solid var(--mt-border); background: rgba(255,255,255,0.96); box-shadow: 0 12px 30px rgba(15,23,42,0.05); }}
        .mt-section-card-tight {{ padding: 14px 16px; border-radius: 18px; border: 1px solid var(--mt-border); background: rgba(255,255,255,0.98); box-shadow: 0 8px 22px rgba(15,23,42,0.04); }}
        .mt-case-title {{ font-size: 1rem; font-weight: 800; color: var(--mt-text-strong); line-height: 1.35; }}
        .mt-case-sub {{ font-size: 0.84rem; color: var(--mt-text-soft); margin-top: 4px; line-height: 1.55; }}
        .mt-case-meta {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-top:8px; }}
        .mt-badge {{ display:inline-block; padding:4px 10px; border-radius:999px; font-size:0.72rem; font-weight:700; border:1px solid var(--mt-border); background:#fff; color:#334155; margin-right:6px; margin-bottom:6px; }}
        .mt-badge-blue {{ background:#eff6ff; color:#1d4ed8; border-color:#bfdbfe; }}
        .mt-badge-red {{ background:#fef2f2; color:#dc2626; border-color:#fecaca; }}
        .mt-badge-amber {{ background:#fffbeb; color:#d97706; border-color:#fde68a; }}
        .mt-badge-green {{ background:#ecfdf5; color:#059669; border-color:#a7f3d0; }}
        .mt-meta-pill {{ display:inline-flex; align-items:center; gap:6px; padding:6px 10px; border-radius:999px; background:#f8fafc; border:1px solid #e2e8f0; color:#334155; font-size:0.76rem; font-weight:700; margin-right:8px; margin-bottom:8px; }}
        .mt-divider {{ height:1px; background:linear-gradient(90deg, rgba(203,213,225,0.15), rgba(203,213,225,0.9), rgba(203,213,225,0.15)); margin:14px 0 16px 0; }}
        .mt-stream-shell {{ background: radial-gradient(circle at 1px 1px, rgba(15,23,42,0.10) 1px, transparent 0); background-size: 14px 14px; background-color:#f8fafc; border:1px dashed #dbe2ea; border-radius:18px; padding:14px; }}
        .mt-stream-card {{ padding: 14px 16px; border-radius: 16px; border: 1px solid var(--mt-border); background: #fff; margin-bottom: 10px; }}
        .mt-legend {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin: 8px 0 14px 0; }}
        .mt-legend-item {{ display:inline-flex; align-items:center; gap:8px; padding:8px 10px; border-radius:12px; background:#fff; border:1px solid #e2e8f0; font-size:0.78rem; color:#334155; }}
        .mt-legend-dot {{ width:10px; height:10px; border-radius:999px; display:inline-block; }}
        .mt-grid-2 {{ display:grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap:12px; }}
        .mt-grid-3 {{ display:grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap:12px; }}
        .mt-mini {{ font-size: 0.78rem; color: var(--mt-text-soft); }}
        .mt-chip-row {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-top:10px; }}
        .mt-caption-strong {{ font-size:0.8rem; color:#475569; font-weight:700; }}
        .mt-hero-title {{ font-size: 1.25rem; font-weight: 800; color:#0f172a; letter-spacing:-0.02em; }}
        .mt-hero-sub {{ font-size: 0.9rem; color:#64748b; line-height:1.6; }}
        .mt-empty-box {{ padding: 20px 22px; border-radius: 18px; border: 1px dashed #cbd5e1; background: #f8fafc; color:#475569; }}
        .mt-kv-grid {{ display:grid; grid-template-columns: auto 1fr; gap: 6px 16px; align-items: baseline; font-size: 0.85rem; margin-top: 10px; margin-bottom: 12px; }}
        .mt-kv-key {{ color: var(--mt-text-soft); font-weight: 600; min-width: 72px; }}
        .mt-kv-value {{ color: var(--mt-text-strong); font-weight: 500; }}
        div[data-testid="stTabs"] button[role="tab"] {{ font-weight: 700; padding-top: 0.6rem !important; padding-bottom: 0.7rem !important; }}
        div[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {{ color: #2563eb !important; }}
        .stButton button[kind="primary"], .stButton button[kind="primary"] * {{ color: #ffffff !important; background: #2563eb !important; border-color: #2563eb !important; }}
        .stButton button[kind="secondary"], .stButton button[kind="secondary"] * {{ color: #0f172a !important; background: #ffffff !important; border-color: #cbd5e1 !important; }}
        .stButton button, .stDownloadButton button {{ border-radius: 12px !important; font-weight: 700 !important; box-shadow: none !important; min-height: 42px !important; }}
        div[data-baseweb="select"] > div {{ background-color: #ffffff !important; border: 1px solid #e5e7eb !important; border-radius: 8px !important; color: #0f172a !important; }}
        div[data-baseweb="select"] svg {{ fill: #475569 !important; }}
        div[data-baseweb="popover"] {{ z-index: 999999 !important; }}
        div[data-baseweb="popover"] li {{ color: #0f172a !important; background: #fff !important; }}
        div[data-baseweb="popover"] li:hover {{ background: #eff6ff !important; }}
        .stChatMessage {{ background: transparent !important; }}
        .stMetric label, .stMetric div {{ color:#0f172a !important; }}
        .stTabs [data-baseweb="tab-panel"] {{ padding-top: 1rem !important; }}
        .mt-demo-section {{ margin-top: 1.5rem; padding-top: 1.25rem; border-top: 1px solid var(--mt-border); }}
        .mt-demo-section:first-of-type {{ margin-top: 0; padding-top: 0; border-top: none; }}
        .mt-demo-section-title {{ font-size: 1.1rem; font-weight: 800; color: #0f172a; letter-spacing: -0.02em; margin-bottom: 0.35rem; }}
        .mt-demo-section-sub {{ font-size: 0.82rem; color: var(--mt-text-soft); line-height: 1.5; }}
        .mt-demo-scenario-card {{ background: #fff; border: 1px solid var(--mt-border); border-radius: 14px; padding: 12px 14px; box-shadow: 0 4px 14px rgba(15,23,42,0.04); box-sizing: border-box; overflow: hidden; min-height: 1px; }}
        .mt-demo-scenario-title {{ font-size: 0.9rem; font-weight: 700; color: #0f172a; line-height: 1.3; margin-bottom: 4px; }}
        .mt-demo-scenario-desc {{ font-size: 0.75rem; color: #64748b; line-height: 1.4; margin-bottom: 8px; }}
        .mt-demo-panel {{ background: rgba(255,255,255,0.98); border: 1px solid var(--mt-border); border-radius: 16px; padding: 1rem 1.1rem; box-shadow: 0 8px 22px rgba(15,23,42,0.05); }}
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


def fmt_dt_korea(value: Any) -> str:
    """Format as yyyy-mm-dd hh:mm:ss in Korean time (KST, Asia/Seoul)."""
    if not value:
        return "-"
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        kst = dt.astimezone(ZoneInfo("Asia/Seoul"))
        return kst.strftime("%Y-%m-%d %H:%M:%S")
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


# 코드값 → 사용자용 코드명 (케이스 카드·에이전트 스트림 표시)
MCC_DISPLAY_NAMES: dict[str, str] = {
    "5812": "식당/레스토랑",
    "5813": "주점/바",
    "5814": "패스트푸드",
    "5811": "케이터링",
    "7992": "골프장",
    "7996": "놀이공원",
    "7997": "클럽/체육시설",
    "7011": "호텔/숙박",
    "4722": "여행사",
    "5912": "약국",
}
HR_STATUS_DISPLAY_NAMES: dict[str, str] = {
    "WORK": "근무",
    "WORKING": "근무",
    "LEAVE": "휴가/결근",
    "OFF": "휴무",
    "VACATION": "휴가",
    "BUSINESS_TRIP": "출장",
}


def mcc_display_name(mcc_code: str | None) -> str:
    if not mcc_code:
        return "-"
    return MCC_DISPLAY_NAMES.get(str(mcc_code).strip(), f"MCC {mcc_code}")


def hr_status_display_name(hr_status: str | None) -> str:
    if not hr_status:
        return "-"
    return HR_STATUS_DISPLAY_NAMES.get(str(hr_status).strip().upper(), hr_status)


def budget_exceeded_display(exceeded: bool | None) -> str:
    if exceeded is None:
        return "-"
    return "초과" if exceeded else "정상"


def status_display_name(status: str | None) -> str:
    if not status:
        return "-"
    labels = {"NEW": "신규", "PENDING_EXPLANATION": "소명 대기", "IN_REVIEW": "검토 중", "COMPLETED": "완료", "RESOLVED": "해결"}
    return labels.get(str(status).upper(), status)


def severity_display_name(severity: str | None) -> str:
    if not severity:
        return "-"
    labels = {"CRITICAL": "심각", "HIGH": "높음", "MEDIUM": "중간", "LOW": "낮음"}
    return labels.get(str(severity).upper(), severity)


def case_type_display_name(case_type: str | None) -> str:
    if not case_type or str(case_type).upper() == "UNSCREENED":
        return "미분류"
    labels = {
        "HOLIDAY_USAGE": "휴일 사용 의심",
        "LIMIT_EXCEED": "한도 초과 의심",
        "PRIVATE_USE_RISK": "사적 사용 위험",
        "UNUSUAL_PATTERN": "비정상 패턴",
        "NORMAL_BASELINE": "정상 기준선",
        "SPLIT_PAYMENT": "분할 결제 의심",
        "DUPLICATE_SUSPECT": "중복 결제 의심",
    }
    return labels.get(str(case_type).upper(), case_type)


def case_type_badge(case_type: str | None) -> str:
    value = str(case_type or "").upper() or "UNSCREENED"
    labels = {
        "HOLIDAY_USAGE": "휴일 사용 의심",
        "LIMIT_EXCEED": "한도 초과 의심",
        "PRIVATE_USE_RISK": "사적 사용 위험",
        "SPLIT_PAYMENT": "분할 결제 의심",
        "DUPLICATE_SUSPECT": "중복 결제 의심",
        "UNUSUAL_PATTERN": "비정상 패턴",
        "NORMAL_BASELINE": "정상 기준선",
        "UNSCREENED": "미분류",
        "DEFAULT": "기본 분류",
    }
    label = labels.get(value, value)
    if value in {"HOLIDAY_USAGE", "LIMIT_EXCEED", "PRIVATE_USE_RISK", "SPLIT_PAYMENT", "DUPLICATE_SUSPECT", "UNUSUAL_PATTERN"}:
        return f'<span class="mt-badge mt-badge-blue">{label}</span>'
    if value == "NORMAL_BASELINE":
        return f'<span class="mt-badge mt-badge-green">{label}</span>'
    if value == "UNSCREENED":
        return f'<span class="mt-badge" style="background:#f1f5f9;color:#64748b;">미분류</span>'
    return f'<span class="mt-badge">{label}</span>'


def render_page_header(title: str, subtitle: str, right_html: str | None = None) -> None:
    with stylable_container(
        key=f"page_header_{title}",
        css_styles="""
        {
          padding: 14px 20px;
          border: 1px solid #e5e7eb;
          background: rgba(255,255,255,0.94);
          border-radius: 18px;
          box-shadow: 0 8px 28px rgba(15,23,42,0.05);
          margin-bottom: 12px;
        }
        """,
    ):
        cols = st.columns([0.78, 0.22])
        with cols[0]:
            st.markdown(f'<div class="mt-page-title">{title}</div>', unsafe_allow_html=True)
            if subtitle:
                st.markdown(f'<div class="mt-page-sub">{subtitle}</div>', unsafe_allow_html=True)
        with cols[1]:
            if right_html:
                st.markdown(right_html, unsafe_allow_html=True)


def render_panel_header(title: str, subtitle: str = "") -> None:
    subtitle_html = f'<div class="mt-panel-sub">{subtitle}</div>' if subtitle else ''
    st.markdown(
        f'<div class="mt-panel-header"><div><div class="mt-panel-title">{title}</div>{subtitle_html}</div></div>',
        unsafe_allow_html=True,
    )


def render_kpi_card(label: str, value: str, foot: str = "") -> None:
    st.markdown(f'<div class="mt-kpi"><div class="mt-kpi-label">{label}</div><div class="mt-kpi-value">{value}</div><div class="mt-kpi-foot">{foot}</div></div>', unsafe_allow_html=True)


def render_empty_state(message: str) -> None:
    st.markdown(f'<div class="mt-empty-box">{message}</div>', unsafe_allow_html=True)


def render_legend(items: list[tuple[str, str]]) -> None:
    html = ['<div class="mt-legend">']
    for color, label in items:
        html.append(
            f'<div class="mt-legend-item"><span class="mt-legend-dot" style="background:{color}"></span>{label}</div>'
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)


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
