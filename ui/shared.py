from __future__ import annotations

import html
import io
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import re

import matplotlib
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import streamlit as st

matplotlib.use("Agg")  # GUI 백엔드 없이 PNG 생성 (서버 환경)


def stylable_container(key: str, css_styles: str | list[str]):
    """streamlit_extras.stylable_container의 iframe-free 대체 구현.
    
    streamlit-extras 0.7.8은 내부적으로 container.html()을 사용해 iframe을 생성하고,
    브라우저가 'allow-scripts + allow-same-origin' iframe 경고를 초기 로드 시 1회 출력함.
    본 구현은 st.markdown()으로 CSS를 직접 주입해 iframe을 완전히 제거한다.
    Streamlit은 container(key=...)에 자동으로 st-key- 접두사를 붙이므로, key에는 접두사 없이 전달.
    """
    base_key = re.sub(r"[^a-zA-Z0-9_-]", "-", key.strip())
    class_name = f"st-key-{base_key}"
    if isinstance(css_styles, str):
        css_styles = [css_styles]
    style_parts = "".join(f"\n.{class_name} {s}" for s in css_styles)
    st.markdown(f"<style>{style_parts}\n</style>", unsafe_allow_html=True)
    return st.container(key=base_key)


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
        [data-testid="stAppViewContainer"] > .main {{ background: transparent !important; padding-top: 0 !important; }}
        section.main {{ padding-top: 0 !important; }}
        .main .block-container {{ padding-top: 0 !important; }}
        [data-testid="block-container"] {{ padding-top: 0 !important; padding-bottom: 1.25rem !important; max-width: 100% !important; }}
        [data-testid="block-container"] > div {{ padding-top: 0 !important; margin-top: 0 !important; }}
        [data-testid="block-container"] > div:first-child {{ margin-top: 0 !important; padding-top: 0 !important; }}
        [data-testid="block-container"] > [data-testid="stVerticalBlock"],
        [data-testid="block-container"] > div > [data-testid="stVerticalBlock"] {{ padding-top: 0 !important; margin-top: 0 !important; }}
        [data-testid="block-container"] > [data-testid="stVerticalBlock"] > [data-testid="element-container"]:first-child,
        [data-testid="block-container"] > div > [data-testid="stVerticalBlock"] > [data-testid="element-container"]:first-child {{ padding-top: 0 !important; margin-top: 0 !important; }}
        [data-testid="column"] {{ min-width: 0 !important; overflow: hidden !important; }}
        [data-testid="column"] > div {{ min-width: 0 !important; max-width: 100% !important; overflow: hidden !important; }}
        [data-testid="column"] [data-testid="stVerticalBlock"] {{ min-width: 0 !important; max-width: 100% !important; }}
        [data-testid="column"] .stButton {{ min-width: 0 !important; width: 100% !important; max-width: 100% !important; overflow: hidden !important; }}
        [data-testid="column"] .stButton > button {{ width: 100% !important; max-width: 100% !important; min-width: 0 !important; box-sizing: border-box !important; overflow: hidden !important; text-overflow: ellipsis !important; white-space: nowrap !important; }}
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
        body::before {{ content: "Aura Agentic AI"; position: fixed; top: 0; left: 0; right: 140px; height: 52px; display: flex; align-items: center; padding: 0 24px; background: linear-gradient(180deg, #0f172a 0%, #111827 100%); color: #fff; font-weight: 800; font-size: 1.15rem; letter-spacing: -0.02em; z-index: 1000001; box-shadow: 0 1px 0 rgba(255,255,255,0.08); pointer-events: none; }}
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
        .mt-panel-header {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:14px; min-width:0; max-width:100%; }}
        .mt-panel-header > div:first-child {{ min-width:0; flex:1 1 auto; }}
        .mt-panel-header .mt-panel-sub.mt-panel-trailing {{ white-space:nowrap; flex-shrink:0; text-align:right; font-size:0.8rem; }}
        /* 에이전트 대화 영역: trailing 텍스트가 카드 경계 안에서만 표시되도록 */
        [class*="st-key-workspace_chat_card"] {{ overflow: hidden !important; max-width: 100% !important; }}
        [class*="st-key-workspace_chat_card"] .mt-panel-header {{ overflow: visible !important; flex-wrap: wrap; gap: 4px 10px; align-items:flex-start; }}
        [class*="st-key-workspace_chat_card"] .mt-panel-sub.mt-panel-trailing {{
          white-space: normal !important;
          flex: 0 1 34% !important;
          max-width: calc(34% - 14px) !important;
          min-width: 140px !important;
          text-align: right !important;
          padding-right: 28px !important;
          margin-right: 2px !important;
          box-sizing: border-box !important;
          overflow: hidden !important;
          text-overflow: ellipsis !important;
          line-height: 1.35 !important;
          font-size: 0.78rem;
        }}
        .mt-panel-title {{ font-size:1.02rem; font-weight:800; color:#0f172a; letter-spacing:-0.01em; }}
        .mt-panel-sub {{ font-size:0.84rem; color:#64748b; line-height:1.55; margin-top:4px; }}
        .mt-kpi {{ padding: 18px 20px; border-radius: 18px; border: 1px solid var(--mt-border); background: rgba(255,255,255,0.98); box-shadow: 0 10px 24px rgba(15,23,42,0.05); min-height: 132px; overflow: hidden; max-width: 100%; box-sizing: border-box; }}
        .mt-kpi-label {{ font-size: 1.64rem; color: var(--mt-text-soft); font-weight: 800; text-transform: uppercase; letter-spacing: 0.04em; }}
        .mt-kpi-value {{ font-size: 2.15rem; font-weight: 800; color: #0f172a; line-height: 1.1; margin-top: 10px; }}
        .mt-kpi-foot {{ font-size: 0.84rem; color: var(--mt-text-soft); margin-top: 8px; line-height: 1.5; }}
        /* 규정문서 라이브러리 KPI 영역 4곳: 컨텐츠가 영역 우측을 벗어나지 않도록 */
        .mt-rag-kpi-card {{ overflow: hidden !important; max-width: 100% !important; box-sizing: border-box !important; }}
        .mt-rag-kpi-card .mt-rag-kpi-label, .mt-rag-kpi-card .mt-rag-kpi-value, .mt-rag-kpi-card .mt-rag-kpi-foot {{ overflow-wrap: break-word !important; word-break: break-word !important; max-width: 100% !important; min-width: 0 !important; }}
        .mt-rag-kpi-card > div {{ max-width: 100% !important; min-width: 0 !important; }}
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
        .mt-stream-shell {{ background: radial-gradient(circle at 1px 1px, rgba(15,23,42,0.10) 1px, transparent 0); background-size: 14px 14px; background-color:#f8fafc; border:1px dashed #dbe2ea; border-radius:18px; padding:14px; max-height: 54vh; overflow-y:auto; overflow-x:hidden; }}
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
        [data-testid="stExpanderDetails"] {{ background: #0f172a !important; color: #ffffff !important; }}
        [data-testid="stExpanderDetails"] *, [data-testid="stExpanderDetails"] pre, [data-testid="stExpanderDetails"] code, [data-testid="stExpanderDetails"] pre span {{ color: #ffffff !important; }}
        /* 펼쳤을 때 어두운 영역에서 라벨이 보이도록: 이전 타임라인 카드 보기, 판단 흐름 요약 */
        [data-testid="stExpander"]:has(.pipeline-wrapper) details[open] summary,
        [data-testid="stExpander"]:has(.pipeline-wrapper) details[open] summary * {{ color: #e2e8f0 !important; }}
        [data-testid="stExpander"]:has(.pipeline-wrapper) [aria-expanded="true"],
        [data-testid="stExpander"]:has(.pipeline-wrapper) [aria-expanded="true"] * {{ color: #e2e8f0 !important; }}
        [data-testid="stExpander"]:has([class*="st-key-process_story_"]) details[open] summary,
        [data-testid="stExpander"]:has([class*="st-key-process_story_"]) details[open] summary * {{ color: #e2e8f0 !important; }}
        /* 실시간 스트림/타임라인: 토글 라벨·스위치가 배경과 대비되어 보이도록 (키 기반 선택자로 Streamlit DOM에 확실히 적용) */
        [class*="st-key-tl_nested_"] label {{ color: #0f172a !important; font-weight: 600 !important; }}
        [class*="st-key-tl_nested_"] [role="switch"] {{ background: #e2e8f0 !important; border: 1px solid #94a3b8 !important; }}
        [class*="st-key-tl_nested_"] [role="switch"][aria-checked="true"] {{ background: #2563eb !important; border-color: #2563eb !important; }}
        [class*="st-key-tl_nested_"] [role="switch"] span {{ background: #fff !important; border: 1px solid #cbd5e1 !important; box-shadow: 0 1px 2px rgba(0,0,0,0.1) !important; }}
        [class*="st-key-tl_nested_"] [data-testid="stCheckbox"] label {{ color: #0f172a !important; font-weight: 600 !important; }}
        [class*="st-key-tl_nested_"] [data-testid="stCheckbox"] [role="checkbox"] {{ background: #e2e8f0 !important; border: 1px solid #94a3b8 !important; border-radius: 999px !important; }}
        /* 8단계 토글 행 앞 빈 공간 제거(Streamlit 내부 div padding/margin) */
        [class*="st-key-tl_nested_"] {{ padding-left: 0 !important; margin-left: 0 !important; }}
        [class*="st-key-tl_nested_"] > div {{ padding: 0 2px 0 0 !important; margin: 0 !important; }}
        [class*="st-key-tl_nested_"] [data-testid="stVerticalBlock"] > div {{ padding-left: 0 !important; margin-left: 0 !important; }}
        [class*="st-key-tl_nested_"] [data-testid="stCheckbox"], [class*="st-key-tl_nested_"] [role="switch"] {{ padding-left: 0 !important; margin-left: 0 !important; }}
        /* 8단계 마우스 오버 시 선택된 것처럼 색 강조 */
        [class*="st-key-tl_nested_"]:hover label {{ color: #2563eb !important; }}
        [class*="st-key-tl_nested_"] [data-testid="stCheckbox"]:hover label {{ color: #2563eb !important; }}
        /* timeline_shell 컨테이너 기준 선택자(호환용) */
        .st-key-timeline_shell label {{ color: #0f172a !important; font-weight: 600 !important; }}
        .st-key-timeline_shell [role="switch"] {{ background: #e2e8f0 !important; border: 1px solid #94a3b8 !important; margin-left: 0 !important; padding-left: 0 !important; }}
        .st-key-timeline_shell [data-testid="stCheckbox"] {{ padding-left: 0 !important; margin-left: 0 !important; }}
        .st-key-timeline_shell [data-testid="stVerticalBlock"] > div {{ padding-left: 0 !important; margin-left: 0 !important; }}
        .st-key-timeline_shell [data-testid="stCheckbox"]:hover label, .st-key-timeline_shell [data-testid="stVerticalBlock"]:has([data-testid="stCheckbox"]):hover label {{ color: #2563eb !important; }}
        /* 타임라인·판단/실행/발견 영역 텍스트가 영역 밖으로 나가지 않도록 */
        .st-key-timeline_shell p, .st-key-timeline_shell div, .st-key-timeline_shell span, .st-key-timeline_shell .thinking-content, .st-key-timeline_shell .thinking-content p {{ overflow-wrap: break-word !important; word-break: break-word !important; max-width: 100% !important; min-width: 0 !important; box-sizing: border-box !important; }}
        .st-key-timeline_shell .thinking-row .thinking-content {{ flex: 1 1 0; min-width: 0 !important; }}
        [data-testid="stExpander"]:has([class*="st-key-process_story_"]) [aria-expanded="true"],
        [data-testid="stExpander"]:has([class*="st-key-process_story_"]) [aria-expanded="true"] * {{ color: #e2e8f0 !important; }}
        [data-testid="stExpanderDetails"] pre, [data-testid="stExpanderDetails"] code {{ background: #0f172a !important; }}
        [data-testid="stExpanderDetails"] div[data-testid="stJson"] {{ color: #ffffff !important; background: #0f172a !important; }}
        [data-testid="stExpanderDetails"] .token {{ color: #ffffff !important; }}
        [data-testid="stExpanderDetails"] pre, [data-testid="stExpanderDetails"] pre * {{ background: #0f172a !important; color: #ffffff !important; }}
        [data-testid="stExpanderDetails"] div {{ background: #0f172a !important; }}
        div[data-baseweb="tab-panel"] [data-testid="stJson"], div[data-baseweb="tab-panel"] [data-testid="stJson"] * {{ background: #0f172a !important; color: #ffffff !important; }}
        div[data-baseweb="tab-panel"] [data-testid="stCode"], div[data-baseweb="tab-panel"] .stCode {{ background: #0f172a !important; color: #ffffff !important; }}
        div[data-baseweb="tab-panel"] [data-testid="stCode"] pre, div[data-baseweb="tab-panel"] [data-testid="stCode"] code, div[data-baseweb="tab-panel"] [data-testid="stCode"] pre * {{ background: #0f172a !important; color: #ffffff !important; }}
        div[data-baseweb="tab-panel"] .stCode pre, div[data-baseweb="tab-panel"] .stCode code, div[data-baseweb="tab-panel"] .stCode pre * {{ background: #0f172a !important; color: #ffffff !important; }}
        .stTabs [data-baseweb="tab-panel"] {{ padding-top: 1rem !important; }}
        .mt-demo-section {{ margin-top: 1.5rem; padding-top: 1.25rem; border-top: 1px solid var(--mt-border); }}
        .mt-demo-section:first-of-type {{ margin-top: 0; padding-top: 0; border-top: none; }}
        .mt-demo-section-title {{ font-size: 1.1rem; font-weight: 800; color: #0f172a; letter-spacing: -0.02em; margin-bottom: 0.35rem; }}
        .mt-demo-section-sub {{ font-size: 0.82rem; color: var(--mt-text-soft); line-height: 1.5; }}
        .mt-demo-scenario-card {{ background: #fff; border: 1px solid var(--mt-border); border-radius: 14px; padding: 12px 14px; box-shadow: 0 4px 14px rgba(15,23,42,0.04); box-sizing: border-box; overflow: hidden; min-height: 1px; }}
        .mt-demo-scenario-title {{ font-size: 0.9rem; font-weight: 700; color: #0f172a; line-height: 1.3; margin-bottom: 4px; }}
        .mt-demo-scenario-desc {{ font-size: 0.75rem; color: #64748b; line-height: 1.4; margin-bottom: 8px; }}
        .mt-demo-panel {{ background: rgba(255,255,255,0.98); border: 1px solid var(--mt-border); border-radius: 16px; padding: 1rem 1.1rem; box-shadow: 0 8px 22px rgba(15,23,42,0.05); }}
        .mt-workspace-summary {{ display:grid; grid-template-columns: minmax(0,1.55fr) minmax(280px,0.82fr); gap:16px; align-items:stretch; }}
        .mt-workspace-hero {{ padding:18px 20px; border-radius:18px; background: linear-gradient(135deg, #eff6ff 0%, #ffffff 65%); border:1px solid #dbeafe; min-height: 100%; }}
        .mt-workspace-hero-title {{ font-size:1.05rem; font-weight:800; color:#0f172a; letter-spacing:-0.02em; }}
        .mt-workspace-hero-sub {{ font-size:0.85rem; color:#475569; line-height:1.55; margin-top:6px; }}
        .mt-workspace-inline-meta {{ display:flex; flex-wrap:wrap; gap:10px 14px; margin-top:14px; }}
        .mt-workspace-inline-item {{ display:flex; align-items:center; gap:6px; font-size:0.82rem; color:#334155; }}
        .mt-workspace-inline-label {{ color:#64748b; font-weight:700; }}
        .mt-workspace-action {{ padding:18px 18px; border-radius:18px; background:#ffffff; border:1px solid #e5e7eb; display:flex; flex-direction:column; justify-content:space-between; min-height:100%; }}
        .mt-workspace-action-top {{ font-size:0.82rem; color:#64748b; line-height:1.5; }}
        .mt-workspace-action-title {{ font-size:0.95rem; font-weight:800; color:#0f172a; margin-bottom:8px; }}
        .mt-workspace-action-meta {{ display:grid; grid-template-columns:auto 1fr; gap:6px 10px; margin-top:10px; font-size:0.8rem; }}
        .mt-workspace-action-key {{ color:#64748b; font-weight:700; }}
        .mt-workspace-action-value {{ color:#0f172a; font-weight:600; }}
        .mt-workspace-strip {{ margin-top:12px; padding:10px 12px; border-radius:14px; border:1px solid #bfdbfe; background:#eff6ff; font-size:0.82rem; color:#1e3a8a; font-weight:700; }}
        .mt-workspace-strip-inline {{ margin-top:0 !important; padding:6px 12px !important; border-radius:14px; border:1px solid #bfdbfe; background:#eff6ff; font-size:0.82rem; color:#1e3a8a; font-weight:700; display:inline-flex; align-items:center; min-height:36px; box-sizing:border-box; }}
        [class*="st-key-workspace_chat_card"] [data-testid="stHorizontalBlock"] [data-testid="column"] {{ align-self: center !important; }}
        .mt-workspace-case-stats {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:8px 0 14px 0; }}
        .mt-workspace-case-stat {{ padding:10px 12px; border-radius:14px; border:1px solid #e5e7eb; background:#f8fafc; }}
        .mt-workspace-case-stat-value {{ font-size:1rem; font-weight:800; color:#0f172a; }}
        .mt-workspace-case-stat-label {{ font-size:0.76rem; font-weight:700; color:#64748b; margin-top:2px; }}
        .mt-case-card {{ padding:14px 14px 16px 14px; border-radius:18px; background:rgba(255,255,255,0.98); border:1px solid #e5e7eb; box-shadow:0 8px 22px rgba(15,23,42,0.04); transition: all 0.18s ease; }}
        .mt-case-card-selected {{ border:2px solid #2563eb; box-shadow:0 0 0 3px rgba(37,99,235,0.08), 0 12px 26px rgba(15,23,42,0.08); }}
        .mt-case-click-wrap {{ cursor:pointer !important; }}
        .mt-case-click-wrap:hover .mt-case-card {{ border-color:#93c5fd; box-shadow:0 10px 24px rgba(37,99,235,0.10); }}
        .mt-case-link {{ display:block; text-decoration:none !important; color:inherit !important; cursor:pointer !important; }}
        .mt-case-link:hover, .mt-case-link:focus, .mt-case-link:visited {{ text-decoration:none !important; color:inherit !important; }}
        .mt-case-name {{ font-size:1rem; font-weight:800; color:#0f172a; line-height:1.35; margin-top:4px; }}
        .mt-case-meta-line {{ font-size:0.84rem; color:#64748b; line-height:1.55; margin-top:8px; }}
        .mt-case-submeta {{ display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:6px 12px; margin-top:12px; }}
        .mt-case-submeta-item {{ font-size:0.8rem; color:#334155; }}
        .mt-case-submeta-label {{ color:#64748b; font-weight:700; margin-right:6px; }}
        .mt-stream-note {{ margin-top:8px; font-size:0.78rem; color:#64748b; }}
        .mt-section-note {{ font-size:0.82rem; color:#64748b; line-height:1.55; margin-bottom:12px; }}
        .mt-hitl-banner {{ width:100%; background:#fef9c3; border:1px solid #fde68a; color:#92400e; font-weight:700; border-radius:12px; padding:10px 12px; line-height:1.35; box-sizing:border-box; }}
        .mt-section-inline {{ display:flex; align-items:center; flex-wrap:wrap; gap:8px 12px; margin-bottom:12px; }}
        .mt-section-inline-title {{ font-size:0.95rem; font-weight:800; color:#0f172a; flex-shrink:0; }}
        .mt-section-inline-content {{ font-size:0.84rem; color:#64748b; line-height:1.5; }}
        .mt-stream-stage-row {{ display:flex; flex-wrap:wrap; gap:8px; margin:10px 0 12px 0; }}
        .mt-stream-stage-pill {{ display:inline-flex; align-items:center; gap:6px; padding:6px 10px; border-radius:999px; background:#fff; border:1px solid #dbeafe; font-size:0.76rem; font-weight:700; color:#1d4ed8; }}
        .mt-result-grid {{ display:grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap:12px; margin-top:14px; margin-bottom:16px; }}
        .mt-result-metric {{ padding:14px 16px; border-radius:16px; border:1px solid #e5e7eb; background:#fff; }}
        .mt-result-metric-label {{ font-size:0.8rem; color:#64748b; font-weight:700; }}
        .mt-result-metric-value {{ font-size:1.65rem; font-weight:800; color:#0f172a; margin-top:8px; }}
        .mt-result-metric-foot {{ font-size:0.78rem; color:#64748b; margin-top:6px; line-height:1.45; }}
        @media (max-width: 1280px) {{
          .mt-workspace-summary {{ grid-template-columns: 1fr; }}
          .mt-workspace-case-stats {{ grid-template-columns: repeat(2,minmax(0,1fr)); }}
          .mt-result-grid {{ grid-template-columns: 1fr; }}
        }}
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
    if value in {"HOLD_AFTER_HITL"}:
        return '<span class="mt-badge mt-badge-amber">보류</span>'
    if value in {"HITL_REQUIRED", "FAILED"}:
        return '<span class="mt-badge mt-badge-red">주의</span>'
    if value in {"RESOLVED", "COMPLETED", "OK", "COMPLETED_AFTER_HITL", "COMPLETED_AFTER_EVIDENCE"}:
        return '<span class="mt-badge mt-badge-green">완료</span>'
    if value == "EVIDENCE_REJECTED":
        return '<span class="mt-badge mt-badge-amber">증빙 불일치</span>'
    return f'<span class="mt-badge">{status_display_name(status) or value}</span>'


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

# 스킬 스키마 필드명 → 한글 라벨 (에이전트 스튜디오 발표용)
FIELD_LABELS_KO: dict[str, str] = {
    "case_id": "케이스 ID",
    "body_evidence": "전표·증거",
    "intended_risk_type": "의도된 위험 유형",
    "occurred_at": "발생 시각",
    "amount": "금액",
    "budget_exceeded": "예산 초과",
    "mcc_code": "가맹점 업종 코드(MCC)",
    "hr_status": "근태 상태",
    "document": "전표 문서",
}


def get_tool_display_summary_ko(
    tool: Any,
    skill_display_summary_ko: str | None,
) -> str:
    """발표용 한글 요약: 고정 문구 우선, 없으면 description 기반 자동 생성."""
    if skill_display_summary_ko and str(skill_display_summary_ko).strip():
        return str(skill_display_summary_ko).strip()
    desc = getattr(tool, "description", None) or ""
    return f"입력/출력: {desc[:120]}{'…' if len(str(desc)) > 120 else ''}" if desc else "—"


def mcc_display_name(mcc_code: str | None) -> str:
    if not mcc_code:
        return "-"
    return MCC_DISPLAY_NAMES.get(str(mcc_code).strip(), f"가맹점 업종 코드(MCC) {mcc_code}")


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
    labels = {
        "NEW": "신규",
        "PENDING_EXPLANATION": "소명 대기",
        "IN_REVIEW": "검토 중",
        "REVIEW_REQUIRED": "검토 필요",
        "REVIEW_AFTER_HITL": "검토 재개",
        "HITL_REQUIRED": "담당자 검토 필요",
        "HOLD_AFTER_HITL": "보류",
        "EVIDENCE_PENDING": "증빙 제출 대기",
        "EVIDENCE_REJECTED": "증빙 불일치",
        "COMPLETED": "완료",
        "COMPLETED_AFTER_HITL": "검토 후 완료",
        "COMPLETED_AFTER_EVIDENCE": "증빙 검증 완료",
        "RESOLVED": "해결",
    }
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
          padding: 4px 20px 14px 20px;
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


def render_panel_header(title: str, subtitle: str = "", trailing: str = "") -> None:
    subtitle_html = f'<div class="mt-panel-sub">{subtitle}</div>' if subtitle else ''
    trailing_html = f'<div class="mt-panel-sub mt-panel-trailing">{trailing}</div>' if trailing else ''
    st.markdown(
        f'<div class="mt-panel-header"><div><div class="mt-panel-title">{title}</div>{subtitle_html}</div>{trailing_html}</div>',
        unsafe_allow_html=True,
    )


def render_kpi_card(label: str, value: str, foot: str = "") -> None:
    st.markdown(f'<div class="mt-kpi"><div class="mt-kpi-label">{label}</div><div class="mt-kpi-value">{value}</div><div class="mt-kpi-foot">{foot}</div></div>', unsafe_allow_html=True)


def render_empty_state(message: str) -> None:
    st.markdown(f'<div class="mt-empty-box">{message}</div>', unsafe_allow_html=True)


# ── RAG 라이브러리 전용 컴포넌트 ────────────────────────────────────────────

def render_rag_kpi_card(
    label: str,
    value: str,
    foot: str = "",
    status: str = "normal",   # "normal" | "warning" | "error" | "success"
    icon: str = "",
) -> None:
    """
    상태 색상이 있는 RAG KPI 카드.
    status에 따라 좌측 border 색과 배경 tint가 달라짐.
    """
    color_map = {
        "normal":  ("#2563eb", "#eff6ff"),
        "warning": ("#d97706", "#fffbeb"),
        "error":   ("#dc2626", "#fef2f2"),
        "success": ("#059669", "#ecfdf5"),
    }
    border_color, bg_tint = color_map.get(status, color_map["normal"])
    icon_html = f'<span style="font-size:1.3rem;flex-shrink:0;line-height:1">{icon}</span>' if icon else ""
    label_esc = html.escape(label)
    value_esc = html.escape(value)
    foot_esc = html.escape(foot)
    st.markdown(f"""
    <div class="mt-rag-kpi-card" style="
        padding: 18px 20px;
        border-radius: 18px;
        border: 1px solid #e5e7eb;
        border-left: 4px solid {border_color};
        background: {bg_tint};
        box-shadow: 0 10px 24px rgba(15,23,42,0.05);
        min-height: 132px;
        overflow: hidden;
        max-width: 100%;
        box-sizing: border-box;
    ">
        <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:4px;min-width:0">
            {icon_html}
            <div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex:1;min-width:0;overflow:hidden">
                <div class="mt-kpi-label mt-rag-kpi-label" style="margin-bottom:0;overflow-wrap:break-word;word-break:break-word;min-width:0">{label_esc}</div>
                <div class="mt-kpi-value mt-rag-kpi-value" style="color:{border_color};font-size:1.75rem;margin:0;flex-shrink:0;overflow-wrap:break-word;word-break:break-word">{value_esc}</div>
            </div>
        </div>
        <div class="mt-kpi-foot mt-rag-kpi-foot">{foot_esc}</div>
    </div>
    """, unsafe_allow_html=True)


def render_rag_meta_grid(meta: dict) -> None:
    """
    문서 메타를 Key-Value 그리드로 렌더링.
    탭 내부에서 HTML 이스케이프되는 문제를 피하기 위해 Streamlit 네이티브만 사용.
    """
    label_map = {
        "status": "처리 상태",
        "version": "버전",
        "effective_from": "시행 시작일",
        "effective_to": "시행 종료일",
        "lifecycle_status": "라이프사이클",
        "active_from": "활성 시작",
        "active_to": "활성 종료",
        "quality_gate_passed": "품질 게이트",
        "updated_at": "최종 수정",
    }
    status_icon_map = {
        "COMPLETED": "✅",
        "PROCESSING": "⏳",
        "FAILED": "❌",
        "ACTIVE": "🟢",
        "INACTIVE": "⚫",
    }

    def _format_val(raw: Any) -> str:
        if raw is None:
            return "—"
        if isinstance(raw, bool):
            return "✅ 통과" if raw else "❌ 미통과"
        val_str = str(raw)
        icon = status_icon_map.get(val_str.upper(), "")
        return f"{icon} {val_str}" if icon else val_str

    for key, display_label in label_map.items():
        raw = meta.get(key)
        val = _format_val(raw)
        c1, c2 = st.columns([0.32, 0.68])
        with c1:
            st.caption(f"**{display_label}**")
        with c2:
            st.caption(val)


def render_rag_quality_report(report: dict) -> None:
    """
    품질 리포트를 게이지 바 + 수치로 시각화.
    JSON 덤프 대신 직관적 지표 카드.
    """
    def _gauge(label: str, value: float | None, *, higher_is_bad: bool = True,
               threshold_warn: float = 0.1, threshold_err: float = 0.3,
               suffix: str = "%") -> str:
        if value is None:
            return f"""
            <div style="margin-bottom:14px">
                <div style="font-size:0.8rem;font-weight:700;color:#64748b;margin-bottom:4px">{label}</div>
                <div style="font-size:0.85rem;color:#94a3b8">데이터 없음</div>
            </div>"""
        pct = float(value) * 100 if suffix == "%" else float(value)
        if higher_is_bad:
            color = "#dc2626" if pct >= threshold_err * 100 else ("#d97706" if pct >= threshold_warn * 100 else "#059669")
        else:
            color = "#059669" if pct >= 80 else ("#d97706" if pct >= 50 else "#dc2626")
        bar_width = min(100, max(0, pct))
        return f"""
        <div style="margin-bottom:14px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px">
                <div style="font-size:0.8rem;font-weight:700;color:#64748b">{label}</div>
                <div style="font-size:0.9rem;font-weight:800;color:{color}">{pct:.1f}{suffix}</div>
            </div>
            <div style="background:#f1f5f9;border-radius:999px;height:7px;overflow:hidden">
                <div style="width:{bar_width}%;background:{color};height:100%;border-radius:999px;
                            transition:width 0.6s ease"></div>
            </div>
        </div>"""

    passed = report.get("quality_report_passed")
    input_c = report.get("input_chunks")
    final_c = report.get("final_chunks")
    missing = report.get("missing_required") or []

    gate_html = f"""
    <div style="display:flex;align-items:center;gap:10px;padding:12px 16px;
                border-radius:12px;background:{'#ecfdf5' if passed else '#fef2f2'};
                border:1px solid {'#a7f3d0' if passed else '#fecaca'};margin-bottom:16px">
        <span style="font-size:1.4rem">{'✅' if passed else '❌'}</span>
        <div>
            <div style="font-size:0.9rem;font-weight:800;color:{'#059669' if passed else '#dc2626'}">
                품질 게이트 {'통과' if passed else '미통과'}
            </div>
            <div style="font-size:0.78rem;color:#64748b">
                입력 {input_c or '?'}개 → 최종 {final_c or '?'}개 청크
            </div>
        </div>
    </div>"""

    gauges_html = (
        _gauge("노이즈율", report.get("noise_rate"), higher_is_bad=True,
               threshold_warn=0.05, threshold_err=0.15)
        + _gauge("중복율", report.get("duplicate_rate"), higher_is_bad=True,
                 threshold_warn=0.05, threshold_err=0.20)
        + _gauge("초단편 청크율", report.get("short_chunk_rate"), higher_is_bad=True,
                 threshold_warn=0.10, threshold_err=0.30)
        + _gauge("조항 커버리지", report.get("article_coverage"), higher_is_bad=False,
                 threshold_warn=50, threshold_err=30, suffix="%")
    )

    missing_html = ""
    if missing:
        items_html = "".join(f'<div style="color:#dc2626;font-size:0.8rem">• {m}</div>' for m in missing)
        missing_html = f"""
        <div style="padding:10px 14px;background:#fef2f2;border-radius:10px;
                    border:1px solid #fecaca;margin-top:10px">
            <div style="font-size:0.8rem;font-weight:700;color:#dc2626;margin-bottom:6px">
                ⚠ 누락 필수 항목
            </div>
            {items_html}
        </div>"""

    st.markdown(gate_html + gauges_html + missing_html, unsafe_allow_html=True)


def render_legend(items: list[tuple[str, str]]) -> None:
    html = ['<div class="mt-legend">']
    for color, label in items:
        html.append(
            f'<div class="mt-legend-item"><span class="mt-legend-dot" style="background:{color}"></span>{label}</div>'
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)


def _draw_graph_png(
    nodes: list[tuple[str, str, str]],
    edges: list[tuple[str, str, str]],
    pos: dict[str, tuple[float, float]],
    figsize: tuple[float, float] = (9, 3.5),
    hitl_nodes: set[str] | None = None,
    skill_nodes: set[str] | None = None,
) -> bytes:
    """matplotlib로 노드/엣지를 그려 PNG 바이트로 반환. graphviz dot 바이너리 불필요."""
    hitl_nodes = hitl_nodes or set()
    skill_nodes = skill_nodes or set()
    fig, ax = plt.subplots(figsize=figsize)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.patch.set_facecolor("#f8fafc")

    node_w, node_h = 1.8, 0.46

    def _node_color(key: str) -> tuple[str, str]:
        if key in hitl_nodes:
            return "#fffbeb", "#f59e0b"
        if key in skill_nodes:
            return "#eff6ff", "#93c5fd"
        return "#f5f3ff", "#a78bfa"

    label_map = {k: lbl for k, lbl, _ in nodes}

    for key, label, _ in nodes:
        x, y = pos[key]
        fc, ec = _node_color(key)
        ls = "--" if key in hitl_nodes else "-"
        rect = mpatches.FancyBboxPatch(
            (x - node_w / 2, y - node_h / 2), node_w, node_h,
            boxstyle="round,pad=0.06", facecolor=fc, edgecolor=ec, linewidth=1.4, linestyle=ls,
        )
        ax.add_patch(rect)
        ax.text(x, y, label, ha="center", va="center", fontsize=7.5, color="#1e293b", fontweight="500")

    for src, dst, elabel in edges:
        if src not in pos or dst not in pos:
            continue
        sx, sy = pos[src]
        dx, dy = pos[dst]
        dx_off = dx - node_w / 2 - 0.06 if dx > sx else dx + node_w / 2 + 0.06
        sx_off = sx + node_w / 2 + 0.06 if dx > sx else sx - node_w / 2 - 0.06
        mid_x = (sx_off + dx_off) / 2
        mid_y = (sy + dy) / 2
        ax.annotate(
            "", xy=(dx_off, dy), xytext=(sx_off, sy),
            arrowprops=dict(arrowstyle="-|>", color="#94a3b8", lw=1.1),
        )
        if elabel:
            ax.text(mid_x, mid_y + 0.1, elabel, ha="center", va="bottom", fontsize=6, color="#64748b")

    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    margin = 1.2
    ax.set_xlim(min(xs) - margin, max(xs) + margin)
    ax.set_ylim(min(ys) - 0.8, max(ys) + 0.8)
    plt.tight_layout(pad=0.1)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


@st.cache_data(show_spinner=False)
def draw_agent_graph() -> bytes:
    """상위 오케스트레이션 그래프를 PNG 바이트로 반환. build_agent_graph()와 동일한 노드·엣지."""
    nodes = [
        ("start", "START", ""),
        ("screener", "Screener", ""),
        ("intake", "Intake Agent", ""),
        ("planner", "Planner Agent", ""),
        ("executor", "Execute Agent", ""),
        ("critic", "Critic Agent", ""),
        ("verifier", "Verifier Agent", ""),
        ("hitl", "HITL Review", ""),
        ("reporter", "Reporter Agent", ""),
        ("finalizer", "Finalizer", ""),
        ("end", "END", ""),
    ]
    # 노드 좌→우 일렬 (START -> screener -> intake -> ...), HITL은 위쪽 분기
    x_main = [0, 1.6, 3.2, 4.8, 6.4, 8.0, 9.6, 11.2, 12.8, 14.4, 16.0]
    keys_main = ["start", "screener", "intake", "planner", "executor", "critic", "verifier", "reporter", "finalizer", "end"]
    pos = {k: (x_main[i], 0.0) for i, k in enumerate(keys_main)}
    pos["hitl"] = (9.6, 1.2)

    edges = [
        ("start", "screener", ""), ("screener", "intake", ""), ("intake", "planner", ""), ("planner", "executor", ""),
        ("executor", "critic", ""), ("critic", "verifier", ""),
        ("verifier", "hitl", "if needed"), ("verifier", "reporter", "or continue"),
        ("hitl", "reporter", "resume"), ("reporter", "finalizer", ""), ("finalizer", "end", ""),
    ]
    return _draw_graph_png(nodes, edges, pos, figsize=(17, 3.6), hitl_nodes={"hitl"})


@st.cache_data(show_spinner=False)
def draw_tool_execution_graph() -> bytes:
    """하위 실행 도구 그래프를 PNG 바이트로 반환. 첫 렌더 후 캐싱."""
    tool_keys = ["holiday", "budget", "merchant", "document", "policy"]
    nodes = [
        ("execute", "execute", ""),
        ("holiday", "holiday_compliance_probe", ""),
        ("budget", "budget_risk_probe", ""),
        ("merchant", "merchant_risk_probe", ""),
        ("document", "document_evidence_probe", ""),
        ("policy", "policy_rulebook_probe", ""),
        ("legacy", "legacy_aura_deep_audit", ""),
        ("score", "score_breakdown", ""),
    ]
    xs = [-4.5, -2.25, 0, 2.25, 4.5]
    pos: dict[str, tuple[float, float]] = {"execute": (0.0, 2.2), "score": (0.0, -2.2), "legacy": (6.5, 0.0)}
    for i, k in enumerate(tool_keys):
        pos[k] = (xs[i], 0.0)

    edges = [(("execute", k, "") for k in tool_keys)] + [("execute", "legacy", "conditional")]
    edges_flat: list[tuple[str, str, str]] = [("execute", k, "") for k in tool_keys]
    edges_flat.append(("execute", "legacy", "cond."))
    for k in tool_keys + ["legacy"]:
        edges_flat.append((k, "score", ""))

    return _draw_graph_png(
        nodes, edges_flat, pos, figsize=(14, 5.5),
        skill_nodes=set(tool_keys) | {"execute", "legacy", "score"},
    )


def render_graph_image(title: str, image_bytes: bytes | None, fallback_graph: bytes | None, caption: str) -> None:
    """그래프를 PNG(st.image)로 표시. st.graphviz_chart를 사용하지 않아 콘솔 에러 없음."""
    st.markdown(f"**{title}**")
    _, center, _ = st.columns([0.08, 0.84, 0.08])
    with center:
        png = image_bytes or fallback_graph
        if png:
            st.image(png, use_container_width=True)
        else:
            st.caption("그래프를 표시할 수 없습니다.")
        st.caption(caption)
