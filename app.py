from __future__ import annotations

import streamlit as st

from ui.dashboard import render_dashboard_page
from ui.demo import render_demo_control_page
from ui.rag import render_rag_library_page
from ui.shared import inject_css
from ui.sidebar import render_sidebar
from ui.studio import render_agent_studio_page
from ui.workspace import render_ai_workspace_page

st.set_page_config(page_title="Aura Agentic AI", layout="wide", initial_sidebar_state="expanded")
inject_css()

# 시연 데이터 제어 등에서 "워크스페이스 열기" 클릭 시: 위젯 키(mt_menu_option)는 위젯 생성 전에만 설정 가능
if "mt_redirect_to_menu" in st.session_state:
    target = st.session_state.pop("mt_redirect_to_menu")
    st.session_state["mt_menu_option"] = target
    st.session_state["mt_menu"] = target

st.markdown('<div class="mt-app-header">Aura Agentic AI</div>', unsafe_allow_html=True)

selected_menu = render_sidebar()

if selected_menu == "AI 워크스페이스":
    render_ai_workspace_page()
elif selected_menu == "에이전트 스튜디오":
    render_agent_studio_page()
elif selected_menu == "규정문서 라이브러리":
    render_rag_library_page()
elif selected_menu == "운영 대시보드":
    render_dashboard_page()
else:
    render_demo_control_page()
