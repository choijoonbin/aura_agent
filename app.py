from __future__ import annotations

import streamlit as st

from ui.demo import render_demo_control_page
from ui.rag import render_rag_library_page
from ui.shared import inject_css
from ui.sidebar import render_sidebar
from ui.studio import render_agent_studio_page
from ui.workspace import render_ai_workspace_page

st.set_page_config(page_title="Aura Agent AI", layout="wide", initial_sidebar_state="expanded")
inject_css()
st.markdown('<div class="mt-app-header">Aura Agent AI</div>', unsafe_allow_html=True)

selected_menu = render_sidebar()

if selected_menu == "AI 워크스페이스":
    render_ai_workspace_page()
elif selected_menu == "에이전트 스튜디오":
    render_agent_studio_page()
elif selected_menu == "규정문서 라이브러리":
    render_rag_library_page()
else:
    render_demo_control_page()
