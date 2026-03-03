from __future__ import annotations

import json
import streamlit as st
from streamlit_extras.stylable_container import stylable_container

from services.rag_chunk_lab_service import list_rulebook_files, load_rulebook_text, preview_chunks, save_uploaded_rulebook
from ui.api_client import get
from ui.shared import render_empty_state, render_kpi_card, render_page_header, render_panel_header


def render_rag_library_page() -> None:
    render_page_header("규정문서 라이브러리", "규정 문서·retrieval·청킹 실험을 한 화면에서 관리합니다. policy_rulebook_probe는 여기 인덱싱된 청크를 검색합니다.")
    data = get("/api/v1/rag/documents")
    items = data.get("items") or []
    total = data.get("total") or len(items)
    indexed = len([item for item in items if str(item.get("status") or "").upper() == "COMPLETED"])
    attention = len([item for item in items if str(item.get("status") or "").upper() in {"PROCESSING", "FAILED", "VECTORIZING"}])
    passed = [item for item in items if item.get("quality_gate_passed") is True or item.get("quality_report_passed") is True]
    pass_rate = (len(passed) / total * 100) if total else 0
    cols = st.columns(4)
    with cols[0]:
        render_kpi_card("문서", str(total), "전체 등록")
    with cols[1]: render_kpi_card("인덱싱됨", str(indexed), "인용 준비 완료")
    with cols[2]: render_kpi_card("주의 필요", str(attention), "인덱싱/오류")
    with cols[3]: render_kpi_card("청킹 합격률", f"{pass_rate:.1f}%", "quality_report 기준")
    top_tabs = st.tabs(["DB 라이브러리", "청킹 실험실"])
    with top_tabs[0]:
        left, right = st.columns([0.48, 0.52])
        selected_doc_id = st.session_state.get("mt_selected_doc_id") or (items[0]["doc_id"] if items else None)
        with left:
            render_panel_header("문서 목록", "저장된 규정/RAG 문서를 상태와 함께 조회합니다.")
            for item in items:
                label = f"{item.get('title')} · doc_id={item.get('doc_id')}"
                if st.button(label, key=f"doc_{item['doc_id']}", use_container_width=True, type="primary" if int(item['doc_id']) == int(selected_doc_id) else "secondary"):
                    st.session_state["mt_selected_doc_id"] = item["doc_id"]
                    st.rerun()
        with right:
            if selected_doc_id is not None:
                detail = get(f"/api/v1/rag/documents/{selected_doc_id}")
                with stylable_container(key=f"rag_doc_hero_{selected_doc_id}", css_styles="""{padding: 18px 20px; border-radius: 18px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.98); box-shadow: 0 10px 24px rgba(15,23,42,0.05); margin-bottom: 12px;}"""):
                    st.markdown(f"## {detail.get('title')}")
                    st.caption(f"doc_id={detail.get('doc_id')} / type={detail.get('doc_type')} / source={detail.get('source_type')}")
                tabs = st.tabs(["문서 메타", "품질 리포트", "청크 목록"])
                with tabs[0]:
                    meta = {k: detail.get(k) for k in ["status", "version", "effective_from", "effective_to", "lifecycle_status", "active_from", "active_to", "quality_gate_passed", "updated_at"]}
                    st.code(json.dumps(meta, indent=2, ensure_ascii=False), language="json")
                with tabs[1]:
                    report = {k: detail.get(k) for k in ["quality_report_passed", "quality_run_id", "input_chunks", "final_chunks", "article_coverage", "noise_rate", "duplicate_rate", "short_chunk_rate", "missing_required", "errors"]}
                    st.code(json.dumps(report, indent=2, ensure_ascii=False), language="json")
                with tabs[2]:
                    for chunk in (detail.get("chunks") or [])[:50]:
                        with st.expander(f"{chunk.get('regulation_article') or '-'} / {chunk.get('parent_title') or '-'} / chunk_id={chunk.get('chunk_id')}"):
                            st.caption(f"page={chunk.get('page_no')} / index={chunk.get('chunk_index')} / version={chunk.get('version')}")
                            st.write(chunk.get("chunk_text") or "")
            else:
                render_empty_state("표시할 문서가 없습니다.")
    with top_tabs[1]:
        render_panel_header("청킹 실험실", "업로드한 규정 텍스트에 전략을 바꿔 적용하며 청킹 결과를 비교합니다. 전략별 차이는 청크 수·평균 길이로 확인할 수 있습니다.")
        local_files = list_rulebook_files()
        upload = st.file_uploader("규정 TXT 업로드", type=["txt"])
        if upload is not None:
            save_uploaded_rulebook(upload.name, upload.getvalue())
            st.success(f"업로드 완료: {upload.name}")
            st.rerun()
        if not local_files:
            render_empty_state("업로드된 규정 파일이 없습니다.")
            return
        left, right = st.columns([0.38, 0.62])
        with left:
            selected_file = st.selectbox("대상 문서", options=local_files, format_func=lambda x: f"{x['name']} ({x['source']})", index=0)
            strategy = st.selectbox("청킹 전략", options=["hybrid_policy", "article_first", "sliding_window"], format_func=lambda x: {"hybrid_policy": "하이브리드 정책형", "article_first": "조항 우선", "sliding_window": "슬라이딩 윈도우"}[x])
            st.caption("하이브리드 정책형은 조항 경계를 먼저 지키고, 긴 조항만 내부 윈도우로 다시 나눕니다.")
        with right:
            text = load_rulebook_text(selected_file["path"])
            chunks = preview_chunks(text, strategy)
            c1, c2, c3 = st.columns(3)
            c1.metric("원문 길이", f"{len(text):,}")
            c2.metric("예상 청크 수", len(chunks))
            c3.metric("평균 길이", f"{(sum(c['length'] for c in chunks) / len(chunks)):.0f}" if chunks else "0")
            st.markdown("#### 전략 비교")
            compare = {
                "하이브리드 정책형": preview_chunks(text, "hybrid_policy"),
                "조항 우선": preview_chunks(text, "article_first"),
                "슬라이딩 윈도우": preview_chunks(text, "sliding_window"),
            }
            compare_cols = st.columns(3)
            for col, (label, rows) in zip(compare_cols, compare.items()):
                with col:
                    with stylable_container(
                        key=f"rag_compare_{label}",
                        css_styles="""{padding: 14px 16px; border-radius: 16px; border: 1px solid #e5e7eb; background: rgba(255,255,255,0.98); box-shadow: 0 8px 22px rgba(15,23,42,0.04); min-height: 148px;}"""
                    ):
                        st.caption(label)
                        st.subheader(str(len(rows)))
                        avg_len = f"{(sum(r['length'] for r in rows) / len(rows)):.0f}" if rows else "0"
                        st.caption(f"평균 길이 {avg_len} chars")
            st.markdown("#### 청크 미리보기")
            for idx, chunk in enumerate(chunks[:12], start=1):
                with st.expander(f"{idx}. {chunk['title']} · {chunk['length']} chars", expanded=(idx == 1)):
                    st.write(chunk["content"])
