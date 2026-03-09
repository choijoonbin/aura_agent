from __future__ import annotations

import streamlit as st
from ui.shared import stylable_container

from ui.api_client import delete, get, post
from ui.shared import case_type_badge, fmt_num, render_empty_state, render_page_header, status_badge


def _demo_section(title: str, subtitle: str, first: bool = False) -> None:
    cls = "mt-demo-section" if not first else "mt-demo-section mt-demo-section-first"
    st.markdown(
        f'<div class="{cls}"><div class="mt-demo-section-title">{title}</div>'
        f'<div class="mt-demo-section-sub">{subtitle}</div></div>',
        unsafe_allow_html=True,
    )


def render_demo_control_page() -> None:
    render_page_header("시연 데이터 제어", "대표 시나리오를 즉시 생성하고, 저장된 시연 전표를 다시 조회할 수 있습니다.")
    scenario_data = get("/api/v1/demo/scenarios").get("items") or []
    seeded_data = get("/api/v1/demo/seeded").get("items") or []

    # Streamlit's JS layout ignores CSS padding for widget width calculation.
    # To contain widgets within a panel, inner st.columns are used for side spacing.
    # Sequential st.columns calls at the same level (level 2) are all valid.
    panel_css = (
        "{ background: rgba(255,255,255,0.98); border: 1px solid #e5e7eb; border-radius: 16px; "
        "padding: 1rem 0 1.25rem 0; "
        "box-shadow: 0 8px 22px rgba(15,23,42,0.05); box-sizing: border-box; }"
    )
    card_css = (
        "{ background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 12px; "
        "padding: 10px 12px; margin: 6px 0 10px 0; box-sizing: border-box; }"
    )

    col_scenario, col_batch = st.columns([0.62, 0.38])

    # ---- 대표 시나리오 패널 ----
    with col_scenario:
        with stylable_container(key="demo_scenario_panel", css_styles=panel_css):
            # Row A (level 2): side padding columns — title, selectbox, card
            _la, mid_a, _ra = st.columns([0.04, 0.92, 0.04])
            with mid_a:
                st.markdown(
                    '<div class="mt-demo-section-title">대표 시나리오</div>',
                    unsafe_allow_html=True,
                )
                option_labels = [s["label"] for s in scenario_data]
                selected_label = st.selectbox(
                    "*시나리오 유형",
                    options=option_labels,
                    key="demo_scenario_select",
                )
                scenario = next(
                    (s for s in scenario_data if s["label"] == selected_label),
                    scenario_data[0] if scenario_data else None,
                )
                with stylable_container(key="demo_scenario_unified", css_styles=card_css):
                    if scenario:
                        st.markdown(
                            f'<div class="mt-demo-scenario-title">{scenario["label"]}</div>',
                            unsafe_allow_html=True,
                        )
                        st.caption(scenario["description"])
                        st.markdown(
                            f'<div style="display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 0 0;">'
                            f'<span class="mt-badge">{scenario["risk_type"]}</span>'
                            f'<span class="mt-badge">금액 {scenario["amount_range"][0]:,}~{scenario["amount_range"][1]:,} KRW</span>'
                            f'<span class="mt-badge">기준 {scenario["day_mode"]}</span></div>',
                            unsafe_allow_html=True,
                        )
            # Row B (level 2, sequential): slider + button on same row
            _lb, sl_col, btn_col, _rb = st.columns([0.04, 0.54, 0.38, 0.04])
            with sl_col:
                count = st.slider("*생성건수", min_value=1, max_value=20, value=5, key="demo_count")
            with btn_col:
                st.markdown('<div style="height:1.75rem"></div>', unsafe_allow_html=True)
                if st.button("생성", key="demo_seed_btn", type="primary") and scenario:
                    out = post("/api/v1/demo/seed", params={"scenario": scenario["scenario"], "count": int(count)})
                    st.success(f"생성 완료: {out['scenario']} / {out['inserted']}건")
                    st.rerun()

    # ---- 일괄 제어 패널 ----
    with col_batch:
        with stylable_container(key="demo_batch_panel", css_styles=panel_css):
            # level 2: side padding columns — all content
            _lc, mid_c, _rc = st.columns([0.05, 0.90, 0.05])
            with mid_c:
                st.markdown(
                    '<div class="mt-demo-section-title">일괄 제어</div>'
                    '<div class="mt-demo-section-sub" style="margin-bottom:10px;">저장된 시연 데이터 현황 및 일괄 삭제</div>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    '모든 시연 데이터는 <span class="mt-badge mt-badge-blue" style="margin:0 4px;">DEMO-*</span> 키로 저장됩니다.',
                    unsafe_allow_html=True,
                )
                st.metric("저장된 시연 전표", len(seeded_data))
                grouped: dict[str, int] = {}
                for row in seeded_data:
                    k = row.get("scenario") or "-"
                    grouped[k] = grouped.get(k, 0) + 1
                for k, v in grouped.items():
                    st.caption(f"{k}: {v}건")
                if st.button("시연 데이터 전체 삭제", key="demo_delete_all"):
                    out = delete("/api/v1/demo/seed")
                    st.warning(
                        "삭제 완료: "
                        f"전표 {out.get('fi_doc_header_deleted', out.get('deleted', 0))}건 / "
                        f"품목 {out.get('fi_doc_item_deleted', 0)}건 / "
                        f"케이스 {out.get('agent_case_deleted', 0)}건 / "
                        f"분석run {out.get('case_analysis_run_deleted', 0)}건 / "
                        f"결과 {out.get('case_analysis_result_deleted', 0)}건 / "
                        f"활동로그 {out.get('agent_activity_log_deleted', 0)}건"
                    )
                    st.rerun()

    # ----- 섹션 2: 생성된 시연 전표 -----
    _demo_section("생성된 시연 전표", "")

    if seeded_data:
        for item in seeded_data[:50]:
            row_css = (
                "{ background: rgba(255,255,255,0.98); border: 1px solid #e5e7eb; border-radius: 14px; "
                "padding: 0.85rem 1rem; margin-bottom: 0.6rem; box-shadow: 0 4px 14px rgba(15,23,42,0.04); "
                "box-sizing: border-box; }"
            )
            with stylable_container(key=f"seeded_{item['voucher_key']}", css_styles=row_css):
                st.markdown(
                    case_type_badge(item.get("case_type") or item.get("risk_type"))
                    + status_badge(item.get("case_status")),
                    unsafe_allow_html=True,
                )
                st.markdown(f"**{item.get('title') or '-'}**")
                st.caption(
                    f"{item.get('voucher_key')} · {item.get('scenario')} · "
                    f"{fmt_num(item.get('amount'))} {item.get('currency') or ''}"
                )
                st.caption(
                    f"hr={item.get('hr_status') or '-'} · mcc={item.get('mcc_code') or '-'} · "
                    f"budgetExceeded={item.get('budget_exceeded')}"
                )
                ac1, ac2 = st.columns([0.7, 0.3])
                with ac2:
                    if st.button(
                        "워크스페이스 열기",
                        key=f"open_workspace_{item['voucher_key']}",
                        type="secondary",
                    ):
                        # mt_menu_option은 사이드바 위젯 키라 직접 수정 불가 → 리다이렉트 키로 넘기고 rerun
                        st.session_state["mt_redirect_to_menu"] = "AI 워크스페이스"
                        st.session_state["mt_selected_voucher"] = item["voucher_key"]
                        st.rerun()
    else:
        render_empty_state("저장된 시연 전표가 없습니다.")
