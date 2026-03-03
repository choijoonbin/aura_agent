from __future__ import annotations

import streamlit as st
from streamlit_extras.stylable_container import stylable_container

from agent.langgraph_agent import build_agent_graph
from agent.skills import SKILL_REGISTRY
from ui.api_client import get
from ui.shared import draw_agent_graph, draw_skill_execution_graph, fmt_dt, render_empty_state, render_graph_image, render_legend, render_page_header, render_panel_header


@st.cache_resource
def get_agent_graph_mermaid_png() -> bytes | None:
    try:
        graph = build_agent_graph()
        return graph.get_graph().draw_mermaid_png()
    except Exception:
        return None


def render_agent_studio_page() -> None:
    render_page_header("에이전트 스튜디오", "에이전트 구조, 프롬프트, 런타임 스킬, 연결 지식을 한 화면에서 점검합니다.")
    agents = get("/api/v1/agents").get("items") or []
    if not agents:
        st.info("에이전트 데이터가 없습니다.")
        return
    selected_id = st.session_state.get("mt_selected_agent_id") or agents[0]["agent_id"]
    left, right = st.columns([0.28, 0.72])
    with left:
        render_panel_header("활성 에이전트", "현재 PoC에서 사용 가능한 활성 에이전트 정의입니다.")
        for agent in agents:
            with stylable_container(
                key=f"studio_agent_pick_{agent['agent_id']}",
                css_styles=f"""{{padding: 10px 10px 8px 10px; border-radius: 14px; border: {'2px solid #2563eb' if int(agent['agent_id']) == int(selected_id) else '1px solid #e5e7eb'}; background: rgba(255,255,255,0.98); box-shadow: 0 6px 18px rgba(15,23,42,0.04); margin-bottom: 0.55rem;}}"""
            ):
                st.caption(agent.get("agent_key") or "-")
                if st.button(agent.get('name') or '-', key=f"agent_{agent['agent_id']}", use_container_width=True, type="primary" if int(agent['agent_id']) == int(selected_id) else "secondary"):
                    st.session_state["mt_selected_agent_id"] = agent["agent_id"]
                    st.rerun()
    with right:
        detail = get(f"/api/v1/agents/{selected_id}")
        with stylable_container(key=f"studio_hero_{selected_id}", css_styles="""{padding: 18px 20px; border-radius: 18px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.98); box-shadow: 0 10px 24px rgba(15,23,42,0.05); margin-bottom: 12px;}"""):
            st.markdown(f"## {detail.get('name') or '-'}")
            st.caption(f"agent_key={detail.get('agent_key') or '-'} / domain={detail.get('domain') or '-'}")
        tabs = st.tabs(["모델", "프롬프트", "도구", "지식", "그래프"])
        with tabs[0]:
            c1, c2, c3 = st.columns(3)
            c1.metric("모델", str(detail.get("model_name") or "-"))
            c2.metric("temperature", str(detail.get("temperature") or "-"))
            c3.metric("max_tokens", str(detail.get("max_tokens") or "-"))
            st.caption(f"active={detail.get('is_active')} / updated_at={fmt_dt(detail.get('updated_at'))}")
        with tabs[1]:
            render_panel_header("프롬프트", "현재 시스템 프롬프트와 변경 이력을 점검합니다.")
            current_prompt = detail.get("current_prompt") or {}
            st.text_area("Current System Prompt", value=str(current_prompt.get("system_instruction") or ""), height=320)
            with st.expander("Prompt History"):
                st.json(detail.get("prompt_history") or [])
        with tabs[2]:
            render_panel_header("런타임 스킬", "LangGraph execute 단계에서 실제 호출 가능한 스킬 목록입니다.")
            skill_cols = st.columns(2)
            for idx, (skill_name, skill) in enumerate(SKILL_REGISTRY.items()):
                with skill_cols[idx % 2]:
                    with stylable_container(
                        key=f"skill_card_{skill_name}",
                        css_styles="""{padding: 14px 16px; border-radius: 16px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.98); box-shadow: 0 8px 22px rgba(15,23,42,0.04); min-height: 128px; margin-bottom: 0.7rem;}"""
                    ):
                        st.caption("runtime skill")
                        st.markdown(f"**{skill_name}**")
                        st.write(skill.description or "-")
        with tabs[3]:
            render_panel_header("연결 지식", "이 에이전트가 참조하는 문서와 지식 자산입니다.")
            docs = detail.get("documents") or []
            if docs:
                for doc in docs:
                    st.markdown(f"- **{doc.get('title')}** · status={doc.get('status')} · doc_id={doc.get('doc_id')}")
            else:
                render_empty_state("연결된 지식 문서가 없습니다.")
        with tabs[4]:
            graph_tabs = st.tabs(["메인 오케스트레이션", "스킬 실행 흐름"])
            with graph_tabs[0]:
                render_legend(
                    [
                        ("#f5f3ff", "에이전트 메인 노드"),
                        ("#fffbeb", "조건부 HITL 노드"),
                        ("#94a3b8", "상태 전이"),
                    ]
                )
                render_graph_image("메인 오케스트레이션 그래프", None, draw_agent_graph(), "현재 PoC에서 실제 실행되는 메인 에이전트 오케스트레이션입니다.")
                st.markdown("""
**단계별 설명**

1. **START**: 분석 런 시작
2. **Intake Agent**: 전표 입력과 위험 신호 정규화
3. **Planner Agent**: 조사 계획과 tool 순서 수립
4. **Execute Agent**: 실제 skill/tool 호출
5. **Critic Agent**: 과잉 주장과 반례 검토
6. **Verifier Agent**: 자동 판정 가능 여부 검증
7. **HITL Review**: 사람 검토 필요 시 개입
8. **Reporter Agent**: 설명 문장과 최종 요약 생성
9. **Finalizer**: 상태/점수/이력 최종 확정
10. **END**: 저장/조회 가능한 결과로 종료
""")
            with graph_tabs[1]:
                render_legend(
                    [
                        ("#eff6ff", "실행 스킬 노드"),
                        ("#94a3b8", "실행/집계 흐름"),
                    ]
                )
                render_graph_image("실행 스킬 그래프", None, draw_skill_execution_graph(), "execute 노드 내부에서 호출되는 런타임 skill 흐름입니다.")
                st.markdown("""
**단계별 설명**

1. **execute**: 조사 계획의 실제 실행 허브
2. **holiday_compliance_probe**: 휴일/휴무/시간대 검증
3. **budget_risk_probe**: 예산 초과 및 금액 리스크 확인
4. **merchant_risk_probe**: MCC/거래처 기반 업종 리스크 판별
5. **document_evidence_probe**: 전표 라인/문서 증거 수집
6. **policy_rulebook_probe**: 규정 조항 검색 및 연결
7. **legacy_aura_deep_audit**: 필요 시 specialist 심층 감사 호출
8. **score_breakdown**: 정량 점수와 품질 신호 집계
""")
