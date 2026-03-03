from __future__ import annotations

import streamlit as st
from streamlit_extras.stylable_container import stylable_container

from ui.api_client import delete, get, post
from ui.shared import case_type_badge, fmt_num, render_empty_state, render_page_header, render_panel_header, status_badge


def render_demo_control_page() -> None:
    render_page_header("시연 데이터 제어", "대표 시나리오를 즉시 생성하고, 저장된 시연 전표를 다시 조회할 수 있습니다.")
    scenario_data = get("/api/v1/demo/scenarios").get("items") or []
    seeded_data = get("/api/v1/demo/seeded").get("items") or []
    top, bottom = st.columns([0.60, 0.40])
    with top:
        render_panel_header("대표 시나리오", "대표 위반 유형을 즉시 생성해 에이전트 시연 데이터로 사용합니다.")
        count = st.slider("생성 건수", min_value=1, max_value=20, value=5)
        cols = st.columns(2)
        for idx, scenario in enumerate(scenario_data):
            with cols[idx % 2]:
                with stylable_container(key=f"scenario_card_{scenario['scenario']}", css_styles="""{background: rgba(255,255,255,0.98); border: 1px solid #e5e7eb; border-radius: 18px; padding: 0.9rem 1rem 1rem 1rem; margin-bottom: 0.8rem; box-shadow: 0 8px 22px rgba(15,23,42,0.04); overflow: hidden; box-sizing: border-box;}"""):
                    st.markdown(f"### {scenario['label']}")
                    st.caption(scenario["description"])
                    st.markdown(f"<div style='display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 12px 0'><span class='mt-badge'>{scenario['risk_type']}</span><span class='mt-badge'>금액 {scenario['amount_range'][0]:,}~{scenario['amount_range'][1]:,} KRW</span><span class='mt-badge'>기준 {scenario['day_mode']}</span></div>", unsafe_allow_html=True)
                    if st.button(f"{scenario['label']} 생성", key=f"seed_{scenario['scenario']}", use_container_width=True, type="primary"):
                        out = post("/api/v1/demo/seed", params={"scenario": scenario["scenario"], "count": int(count)})
                        st.success(f"생성 완료: {out['scenario']} / {out['inserted']}건")
                        st.rerun()
    with bottom:
        render_panel_header("일괄 제어", "저장된 시연 데이터의 생성 현황을 보고 필요 시 일괄 삭제합니다.")
        st.caption("모든 시연 데이터는 `DEMO-*` 키로 저장됩니다.")
        if st.button("시연 데이터 전체 삭제", use_container_width=True):
            out = delete("/api/v1/demo/seed")
            st.warning(f"삭제 완료: {out.get('deleted', 0)}건")
            st.rerun()
        st.metric("저장된 시연 전표", len(seeded_data))
        grouped = {}
        for row in seeded_data:
            grouped[row.get("scenario") or "-"] = grouped.get(row.get("scenario") or "-", 0) + 1
        for key, value in grouped.items():
            st.caption(f"{key}: {value}건")
    render_panel_header("생성된 시연 전표", "저장 후에도 유지되는 시연 전표 목록입니다.")
    if seeded_data:
        for item in seeded_data[:50]:
            with stylable_container(key=f"seeded_{item['voucher_key']}", css_styles="""{background: rgba(255,255,255,0.98); border: 1px solid #e5e7eb; border-radius: 16px; padding: 0.85rem 1rem; margin-bottom: 0.6rem; box-shadow: 0 8px 22px rgba(15,23,42,0.04); overflow: hidden;}"""):
                st.markdown(case_type_badge(item.get("risk_type")) + status_badge("READY"), unsafe_allow_html=True)
                st.markdown(f"**{item.get('title') or '-'}**")
                st.caption(f"{item.get('voucher_key')} · {item.get('scenario')} · {fmt_num(item.get('amount'))} {item.get('currency') or ''}")
                st.caption(f"hr={item.get('hr_status') or '-'} · mcc={item.get('mcc_code') or '-'} · budgetExceeded={item.get('budget_exceeded')}")
                action_cols = st.columns([0.62, 0.38])
                with action_cols[1]:
                    if st.button("워크스페이스 열기", key=f"open_workspace_{item['voucher_key']}", use_container_width=True, type="secondary"):
                        st.session_state["mt_menu"] = "AI 워크스페이스"
                        st.session_state["mt_menu_option"] = "AI 워크스페이스"
                        st.session_state["mt_selected_voucher"] = item["voucher_key"]
                        st.rerun()
    else:
        render_empty_state("저장된 시연 전표가 없습니다.")
