from __future__ import annotations

import streamlit as st
from streamlit_option_menu import option_menu

from utils.config import settings

MENU_OPTIONS = [
    ("AI 워크스페이스", "grid", ""),
    ("에이전트 스튜디오", "cpu", "Agent model, prompt, tool, knowledge"),
    ("규정문서 라이브러리", "journal-text", "RAG document governance and quality"),
    ("시연 데이터 제어", "sliders", "Scenario data generator"),
]

# _ICON_MAP = {
#     "grid": "▦",
#     "cpu": "◫",
#     "journal-text": "≣",
#     "graph-up": "↗",
#     "sliders": "☷",
# }


def render_sidebar() -> str:
    with st.sidebar:
        options = [item[0] for item in MENU_OPTIONS]
        icons = [item[1] for item in MENU_OPTIONS]
        selected = option_menu(
            menu_title=None,
            options=options,
            icons=icons,
            default_index=options.index(st.session_state.get("mt_menu", options[0])) if st.session_state.get("mt_menu", options[0]) in options else 0,
            key="mt_menu_option",
            styles={
                "container": {"padding": "0!important", "background-color": "transparent"},
                "icon": {"color": "#93c5fd", "font-size": "18px"},
                "nav-link": {"font-size": "14px", "font-weight": "700", "text-align": "left", "margin": "4px 0", "padding": "12px 14px", "border-radius": "12px", "color": "#e5e7eb", "background-color": "transparent"},
                "nav-link-selected": {"background-color": "#2563eb", "color": "#ffffff", "font-weight": "800"},
            },
        )
        st.session_state["mt_menu"] = selected
        st.divider()
        with st.expander("개발 옵션", expanded=False):
            st.checkbox("디버그 보기 표시", key="mt_debug_mode")
            st.caption("워크벤치에서는 디버그 표시는 여기서만 켭니다.")
            st.code(f"AGENT_RUNTIME_MODE={settings.agent_runtime_mode}\nENABLE_MULTI_AGENT={settings.enable_multi_agent}\nAPI_BASE_URL={settings.api_base_url}", language="bash")
        return selected
