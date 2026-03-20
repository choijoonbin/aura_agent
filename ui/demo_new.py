"""
시연데이터 생성 UI.
업로드 증빙 이미지에서 핵심 엔티티(금액/일자/가맹점)를 자동 추출하고,
사용자 보정 후 테스트 케이스 데이터를 저장한다.
기존 '시연 데이터 제어 (Legacy)'와 완전히 분리된 신규 경로.
"""
from __future__ import annotations

import base64
import json
import logging
import unicodedata
from typing import Any

import streamlit as st

from ui.api_client import delete
from ui.shared import inject_css, render_page_header

logger = logging.getLogger(__name__)

_CASE_TYPE_OPTIONS: list[tuple[str, str]] = [
    ("NORMAL_BASELINE", "정상 케이스 (NORMAL_BASELINE)"),
    ("HOLIDAY_USAGE", "휴일 사용 의심 (HOLIDAY_USAGE)"),
    ("LIMIT_EXCEED", "한도 초과 의심 (LIMIT_EXCEED)"),
    ("PRIVATE_USE_RISK", "사적 사용 위험 (PRIVATE_USE_RISK)"),
    ("UNUSUAL_PATTERN", "비정상 패턴 (UNUSUAL_PATTERN)"),
]
_ABNORMAL_CASE_TYPES = {"HOLIDAY_USAGE", "LIMIT_EXCEED", "PRIVATE_USE_RISK", "UNUSUAL_PATTERN"}
_APPROVAL_DOC_OPTIONS: list[tuple[str, str]] = [
    ("", "선택 안 함"),
    ("HOLIDAY_WORK_APPROVAL", "휴일근무 품의서"),
]
_APPROVAL_DOC_PRESETS: dict[str, dict[str, Any]] = {
    "HOLIDAY_WORK_APPROVAL": {
        "title": "휴일근무 품의서",
        "content": "2명이 주말출근 승인을 받아서 회사 근처 식당 이용",
        "attendees": [
            {"name": "내부참석자A", "type": "INTERNAL", "org": "재무팀"},
            {"name": "내부참석자B", "type": "INTERNAL", "org": "재무팀"},
        ],
        "approved": True,
    },
}

_IMAGE_CONDITION_LABELS: dict[str, str] = {
    "clear": "선명",
    "blurry": "흐림",
    "damaged": "훼손",
    "partial_cut": "일부 잘림",
}
_BBOX_COLORS = ["#ef4444", "#3b82f6", "#10b981", "#f59e0b", "#8b5cf6"]
_HOLIDAY_DEFAULTS: dict[str, str] = {
    "amount_total": "68000",
    "merchant_name": "가온식당 강남점",
    "date_occurrence": "2026-03-14",
    "time_occurrence": "23:42",
}


def _check_required_fields(
    amount: str,
    date_occ: str,
    merchant: str,
) -> tuple[bool, list[str]]:
    """핵심 필수 항목(금액/일자/가맹점) 유효성 검사. 순수 서비스 함수 위임."""
    from services.demo_data_service import validate_demo_required_fields
    return validate_demo_required_fields(amount, date_occ, merchant)


def _is_generate_disabled(all_valid: bool, is_abnormal: bool, has_file: bool) -> bool:
    """버튼 비활성화 조건: 필수 필드 미완료 OR 비정상 케이스+파일 미첨부."""
    from services.demo_data_service import is_generate_disabled
    return is_generate_disabled(all_valid, is_abnormal, has_file)


def _run_visual_analysis(image_bytes: bytes) -> "Any":
    """이미지 바이트를 analyze_visual_evidence로 전달해 분석 결과 반환."""
    from utils.llm_azure import analyze_visual_evidence
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return analyze_visual_evidence(b64)


def _image_condition_display(condition: str) -> str:
    token = str(condition or "").strip().lower()
    if not token:
        return "-"
    ko = _IMAGE_CONDITION_LABELS.get(token)
    return f"{ko}({token})" if ko else token


def _extract_entity_value(entities: list, label: str) -> str:
    """entities 목록에서 특정 label의 text 반환. 없으면 빈 문자열."""
    for e in entities:
        ent_label = e.label if hasattr(e, "label") else e.get("label", "")
        if ent_label == label:
            return e.text if hasattr(e, "text") else e.get("text", "")
    return ""


def _normalize_amount_text(text: str) -> str:
    raw = _sanitize_for_compare(text)
    return "".join(ch for ch in raw if ch.isdigit())


def _normalize_date_text(text: str) -> str:
    raw = _sanitize_for_compare(text).strip().replace(".", "-").replace("/", "-")
    parts = [p for p in raw.split("-") if p]
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        y, m, d = parts[0], parts[1].zfill(2), parts[2].zfill(2)
        return f"{y}-{m}-{d}"
    return raw


def _normalize_time_text(text: str) -> str:
    raw = _sanitize_for_compare(text).strip().replace(".", ":").replace("：", ":")
    parts = raw.split(":")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    return raw


def _normalize_merchant_text(text: str) -> str:
    raw = _sanitize_for_compare(text)
    return "".join(ch for ch in raw.lower() if ch.isalnum())


def _sanitize_for_compare(text: str) -> str:
    raw = unicodedata.normalize("NFKC", str(text or ""))
    # zero-width/format 문자 제거로 육안 일치인데 비교 오탐 나는 케이스 차단
    return "".join(ch for ch in raw if unicodedata.category(ch) != "Cf")


def _build_entity_color_map(entities: list) -> dict[str, str]:
    """엔티티 라벨별 bbox 색상(hex) 맵."""
    out: dict[str, str] = {}
    group_idx = 0
    for ent in entities:
        conf = ent.confidence if hasattr(ent, "confidence") else ent.get("confidence", 1.0)
        if float(conf) < 0.5:
            continue
        raw_label = ent.label if hasattr(ent, "label") else ent.get("label", "")
        if not raw_label or raw_label in out:
            group_idx += 1
            continue
        out[str(raw_label)] = _BBOX_COLORS[group_idx % len(_BBOX_COLORS)]
        group_idx += 1
    return out


def _is_field_mismatch(field: str, extracted: str, current: str) -> bool:
    # 동일 텍스트(공백/정규화 포함)는 불일치로 보지 않는다.
    ext_raw = _sanitize_for_compare(extracted).strip()
    cur_raw = _sanitize_for_compare(current).strip()
    # 사용자가 아직 값을 입력하지 않았거나 초기화 타이밍 이슈로 빈 값이면 불일치 표시는 하지 않는다.
    if not cur_raw:
        return False
    if ext_raw == cur_raw:
        return False
    # 특수문자/공백 제거 후 동일하면 일치로 간주한다.
    ext_compact = "".join(ch for ch in ext_raw.lower() if ch.isalnum())
    cur_compact = "".join(ch for ch in cur_raw.lower() if ch.isalnum())
    if ext_compact and cur_compact and ext_compact == cur_compact:
        return False
    if not str(extracted or "").strip():
        return False
    if field == "amount_total":
        return _normalize_amount_text(extracted) != _normalize_amount_text(current)
    if field == "merchant_name":
        return _normalize_merchant_text(extracted) != _normalize_merchant_text(current)
    if field == "date_occurrence":
        return _normalize_date_text(extracted) != _normalize_date_text(current)
    if field == "time_occurrence":
        return _normalize_time_text(extracted) != _normalize_time_text(current)
    return False


def _mismatch_badge_label(title: str, is_mismatch: bool) -> str:
    badge_common = (
        "display:inline-block;padding:2px 8px;border-radius:999px;"
        "font-size:12px;font-weight:700;line-height:1.2;"
        "border:1px solid #fecaca;min-width:46px;text-align:center;"
    )
    if is_mismatch:
        badge = f'<span style="{badge_common}background:#fee2e2;color:#991b1b;">불일치</span>'
    else:
        # 배지 공간 고정: 미표시 상태에서도 동일 간격 유지
        badge = f'<span style="{badge_common}visibility:hidden;background:transparent;color:transparent;">불일치</span>'
    return f"{title} {badge}"


def _entities_to_boxes_and_labels(
    entities: list,
) -> tuple[list[list[int]], list[str], list[int]]:
    """VisualEntity 목록 → boxes, labels, color_groups 변환.

    엔티티당 최대 2개 박스를 생성합니다:
    - bbox_key: 항목명 위치 → 색상 그룹과 동일 색, 라벨 "[항목명]"
    - bbox (값): 실제 값 위치 → 같은 색상 그룹, 라벨 "항목명: 값"
    같은 그룹 인덱스를 공유하므로 render_image_with_bboxes에서 동일 색으로 렌더링됩니다.
    """
    _label_map = {
        "amount_total": "금액",
        "date_occurrence": "일자",
        "time_occurrence": "시간",
        "merchant_name": "가맹점",
    }
    boxes: list[list[int]] = []
    labels: list[str] = []
    color_groups: list[int] = []

    for group_idx, e in enumerate(entities):
        # 신뢰도 낮은 entity 제외
        conf = e.confidence if hasattr(e, "confidence") else e.get("confidence", 1.0)
        if float(conf) < 0.5:
            continue

        raw_label = e.label if hasattr(e, "label") else e.get("label", "")
        ent_text = e.text if hasattr(e, "text") else e.get("text", "")
        short_name = _label_map.get(raw_label, raw_label)

        def _box_coords(b: object) -> list[int] | None:
            if b is None:
                return None
            ymin = b.ymin if hasattr(b, "ymin") else b.get("ymin", 0)  # type: ignore[union-attr]
            xmin = b.xmin if hasattr(b, "xmin") else b.get("xmin", 0)  # type: ignore[union-attr]
            ymax = b.ymax if hasattr(b, "ymax") else b.get("ymax", 0)  # type: ignore[union-attr]
            xmax = b.xmax if hasattr(b, "xmax") else b.get("xmax", 0)  # type: ignore[union-attr]
            return [ymin, xmin, ymax, xmax]

        # 항목명(키) 박스 — 라벨 태그 있음
        bbox_key = e.bbox_key if hasattr(e, "bbox_key") else e.get("bbox_key")
        key_coords = _box_coords(bbox_key)
        if key_coords is not None:
            boxes.append(key_coords)
            labels.append(f"[{short_name}]")
            color_groups.append(group_idx)

        # 값 박스 — 라벨 태그에 값 텍스트 표시
        bbox_val = e.bbox if hasattr(e, "bbox") else e.get("bbox")
        val_coords = _box_coords(bbox_val)
        if val_coords is not None:
            boxes.append(val_coords)
            labels.append(f"{short_name}: {ent_text}")
            color_groups.append(group_idx)

    return boxes, labels, color_groups


def render_demo_new_page() -> None:
    render_page_header(
        "시연데이터 생성",
        "증빙 기반으로 시연 케이스를 빠르게 생성하고 검토할 수 있습니다.",
    )
    st.markdown(
        """
        <style>
        [data-testid="stFileUploaderDropzone"] {
          background: #eaf2ff !important;
          border: 1px dashed #93c5fd !important;
        }
        [data-testid="stFileUploaderDropzone"] * {
          color: #0f172a !important;
        }
        [data-testid="stFileUploaderDropzone"] button {
          background: #ffffff !important;
          color: #0f172a !important;
          border: 1px solid #bfdbfe !important;
        }
        [data-testid="stFileUploaderDropzone"] button:hover {
          background: #f8fbff !important;
          border-color: #93c5fd !important;
        }
        /* 우측 입력 필드 가독성 개선 */
        [data-testid="stTextInput"] input {
          background: #ffffff !important;
          color: #0f172a !important;
          border: 1px solid #cbd5e1 !important;
        }
        [data-testid="stTextInput"] input::placeholder {
          color: #64748b !important;
          opacity: 1 !important;
        }
        [data-testid="stTextInput"] input:focus {
          border-color: #60a5fa !important;
          box-shadow: 0 0 0 1px #93c5fd !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # ── 케이스 타입 선택 + 전체 삭제(legacy 동일 동작) ─────
    case_type_labels = [label for _, label in _CASE_TYPE_OPTIONS]
    case_type_keys = [key for key, _ in _CASE_TYPE_OPTIONS]
    case_type_col, delete_col = st.columns([0.74, 0.26])
    with case_type_col:
        selected_label = st.selectbox(
            "케이스 유형",
            options=case_type_labels,
            key="demo_new_case_type_label",
        )
    with delete_col:
        # selectbox 레이블 높이에 맞춰 버튼을 같은 라인 우측 끝에 배치
        st.markdown('<div style="height:1.75rem"></div>', unsafe_allow_html=True)
        if st.button("시연 데이터 전체 삭제", key="demo_new_delete_all", use_container_width=True):
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

    selected_case_type = case_type_keys[case_type_labels.index(selected_label)]
    is_abnormal = selected_case_type in _ABNORMAL_CASE_TYPES
    is_normal_baseline = selected_case_type == "NORMAL_BASELINE"

    prev_case_type = str(st.session_state.get("demo_new_prev_case_type") or "")
    # HOLIDAY_USAGE는 POC 시연 기본값을 즉시 채워 시작한다.
    if selected_case_type == "HOLIDAY_USAGE" and prev_case_type != "HOLIDAY_USAGE":
        st.session_state["demo_new_field_amount"] = _HOLIDAY_DEFAULTS["amount_total"]
        st.session_state["demo_new_field_merchant"] = _HOLIDAY_DEFAULTS["merchant_name"]
        st.session_state["demo_new_field_date"] = _HOLIDAY_DEFAULTS["date_occurrence"]
        st.session_state["demo_new_field_time"] = _HOLIDAY_DEFAULTS["time_occurrence"]
    # 동일 케이스 재진입/새로고침 시에도 기본값이 비어 있으면 보정한다.
    if selected_case_type == "HOLIDAY_USAGE":
        if not str(st.session_state.get("demo_new_field_amount") or "").strip():
            st.session_state["demo_new_field_amount"] = _HOLIDAY_DEFAULTS["amount_total"]
        if not str(st.session_state.get("demo_new_field_merchant") or "").strip():
            st.session_state["demo_new_field_merchant"] = _HOLIDAY_DEFAULTS["merchant_name"]
        if not str(st.session_state.get("demo_new_field_date") or "").strip():
            st.session_state["demo_new_field_date"] = _HOLIDAY_DEFAULTS["date_occurrence"]
        if not str(st.session_state.get("demo_new_field_time") or "").strip():
            st.session_state["demo_new_field_time"] = _HOLIDAY_DEFAULTS["time_occurrence"]
    st.session_state["demo_new_prev_case_type"] = selected_case_type

    if is_abnormal:
        st.warning("비정상 케이스는 증빙 이미지 첨부를 권장합니다. 미첨부 시 금액/일자/가맹점을 직접 입력해야 합니다.")

    st.divider()

    # ── 레이아웃: 좌(필드 편집) / 우(이미지+분석) ──────────
    col_right, col_left = st.columns([1, 1], gap="large")

    with col_left:
        st.subheader("증빙 이미지")

        if is_normal_baseline:
            for key in (
                "demo_new_uploader",
                "demo_new_analysis_result",
                "demo_new_image_bytes",
                "demo_new_auto_amount",
                "demo_new_auto_date",
                "demo_new_auto_time",
                "demo_new_auto_merchant",
                "demo_new_auto_summary",
            ):
                st.session_state.pop(key, None)

        uploaded_file = st.file_uploader(
            "영수증/전표 이미지 업로드 (JPG/PNG/WEBP)",
            type=["jpg", "jpeg", "png", "webp"],
            key="demo_new_uploader",
            disabled=is_normal_baseline,
        )

        analysis_result = st.session_state.get("demo_new_analysis_result")
        image_bytes: bytes | None = None

        if is_normal_baseline:
            st.info("정상 케이스는 증빙 이미지 업로드를 사용하지 않습니다.")
            uploaded_file = None
            analysis_result = None
            image_bytes = None
        elif uploaded_file is not None:
            image_bytes = uploaded_file.read()
            st.session_state["demo_new_image_bytes"] = image_bytes

            if st.button("이미지 분석 실행", key="demo_new_analyze_btn", type="primary"):
                with st.spinner("Vision LLM으로 분석 중..."):
                    result = _run_visual_analysis(image_bytes)
                    st.session_state["demo_new_analysis_result"] = result
                    # 추출 결과는 불일치 검출/하이라이트 용도로만 사용한다.
                    # 편집 필드 값은 자동으로 덮어쓰지 않는다.
                    entities = result.entities if hasattr(result, "entities") else []
                    st.session_state["demo_new_entity_color_map"] = _build_entity_color_map(entities)
                st.rerun()

            analysis_result = st.session_state.get("demo_new_analysis_result")

            # bbox 오버레이 미리보기
            if analysis_result is not None:
                from ui.shared import render_image_with_bboxes

                entities = analysis_result.entities if hasattr(analysis_result, "entities") else []
                boxes, bbox_labels, color_groups = _entities_to_boxes_and_labels(entities)
                st.session_state["demo_new_entity_color_map"] = _build_entity_color_map(entities)

                if boxes:
                    st.caption("추출 위치 하이라이트 (항목명·값 동일 색 매칭)")
                    render_image_with_bboxes(image_bytes, boxes, bbox_labels, color_groups=color_groups)
                else:
                    st.image(image_bytes, use_container_width=True)
                    if getattr(analysis_result, "fallback_used", False):
                        st.warning(f"분석 실패 (fallback): {analysis_result.audit_comment}")
                    else:
                        st.caption("추출된 bbox가 없습니다.")
            else:
                st.image(image_bytes, use_container_width=True)

        else:
            st.info("이미지를 업로드하면 자동 분석이 가능합니다.")
            # 이전 분석 결과 및 자동 채우기 위젯 키 초기화
            for key in (
                "demo_new_analysis_result",
                "demo_new_image_bytes",
                "demo_new_auto_amount",
                "demo_new_auto_date",
                "demo_new_auto_time",
                "demo_new_auto_merchant",
                "demo_new_auto_summary",
            ):
                st.session_state.pop(key, None)
            image_bytes = None
            analysis_result = None

        # 분석 정보 표시
        if analysis_result is not None:
            cond = (analysis_result.image_analysis or {}).get("condition", "-")
            has_stamp = (analysis_result.image_analysis or {}).get("has_stamp", False)
            fallback = getattr(analysis_result, "fallback_used", False)
            st.caption(
                f"이미지 상태: **{_image_condition_display(str(cond))}** | 직인: {'있음' if has_stamp else '없음'}"
                + (" | ⚠️ fallback" if fallback else "")
            )
            if analysis_result.audit_comment:
                st.caption(f"감사 코멘트: {analysis_result.audit_comment}")

    with col_right:
        st.subheader("데이터 보정 및 저장")

        # 자동 추출된 값을 초기값으로 (분석 후 처음 한 번만 채움)
        auto_amount = st.session_state.get("demo_new_auto_amount", "")
        auto_date = st.session_state.get("demo_new_auto_date", "")
        auto_time = st.session_state.get("demo_new_auto_time", "")
        auto_merchant = st.session_state.get("demo_new_auto_merchant", "")
        auto_summary = st.session_state.get("demo_new_auto_summary", "")

        analysis_result = st.session_state.get("demo_new_analysis_result")
        entities_for_mismatch = analysis_result.entities if (analysis_result is not None and hasattr(analysis_result, "entities")) else []
        extracted_map = {
            "amount_total": _extract_entity_value(entities_for_mismatch, "amount_total"),
            "merchant_name": _extract_entity_value(entities_for_mismatch, "merchant_name"),
            "date_occurrence": _extract_entity_value(entities_for_mismatch, "date_occurrence"),
            "time_occurrence": _extract_entity_value(entities_for_mismatch, "time_occurrence"),
        }
        if "demo_new_field_amount" not in st.session_state and auto_amount:
            st.session_state["demo_new_field_amount"] = str(auto_amount)
        if "demo_new_field_merchant" not in st.session_state and auto_merchant:
            st.session_state["demo_new_field_merchant"] = str(auto_merchant)
        if "demo_new_field_date" not in st.session_state and auto_date:
            st.session_state["demo_new_field_date"] = str(auto_date)
        if "demo_new_field_time" not in st.session_state and auto_time:
            st.session_state["demo_new_field_time"] = str(auto_time)

        mismatch_state = dict(st.session_state.get("demo_new_mismatch_state") or {})
        if analysis_result is None:
            mismatch_state = {}

        # 입력 레이아웃: 2, 3
        # 1행: 금액 / 가맹점
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(
                _mismatch_badge_label("금액 *", bool(mismatch_state.get("amount_total"))),
                unsafe_allow_html=True,
            )
            amount_val = st.text_input(
                "금액 *",
                placeholder="예: 97042",
                key="demo_new_field_amount",
                label_visibility="collapsed",
            )
            if mismatch_state.get("amount_total"):
                st.caption(f"이미지추출값: {extracted_map.get('amount_total')}")
        with c2:
            st.markdown(
                _mismatch_badge_label("가맹점 *", bool(mismatch_state.get("merchant_name"))),
                unsafe_allow_html=True,
            )
            merchant_val = st.text_input(
                "가맹점 *",
                placeholder="예: 가온 식당",
                key="demo_new_field_merchant",
                label_visibility="collapsed",
            )
            if mismatch_state.get("merchant_name"):
                st.caption(f"이미지추출값: {extracted_map.get('merchant_name')}")

        # 2행: 일자 / 시간 / 적요
        c4, c5, c6 = st.columns(3)
        with c4:
            st.markdown(
                _mismatch_badge_label("사용일자 *", bool(mismatch_state.get("date_occurrence"))),
                unsafe_allow_html=True,
            )
            date_val = st.text_input(
                "사용일자 *",
                placeholder="예: 2026-03-14",
                key="demo_new_field_date",
                label_visibility="collapsed",
            )
            if mismatch_state.get("date_occurrence"):
                st.caption(f"이미지추출값: {extracted_map.get('date_occurrence')}")
        with c5:
            st.markdown(
                _mismatch_badge_label("사용시간 *", bool(mismatch_state.get("time_occurrence"))),
                unsafe_allow_html=True,
            )
            time_val = st.text_input(
                "사용시간 *",
                placeholder="예: 19:45",
                key="demo_new_field_time",
                help="영수증의 거래시간. 이미지 분석 시 자동 추출. HH:MM 형식 (24시간제)",
                label_visibility="collapsed",
            )
            if mismatch_state.get("time_occurrence"):
                st.caption(f"이미지추출값: {extracted_map.get('time_occurrence')}")
        with c6:
            st.markdown(
                _mismatch_badge_label("적요 (bktxt)", False),
                unsafe_allow_html=True,
            )
            bktxt_val = st.text_input(
                "적요 (bktxt)",
                value=auto_summary if auto_summary else "",
                placeholder="예: 휴일 야간 식대",
                key="demo_new_field_bktxt",
                label_visibility="collapsed",
            )

        recalculated_mismatch = {
            "amount_total": _is_field_mismatch("amount_total", extracted_map.get("amount_total", ""), amount_val),
            "merchant_name": _is_field_mismatch("merchant_name", extracted_map.get("merchant_name", ""), merchant_val),
            "date_occurrence": _is_field_mismatch("date_occurrence", extracted_map.get("date_occurrence", ""), date_val),
            "time_occurrence": _is_field_mismatch("time_occurrence", extracted_map.get("time_occurrence", ""), time_val),
        }
        debug_rows = {
            "amount_total": {
                "extracted_raw": extracted_map.get("amount_total", ""),
                "current_raw": amount_val,
                "extracted_norm": _normalize_amount_text(extracted_map.get("amount_total", "")),
                "current_norm": _normalize_amount_text(amount_val),
                "mismatch": recalculated_mismatch.get("amount_total", False),
            },
            "merchant_name": {
                "extracted_raw": extracted_map.get("merchant_name", ""),
                "current_raw": merchant_val,
                "extracted_norm": _normalize_merchant_text(extracted_map.get("merchant_name", "")),
                "current_norm": _normalize_merchant_text(merchant_val),
                "mismatch": recalculated_mismatch.get("merchant_name", False),
            },
            "date_occurrence": {
                "extracted_raw": extracted_map.get("date_occurrence", ""),
                "current_raw": date_val,
                "extracted_norm": _normalize_date_text(extracted_map.get("date_occurrence", "")),
                "current_norm": _normalize_date_text(date_val),
                "mismatch": recalculated_mismatch.get("date_occurrence", False),
            },
            "time_occurrence": {
                "extracted_raw": extracted_map.get("time_occurrence", ""),
                "current_raw": time_val,
                "extracted_norm": _normalize_time_text(extracted_map.get("time_occurrence", "")),
                "current_norm": _normalize_time_text(time_val),
                "mismatch": recalculated_mismatch.get("time_occurrence", False),
            },
        }
        if st.checkbox("불일치 비교 로그 보기", key="demo_new_show_mismatch_debug", value=False):
            st.json(debug_rows, expanded=False)
        if analysis_result is None:
            recalculated_mismatch = {}
        if recalculated_mismatch != mismatch_state:
            st.session_state["demo_new_mismatch_state"] = recalculated_mismatch
            st.rerun()

        st.markdown('<div style="height:8px;"></div>', unsafe_allow_html=True)
        doc_labels = [label for _, label in _APPROVAL_DOC_OPTIONS]
        doc_keys = [key for key, _ in _APPROVAL_DOC_OPTIONS]
        opt_left, opt_right = st.columns(2)
        with opt_left:
            selected_doc_label = st.selectbox(
                "품의서(전자결재)",
                options=doc_labels,
                key="demo_new_approval_doc_label",
                help="POC 시연용 하드코딩 품의서를 선택합니다.",
            )
        with opt_right:
            create_count = st.slider(
                "생성건수",
                min_value=1,
                max_value=20,
                value=1,
                key="demo_new_create_count",
            )
        selected_doc_key = doc_keys[doc_labels.index(selected_doc_label)]
        approval_doc = _APPROVAL_DOC_PRESETS.get(selected_doc_key)
        if approval_doc:
            st.caption(f"선택된 품의서: {approval_doc.get('title')}")
            st.caption(f"내용: {approval_doc.get('content')}")

        st.markdown('<div style="height:8px;"></div>', unsafe_allow_html=True)

        # 필수 입력 유효성 + 버튼 활성화 판단
        # NORMAL_BASELINE은 즉시 생성 가능하도록 필수값 검증을 우회한다.
        if selected_case_type == "NORMAL_BASELINE":
            all_valid, validation_errors = True, []
        else:
            all_valid, validation_errors = _check_required_fields(
                amount_val, date_val, merchant_val
            )

        if validation_errors:
            for err in validation_errors:
                st.caption(f"⚠ {err}")

        # 비정상 케이스 + 파일 미첨부 → 버튼 항상 disabled (스펙 정책)
        if is_abnormal and uploaded_file is None:
            st.info("비정상 케이스는 증빙 이미지 첨부가 필수입니다.")

        generate_disabled = _is_generate_disabled(all_valid, is_abnormal, uploaded_file is not None)

        if st.button(
            "테스트 데이터 생성",
            key="demo_new_generate_btn",
            type="primary",
            disabled=generate_disabled,
        ):
            _handle_generate(
                case_type=selected_case_type,
                amount=amount_val,
                date_occ=date_val,
                time_occ=time_val,
                merchant=merchant_val,
                bktxt=bktxt_val,
                approval_doc=approval_doc,
                create_count=int(create_count),
                image_bytes=st.session_state.get("demo_new_image_bytes"),
                uploaded_filename=uploaded_file.name if uploaded_file else None,
                analysis_result=st.session_state.get("demo_new_analysis_result"),
            )


def _handle_generate(
    *,
    case_type: str,
    amount: str,
    date_occ: str,
    time_occ: str,
    merchant: str,
    bktxt: str,
    approval_doc: dict[str, Any] | None,
    create_count: int,
    image_bytes: bytes | None,
    uploaded_filename: str | None,
    analysis_result: "Any",
) -> None:
    """테스트 데이터 생성 버튼 클릭 처리."""
    from services.demo_data_service import save_custom_demo_case

    payload: dict[str, Any] = {
        "case_type": case_type,
        "amount_total": amount.replace(",", "").strip(),
        "date_occurrence": date_occ.strip(),
        "time_occurrence": time_occ.strip(),
        "merchant_name": merchant.strip(),
        "bktxt": bktxt.strip(),
        "approval_doc": approval_doc or {},
    }

    # 분석 결과 직렬화
    if analysis_result is not None:
        try:
            payload["extracted_entities"] = [
                {
                    "id": e.id,
                    "label": e.label,
                    "text": e.text,
                    "confidence": e.confidence,
                    "bbox": {
                        "ymin": e.bbox.ymin,
                        "xmin": e.bbox.xmin,
                        "ymax": e.bbox.ymax,
                        "xmax": e.bbox.xmax,
                    },
                }
                for e in (analysis_result.entities or [])
            ]
            payload["model_source"] = getattr(analysis_result, "source", "vision_llm")
            payload["fallback_used"] = getattr(analysis_result, "fallback_used", False)
        except Exception as e:
            logger.warning("demo_new: entity serialization failed: %s", e)
            payload["extracted_entities"] = []
            payload["model_source"] = "vision_llm"
            payload["fallback_used"] = True

    target_count = max(1, min(int(create_count or 1), 20))

    with st.spinner(f"테스트 데이터 저장 중... ({target_count}건)"):
        try:
            from db.session import SessionLocal

            db = SessionLocal()
            try:
                results: list[dict[str, Any]] = []
                for _ in range(target_count):
                    one = save_custom_demo_case(
                        payload=dict(payload),
                        image_bytes=image_bytes or b"",
                        filename=uploaded_filename or "",
                        db=db,
                    )
                    results.append(one)
            finally:
                db.close()
            first_result = results[0] if results else {}
            case_uuid = first_result.get("case_uuid", "-")
            voucher_keys = [str(r.get("voucher_key") or "").strip() for r in results]
            voucher_keys = [v for v in voucher_keys if v]

            success_msg = f"저장 완료! 총 `{len(results)}`건 생성"
            if voucher_keys:
                if len(voucher_keys) == 1:
                    success_msg += f"  |  전표: `{voucher_keys[0]}`"
                else:
                    success_msg += f"  |  전표: `{voucher_keys[0]}` ~ `{voucher_keys[-1]}`"
            else:
                success_msg += f"  |  첫 UUID: `{case_uuid}`"
            st.success(success_msg)
            pretty_payload = {
                "count": len(results),
                "items": results,
            }
            pretty_result = json.dumps(pretty_payload, ensure_ascii=False, indent=2)
            st.markdown(
                (
                    '<div style="background:#f8fafc;color:#0f172a;border:1px solid #cbd5e1;'
                    'border-radius:8px;padding:12px 14px;margin-top:8px;">'
                    '<div style="font-weight:700;margin-bottom:6px;">저장 결과</div>'
                    f'<pre style="margin:0;white-space:pre-wrap;color:#0f172a;">{pretty_result}</pre>'
                    "</div>"
                ),
                unsafe_allow_html=True,
            )

            # 저장 후 세션 초기화 (재생성 방지)
            for key in (
                "demo_new_analysis_result",
                "demo_new_image_bytes",
                "demo_new_auto_amount",
                "demo_new_auto_date",
                "demo_new_auto_merchant",
                "demo_new_auto_summary",
                "demo_new_field_reason",
            ):
                st.session_state.pop(key, None)

        except Exception as exc:
            logger.exception("demo_new: save_custom_demo_case failed")
            st.error(f"저장 실패: {exc}")
