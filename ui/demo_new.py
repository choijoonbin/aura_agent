"""
시연데이터 생성 (Beta) UI.
업로드 증빙 이미지에서 핵심 엔티티(금액/일자/가맹점)를 자동 추출하고,
사용자 보정 후 테스트 케이스 데이터를 저장한다.
기존 '시연 데이터 제어 (Legacy)'와 완전히 분리된 신규 경로.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

import streamlit as st

from ui.shared import inject_css, render_page_header

logger = logging.getLogger(__name__)

_CASE_TYPE_OPTIONS: list[tuple[str, str]] = [
    ("NORMAL_BASELINE", "정상 비교군 (NORMAL_BASELINE)"),
    ("HOLIDAY_USAGE", "휴일 사용 의심 (HOLIDAY_USAGE)"),
    ("LIMIT_EXCEED", "한도 초과 의심 (LIMIT_EXCEED)"),
    ("PRIVATE_USE_RISK", "사적 사용 위험 (PRIVATE_USE_RISK)"),
    ("UNUSUAL_PATTERN", "비정상 패턴 (UNUSUAL_PATTERN)"),
]
_ABNORMAL_CASE_TYPES = {"HOLIDAY_USAGE", "LIMIT_EXCEED", "PRIVATE_USE_RISK", "UNUSUAL_PATTERN"}


def _check_required_fields(
    amount: str,
    date_occ: str,
    merchant: str,
    bktxt: str,
    user_reason: str,
) -> tuple[bool, list[str]]:
    """5개 필수 항목 유효성 검사. 순수 서비스 함수 위임."""
    from services.demo_data_service import validate_demo_required_fields
    return validate_demo_required_fields(amount, date_occ, merchant, bktxt, user_reason)


def _run_visual_analysis(image_bytes: bytes) -> "Any":
    """이미지 바이트를 analyze_visual_evidence로 전달해 분석 결과 반환."""
    from utils.llm_azure import analyze_visual_evidence
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return analyze_visual_evidence(b64)


def _extract_entity_value(entities: list, label: str) -> str:
    """entities 목록에서 특정 label의 text 반환. 없으면 빈 문자열."""
    for e in entities:
        ent_label = e.label if hasattr(e, "label") else e.get("label", "")
        if ent_label == label:
            return e.text if hasattr(e, "text") else e.get("text", "")
    return ""


def _entities_to_boxes_and_labels(entities: list) -> tuple[list[list[int]], list[str]]:
    """VisualEntity 목록 → boxes([[ymin,xmin,ymax,xmax],...]), labels 변환."""
    _label_map = {
        "amount_total": "금액",
        "date_occurrence": "일자",
        "merchant_name": "가맹점",
    }
    boxes: list[list[int]] = []
    labels: list[str] = []
    for e in entities:
        bbox = e.bbox if hasattr(e, "bbox") else None
        if bbox is None:
            continue
        ymin = bbox.ymin if hasattr(bbox, "ymin") else bbox.get("ymin", 0)
        xmin = bbox.xmin if hasattr(bbox, "xmin") else bbox.get("xmin", 0)
        ymax = bbox.ymax if hasattr(bbox, "ymax") else bbox.get("ymax", 0)
        xmax = bbox.xmax if hasattr(bbox, "xmax") else bbox.get("xmax", 0)
        boxes.append([ymin, xmin, ymax, xmax])
        raw_label = e.label if hasattr(e, "label") else e.get("label", "")
        ent_text = e.text if hasattr(e, "text") else e.get("text", "")
        labels.append(f"{_label_map.get(raw_label, raw_label)}: {ent_text}")
    return boxes, labels


def render_demo_new_page() -> None:
    render_page_header(
        "시연데이터 생성 (Beta)",
        "증빙 이미지 업로드 → 자동 추출 → 보정 → 테스트 케이스 저장",
    )

    # ── 케이스 타입 선택 ──────────────────────────────────
    case_type_labels = [label for _, label in _CASE_TYPE_OPTIONS]
    case_type_keys = [key for key, _ in _CASE_TYPE_OPTIONS]
    selected_label = st.selectbox(
        "케이스 유형",
        options=case_type_labels,
        key="demo_new_case_type_label",
    )
    selected_case_type = case_type_keys[case_type_labels.index(selected_label)]
    is_abnormal = selected_case_type in _ABNORMAL_CASE_TYPES

    if is_abnormal:
        st.warning("비정상 케이스는 증빙 이미지 첨부를 권장합니다. 미첨부 시 금액/일자/가맹점/적요 직접 입력이 필요합니다.")

    st.divider()

    # ── 레이아웃: 좌(이미지+분석) / 우(필드 편집) ──────────
    col_left, col_right = st.columns([1, 1], gap="large")

    with col_left:
        st.subheader("증빙 이미지")
        uploaded_file = st.file_uploader(
            "영수증/전표 이미지 업로드 (JPG/PNG/WEBP)",
            type=["jpg", "jpeg", "png", "webp"],
            key="demo_new_uploader",
        )

        analysis_result = st.session_state.get("demo_new_analysis_result")
        image_bytes: bytes | None = None

        if uploaded_file is not None:
            image_bytes = uploaded_file.read()
            st.session_state["demo_new_image_bytes"] = image_bytes

            if st.button("이미지 분석 실행", key="demo_new_analyze_btn", type="primary"):
                with st.spinner("Vision LLM으로 분석 중..."):
                    result = _run_visual_analysis(image_bytes)
                    st.session_state["demo_new_analysis_result"] = result
                    # 추출 결과를 편집 필드 초기값으로 자동 채우기
                    entities = result.entities if hasattr(result, "entities") else []
                    st.session_state["demo_new_auto_amount"] = _extract_entity_value(entities, "amount_total")
                    st.session_state["demo_new_auto_date"] = _extract_entity_value(entities, "date_occurrence")
                    st.session_state["demo_new_auto_merchant"] = _extract_entity_value(entities, "merchant_name")
                    st.session_state["demo_new_auto_summary"] = (
                        result.suggested_summary if hasattr(result, "suggested_summary") else ""
                    )
                st.rerun()

            analysis_result = st.session_state.get("demo_new_analysis_result")

            # bbox 오버레이 미리보기
            if analysis_result is not None:
                from ui.shared import render_image_with_bboxes

                entities = analysis_result.entities if hasattr(analysis_result, "entities") else []
                boxes, bbox_labels = _entities_to_boxes_and_labels(entities)

                if boxes:
                    st.caption("추출 위치 하이라이트")
                    render_image_with_bboxes(image_bytes, boxes, bbox_labels)
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
            # 이전 분석 결과 초기화
            for key in (
                "demo_new_analysis_result",
                "demo_new_image_bytes",
                "demo_new_auto_amount",
                "demo_new_auto_date",
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
                f"이미지 상태: **{cond}** | 직인: {'있음' if has_stamp else '없음'}"
                + (" | ⚠️ fallback" if fallback else "")
            )
            if analysis_result.audit_comment:
                st.caption(f"감사 코멘트: {analysis_result.audit_comment}")

    with col_right:
        st.subheader("데이터 보정 및 저장")

        # 자동 추출된 값을 초기값으로 (분석 후 처음 한 번만 채움)
        auto_amount = st.session_state.get("demo_new_auto_amount", "")
        auto_date = st.session_state.get("demo_new_auto_date", "")
        auto_merchant = st.session_state.get("demo_new_auto_merchant", "")
        auto_summary = st.session_state.get("demo_new_auto_summary", "")

        # 필수 5개 필드
        amount_val = st.text_input(
            "금액 (amount_total) *",
            value=auto_amount,
            placeholder="예: 97042",
            key="demo_new_field_amount",
        )
        date_val = st.text_input(
            "일자 (date_occurrence) *",
            value=auto_date,
            placeholder="예: 2026-03-14",
            key="demo_new_field_date",
        )
        merchant_val = st.text_input(
            "가맹점 (merchant_name) *",
            value=auto_merchant,
            placeholder="예: 가온 식당",
            key="demo_new_field_merchant",
        )
        bktxt_val = st.text_input(
            "적요 (bktxt) *",
            value=auto_summary if auto_summary else "",
            placeholder="예: 휴일 야간 식대",
            key="demo_new_field_bktxt",
        )
        sgtxt_val = st.text_input(
            "비고 (sgtxt)",
            value="",
            placeholder="예: 야간 업무 관련",
            key="demo_new_field_sgtxt",
        )

        # 사유 (review_questions 기반)
        review_questions = _get_review_questions_for_case_type(selected_case_type)
        if review_questions:
            st.caption("검토 질문 (규정 기반):")
            for q in review_questions:
                st.caption(f"• {q}")

        user_reason_val = st.text_area(
            "사유 (user_reason) *",
            value="",
            placeholder="위 검토 질문에 대한 사유를 입력하세요",
            key="demo_new_field_reason",
            height=90,
        )

        st.divider()

        # 필수 입력 유효성 + 버튼 활성화 판단
        all_valid, validation_errors = _check_required_fields(
            amount_val, date_val, merchant_val, bktxt_val, user_reason_val
        )

        # 비정상 케이스 + 이미지 없음 → 추가 차단
        if is_abnormal and uploaded_file is None:
            st.info("비정상 케이스는 증빙 이미지를 첨부하거나 위 필드를 직접 입력하세요.")

        if validation_errors:
            for err in validation_errors:
                st.caption(f"⚠ {err}")

        generate_disabled = not all_valid

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
                merchant=merchant_val,
                bktxt=bktxt_val,
                sgtxt=sgtxt_val,
                user_reason=user_reason_val,
                image_bytes=st.session_state.get("demo_new_image_bytes"),
                uploaded_filename=uploaded_file.name if uploaded_file else None,
                analysis_result=st.session_state.get("demo_new_analysis_result"),
                review_questions=review_questions,
            )


def _get_review_questions_for_case_type(case_type: str) -> list[str]:
    """케이스 유형별 표준 검토 질문 반환 (에이전트 규정 기반 질문과 동일 구조)."""
    _questions: dict[str, list[str]] = {
        "HOLIDAY_USAGE": [
            "휴일 사용에 대한 사전 승인을 받았습니까?",
            "해당 지출이 업무 목적임을 증명할 수 있습니까?",
        ],
        "LIMIT_EXCEED": [
            "한도 초과에 대한 결재 승인이 있습니까?",
            "접대 상대방 및 목적을 명시할 수 있습니까?",
        ],
        "PRIVATE_USE_RISK": [
            "해당 지출이 업무와 직접 관련된 것임을 증명할 수 있습니까?",
            "사적 사용이 아닌 업무 목적 사용 근거가 있습니까?",
        ],
        "UNUSUAL_PATTERN": [
            "심야/비정상 시간대 사용에 대한 업무상 불가피한 사유가 있습니까?",
            "해당 업종 이용이 업무 목적임을 설명할 수 있습니까?",
        ],
        "NORMAL_BASELINE": [],
    }
    return _questions.get(case_type, [])


def _handle_generate(
    *,
    case_type: str,
    amount: str,
    date_occ: str,
    merchant: str,
    bktxt: str,
    sgtxt: str,
    user_reason: str,
    image_bytes: bytes | None,
    uploaded_filename: str | None,
    analysis_result: "Any",
    review_questions: list[str],
) -> None:
    """테스트 데이터 생성 버튼 클릭 처리."""
    from services.demo_data_service import save_custom_demo_case

    payload: dict[str, Any] = {
        "case_type": case_type,
        "amount_total": amount.replace(",", "").strip(),
        "date_occurrence": date_occ.strip(),
        "merchant_name": merchant.strip(),
        "bktxt": bktxt.strip(),
        "sgtxt": sgtxt.strip(),
        "user_reason": user_reason.strip(),
        "review_questions": review_questions,
        "review_answers": [user_reason.strip()],
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

    with st.spinner("테스트 데이터 저장 중..."):
        try:
            result = save_custom_demo_case(
                payload=payload,
                image_bytes=image_bytes or b"",
                filename=uploaded_filename or "",
            )
            case_uuid = result.get("case_uuid", "-")
            st.success(f"저장 완료! UUID: `{case_uuid}`")
            st.json(result, expanded=False)

            # 저장 후 세션 초기화 (재생성 방지)
            for key in (
                "demo_new_analysis_result",
                "demo_new_image_bytes",
                "demo_new_auto_amount",
                "demo_new_auto_date",
                "demo_new_auto_merchant",
                "demo_new_auto_summary",
            ):
                st.session_state.pop(key, None)

        except Exception as exc:
            logger.exception("demo_new: save_custom_demo_case failed")
            st.error(f"저장 실패: {exc}")
