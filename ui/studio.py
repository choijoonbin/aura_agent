from __future__ import annotations

import json
import streamlit as st

from agent.langgraph_agent import build_agent_graph
from agent.skills import SKILL_REGISTRY, get_langchain_tools
from ui.api_client import get
from ui.shared import (
    draw_agent_graph,
    draw_skill_execution_graph,
    fmt_dt,
    get_tool_display_summary_ko,
    render_empty_state,
    render_graph_image,
    render_legend,
    render_page_header,
    render_panel_header,
    stylable_container,
)

# draw_agent_graph / draw_skill_execution_graph 는 이제 PNG bytes를 반환 (graphviz dot 바이너리 불필요)
# @st.cache_data로 캐싱되므로 최초 1회만 matplotlib 렌더링, 이후 즉시 반환


@st.cache_data(ttl=60, show_spinner=False)
def _get_agents() -> list[dict]:
    """에이전트 목록 API 결과를 60초 캐싱. 메뉴 전환 시 재호출 없이 즉시 반환."""
    return get("/api/v1/agents").get("items") or []


@st.cache_data(ttl=60, show_spinner=False)
def _get_agent_detail(agent_id: int) -> dict:
    """에이전트 상세 API 결과를 60초 캐싱."""
    return get(f"/api/v1/agents/{agent_id}")


def render_agent_studio_page() -> None:
    # 그래프 PNG를 미리 캐싱 (첫 방문 이후엔 즉시 반환)
    draw_agent_graph()
    draw_skill_execution_graph()

    render_page_header("에이전트 스튜디오", "에이전트 구조, 프롬프트, 런타임 스킬, 연결 지식을 한 화면에서 점검합니다.")
    agents = _get_agents()
    if not agents:
        st.info("에이전트 데이터가 없습니다.")
        return
    selected_id = st.session_state.get("mt_selected_agent_id") or agents[0]["agent_id"]
    left, right = st.columns([0.28, 0.72])
    with left:
        # Streamlit ignores CSS padding for widget width — use inner columns for side spacing.
        with stylable_container(
            key="studio_active_agent_box",
            css_styles="""{
              padding: 1rem 0 1.25rem 0;
              border-radius: 16px;
              border: 1px solid #e5e7eb;
              background: rgba(255,255,255,0.98);
              box-shadow: 0 8px 22px rgba(15,23,42,0.05);
              margin-bottom: 1rem;
              box-sizing: border-box;
            }"""
        ):
            _lpad, mid, _rpad = st.columns([0.05, 0.90, 0.05])
            with mid:
                render_panel_header("활성 에이전트", "현재 PoC에서 사용 가능한 활성 에이전트 정의입니다.")
                for agent in agents:
                    if st.button(
                        agent.get("name") or "-",
                        key=f"agent_{agent['agent_id']}",
                        width="stretch",
                        type="primary" if int(agent["agent_id"]) == int(selected_id) else "secondary",
                    ):
                        st.session_state["mt_selected_agent_id"] = agent["agent_id"]
                        st.rerun()
    with right:
        detail = _get_agent_detail(int(selected_id))
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
            render_panel_header("런타임 도구", "실제 runtime graph에서 사용하는 LangChain tool 목록입니다.")
            st.caption("Phase C: execute 노드는 plan 기반으로 이 도구들을 호출합니다.")
            tools = get_langchain_tools()
            skill_cols = st.columns(2)
            for idx, tool in enumerate(tools):
                with skill_cols[idx % 2]:
                    with stylable_container(
                        key=f"skill_card_{getattr(tool, 'name', idx)}",
                        css_styles="""{padding: 14px 16px; border-radius: 16px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.98); box-shadow: 0 8px 22px rgba(15,23,42,0.04); min-height: 128px; margin-bottom: 0.7rem;}"""
                    ):
                        tool_name = getattr(tool, "name", "-")
                        st.caption("LangChain tool")
                        st.markdown(f"**{tool_name}**")
                        skill = SKILL_REGISTRY.get(tool_name) if tool_name else None
                        display_ko = get_tool_display_summary_ko(tool, getattr(skill, "display_summary_ko", None) if skill else None)
                        st.write(display_ko)
                        with st.expander("원본 스키마 보기", expanded=False):
                            try:
                                schema = getattr(tool, "args_schema", None)
                                if schema and hasattr(schema, "model_json_schema"):
                                    st.json(schema.model_json_schema())
                                else:
                                    st.caption("스키마 없음")
                            except Exception:
                                st.caption("스키마를 불러올 수 없습니다.")
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
                st.caption("상위 오케스트레이션 그래프: screener → intake → planner → execute → critic → verify → hitl_pause/reporter → finalizer. 실제 runtime graph와 동일합니다.")
                render_legend(
                    [
                        ("#f5f3ff", "에이전트 메인 노드"),
                        ("#fffbeb", "조건부 HITL 노드"),
                        ("#94a3b8", "상태 전이"),
                    ]
                )
                render_graph_image("메인 오케스트레이션 그래프", draw_agent_graph(), None, "상위 오케스트레이션: 전체 노드 흐름. 하위는 '스킬 실행 흐름' 탭에서 execute 노드 내부 도구 순서를 확인할 수 있습니다.")
                st.markdown("""
**단계별 설명**

1. **START**: 분석 런 시작
2. **Intake Agent**: 전표 입력과 위험 지표 정규화
3. **Planner Agent**: 조사 계획과 tool 순서 수립
4. **Execute Agent**: 실제 skill/tool 호출
5. **Critic Agent**: 과잉 주장과 반례 검토
6. **Verifier Agent**: 자동 판정 가능 여부 검증
7. **HITL Review**: 담당자 검토 필요 시 개입
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
                render_graph_image("실행 스킬 그래프", draw_skill_execution_graph(), None, "하위 실행 스킬 그래프: execute 노드 내부에서 호출되는 런타임 skill 순서입니다.")
                st.markdown("""
**단계별 설명**

1. **execute**: 조사 계획의 실제 실행 허브
2. **holiday_compliance_probe**: 휴일/휴무/시간대 검증
3. **budget_risk_probe**: 예산 초과 및 금액 리스크 확인
4. **merchant_risk_probe**: 가맹점 업종 코드(MCC)/거래처 기반 업종 리스크 판별
5. **document_evidence_probe**: 전표 라인/문서 증거 수집
6. **policy_rulebook_probe**: 규정 조항 검색 및 연결
7. **legacy_aura_deep_audit**: 필요 시 specialist 심층 감사 호출
8. **score_breakdown**: 정량 점수와 품질 지표 집계
""")
