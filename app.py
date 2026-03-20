from __future__ import annotations

import logging
import streamlit as st

# 터미널에서 [HITL_CLOSE]/[RESUME_TRACE] 등 INFO 로그 확인용 (streamlit run 실행 터미널에 출력)
try:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", datefmt="%H:%M:%S", force=True)
except TypeError:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", datefmt="%H:%M:%S")

# 불필요한 라이브러리 로그 억제 (httpx, openai 등이 터미널을 오염하지 않도록)
for _noisy_logger in ("httpx", "httpcore", "openai", "openai._base_client"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

from ui.demo_new import render_demo_new_page
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

selected_menu = render_sidebar()

if selected_menu == "AI 워크스페이스":
    render_ai_workspace_page()
elif selected_menu == "에이전트 스튜디오":
    render_agent_studio_page()
elif selected_menu == "규정문서 라이브러리":
    render_rag_library_page()
elif selected_menu == "시연데이터 생성 (Beta)":
    render_demo_new_page()
else:
    render_ai_workspace_page()
