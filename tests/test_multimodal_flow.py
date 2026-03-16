"""
멀티모달 플로우 테스트 (Sprint 1: 독립 시연 도구 범위).

테스트 범위:
  1. bbox 좌표 범위 검증 (VisualBox Pydantic 모델)
  2. 정상비교군(NORMAL_BASELINE) 회귀 - 증빙 없이 generate 가능
  3. 비정상 케이스 필수 입력 차단 로직
  4. analyze_visual_evidence fallback (API 키 없을 때 fallback_used=True)
  5. save_custom_demo_case 저장 무결성 (uuid 폴더 + json + 이미지)
  6. 필수 5개 항목 유효성 검사 함수 (_check_required_fields)
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# 1. bbox 좌표 범위 검증
# ──────────────────────────────────────────────────────────────────────────────


def test_visual_box_valid():
    from agent.output_models import VisualBox

    box = VisualBox(ymin=100, xmin=200, ymax=300, xmax=400)
    assert box.ymin == 100
    assert box.xmax == 400


def test_visual_box_boundary_zero_to_thousand():
    from agent.output_models import VisualBox

    box = VisualBox(ymin=0, xmin=0, ymax=1000, xmax=1000)
    assert box.ymin == 0
    assert box.xmax == 1000


def test_visual_box_out_of_range_raises():
    from agent.output_models import VisualBox
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VisualBox(ymin=-1, xmin=0, ymax=100, xmax=100)

    with pytest.raises(ValidationError):
        VisualBox(ymin=0, xmin=0, ymax=1001, xmax=100)


def test_visual_box_inverted_coords_raises():
    from agent.output_models import VisualBox
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VisualBox(ymin=500, xmin=0, ymax=100, xmax=100)  # ymin > ymax


def test_multimodal_audit_result_defaults():
    from agent.output_models import MultimodalAuditResult

    result = MultimodalAuditResult()
    assert result.fallback_used is False
    assert result.entities == []
    assert result.source == "vision_llm"


# ──────────────────────────────────────────────────────────────────────────────
# 2. 정상비교군(NORMAL_BASELINE) 회귀: 증빙 없이 generate 가능
# ──────────────────────────────────────────────────────────────────────────────


def test_generate_preview_questions_normal_baseline():
    from services.demo_data_service import generate_preview_questions

    result = generate_preview_questions("NORMAL_BASELINE", {})
    assert result["required_inputs"] == []
    assert result["review_questions"] == []


def test_generate_preview_questions_abnormal_has_questions():
    from services.demo_data_service import generate_preview_questions

    for case_type in ("HOLIDAY_USAGE", "LIMIT_EXCEED", "PRIVATE_USE_RISK", "UNUSUAL_PATTERN"):
        result = generate_preview_questions(case_type, {})
        assert len(result["review_questions"]) > 0, f"{case_type} should have review_questions"
        assert len(result["required_inputs"]) > 0, f"{case_type} should have required_inputs"


# ──────────────────────────────────────────────────────────────────────────────
# 3. 비정상 케이스 필수 입력 차단 로직 (순수 서비스 함수로 테스트)
# ──────────────────────────────────────────────────────────────────────────────


def test_check_required_fields_all_valid():
    from services.demo_data_service import validate_demo_required_fields

    ok, errors = validate_demo_required_fields(
        amount="97042",
        date_occ="2026-03-14",
        merchant="가온 식당",
        bktxt="휴일 야간 식대",
        user_reason="업무상 불가피한 상황",
    )
    assert ok is True
    assert errors == []


def test_check_required_fields_missing_amount():
    from services.demo_data_service import validate_demo_required_fields

    ok, errors = validate_demo_required_fields(
        amount="",
        date_occ="2026-03-14",
        merchant="가온 식당",
        bktxt="식대",
        user_reason="사유",
    )
    assert ok is False
    assert any("금액" in e for e in errors)


def test_check_required_fields_invalid_date():
    from services.demo_data_service import validate_demo_required_fields

    ok, errors = validate_demo_required_fields(
        amount="10000",
        date_occ="20260314",  # 잘못된 형식
        merchant="식당",
        bktxt="식대",
        user_reason="사유",
    )
    assert ok is False
    assert any("일자" in e for e in errors)


def test_check_required_fields_zero_amount():
    from services.demo_data_service import validate_demo_required_fields

    ok, errors = validate_demo_required_fields(
        amount="0",
        date_occ="2026-03-14",
        merchant="식당",
        bktxt="식대",
        user_reason="사유",
    )
    assert ok is False
    assert any("금액" in e for e in errors)


def test_check_required_fields_all_five_required():
    """5개 항목 중 하나라도 누락 시 False 반환."""
    from services.demo_data_service import validate_demo_required_fields

    # 사유 누락
    ok, errors = validate_demo_required_fields(
        amount="10000",
        date_occ="2026-03-14",
        merchant="식당",
        bktxt="식대",
        user_reason="",
    )
    assert ok is False

    # 적요 누락
    ok, errors = validate_demo_required_fields(
        amount="10000",
        date_occ="2026-03-14",
        merchant="식당",
        bktxt="",
        user_reason="사유",
    )
    assert ok is False

    # 가맹점 누락
    ok, errors = validate_demo_required_fields(
        amount="10000",
        date_occ="2026-03-14",
        merchant="",
        bktxt="식대",
        user_reason="사유",
    )
    assert ok is False


# ──────────────────────────────────────────────────────────────────────────────
# 3b. 비정상 케이스 + 파일 미첨부 → generate_disabled=True (UI 정책 로직 검증)
# ──────────────────────────────────────────────────────────────────────────────


def test_is_generate_disabled_abnormal_no_file():
    """비정상 케이스 + 파일 미첨부 시 generate_disabled=True (스펙 정책)."""
    from services.demo_data_service import is_generate_disabled as _is_generate_disabled

    # 비정상 + 파일 없음 → 5개 필드 모두 OK여도 disabled
    assert _is_generate_disabled(all_valid=True, is_abnormal=True, has_file=False) is True

    # 비정상 + 파일 있음 + 필드 OK → enabled
    assert _is_generate_disabled(all_valid=True, is_abnormal=True, has_file=True) is False

    # 정상 비교군 + 파일 없음 + 필드 OK → enabled (회귀 유지)
    assert _is_generate_disabled(all_valid=True, is_abnormal=False, has_file=False) is False

    # 정상/비정상 무관, 필드 미완 → disabled
    assert _is_generate_disabled(all_valid=False, is_abnormal=False, has_file=True) is True
    assert _is_generate_disabled(all_valid=False, is_abnormal=True, has_file=True) is True


# ──────────────────────────────────────────────────────────────────────────────
# 4. analyze_visual_evidence fallback (API 키 없을 때)
# ──────────────────────────────────────────────────────────────────────────────


def test_analyze_visual_evidence_fallback_when_no_api_key():
    """API 키 미설정 시 fallback_used=True의 MultimodalAuditResult 반환."""
    from utils.llm_azure import analyze_visual_evidence

    # openai_api_key를 None으로 패치
    with patch("utils.llm_azure.os.getenv", side_effect=lambda k, d=None: None if k == "OPENAI_API_KEY" else d):
        with patch("utils.config.settings") as mock_settings:
            mock_settings.openai_api_key = None
            mock_settings.openai_base_url = ""
            mock_settings.openai_api_version = "2024-12-01-preview"
            result = analyze_visual_evidence("fake_base64_data")

    assert result.fallback_used is True
    assert result.entities == []


def test_analyze_visual_evidence_fallback_on_exception():
    """예외 발생 시 fallback_used=True 반환 (기능 중단 없음)."""
    from utils.llm_azure import analyze_visual_evidence

    with patch("utils.config.settings") as mock_settings:
        mock_settings.openai_api_key = "fake-key"
        mock_settings.openai_base_url = ""
        mock_settings.openai_api_version = "2024-12-01-preview"

        # utils.llm_azure 내부의 openai import를 실패로 강제
        import sys
        import types

        fake_openai = types.ModuleType("openai")
        fake_openai.AzureOpenAI = None  # type: ignore
        def _raise(*a, **kw):
            raise RuntimeError("network error")
        fake_openai.OpenAI = _raise  # type: ignore

        with patch.dict(sys.modules, {"openai": fake_openai}):
            result = analyze_visual_evidence("fake_base64_data")

    assert result.fallback_used is True


# ──────────────────────────────────────────────────────────────────────────────
# 5. save_custom_demo_case 저장 무결성
# ──────────────────────────────────────────────────────────────────────────────


def test_save_custom_demo_case_creates_uuid_folder_with_files(tmp_path):
    """data/evidence_uploads/{uuid}/ 에 이미지 + meta.json 저장 확인."""
    import services.demo_data_service as svc

    # 임시 디렉토리로 저장 경로 교체
    original_root = svc._EVIDENCE_UPLOAD_ROOT
    svc._EVIDENCE_UPLOAD_ROOT = tmp_path / "evidence_uploads"

    try:
        dummy_image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # 가짜 PNG bytes

        result = svc.save_custom_demo_case(
            payload={
                "case_type": "HOLIDAY_USAGE",
                "amount_total": "97042",
                "date_occurrence": "2026-03-14",
                "merchant_name": "가온 식당",
                "bktxt": "휴일 야간 식대",
                "sgtxt": "야간 업무",
                "user_reason": "업무상 불가피한 상황",
            },
            image_bytes=dummy_image,
            filename="receipt.png",
        )

        case_uuid = result["case_uuid"]
        assert case_uuid, "case_uuid should be non-empty"

        save_dir = svc._EVIDENCE_UPLOAD_ROOT / case_uuid
        assert save_dir.exists(), "UUID 폴더가 생성되어야 합니다"

        # 이미지 파일 확인
        assert result["image_path"], "image_path should be set"
        assert Path(result["image_path"]).exists(), "이미지 파일이 저장되어야 합니다"

        # meta.json 확인
        meta_path = save_dir / "meta.json"
        assert meta_path.exists(), "meta.json이 저장되어야 합니다"

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["case_uuid"] == case_uuid
        assert meta["case_type"] == "HOLIDAY_USAGE"
        assert meta["memo"]["bktxt"] == "휴일 야간 식대"
        assert meta["memo"]["user_reason"] == "업무상 불가피한 상황"
        assert "created_at" in meta

    finally:
        svc._EVIDENCE_UPLOAD_ROOT = original_root


def test_save_custom_demo_case_no_image(tmp_path):
    """이미지 없이 저장 시 meta.json만 생성되고 오류 없음."""
    import services.demo_data_service as svc

    original_root = svc._EVIDENCE_UPLOAD_ROOT
    svc._EVIDENCE_UPLOAD_ROOT = tmp_path / "evidence_uploads"

    try:
        result = svc.save_custom_demo_case(
            payload={
                "case_type": "NORMAL_BASELINE",
                "amount_total": "15000",
                "date_occurrence": "2026-03-17",
                "merchant_name": "일반 식당",
                "bktxt": "정상 업무 식대",
                "sgtxt": "",
                "user_reason": "정상 업무",
            },
            image_bytes=b"",
            filename="",
        )

        case_uuid = result["case_uuid"]
        meta_path = svc._EVIDENCE_UPLOAD_ROOT / case_uuid / "meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["image_path"] == ""

    finally:
        svc._EVIDENCE_UPLOAD_ROOT = original_root


# ──────────────────────────────────────────────────────────────────────────────
# Sprint 2: 이미지-텍스트 교차 검증 (_check_visual_consistency)
# ──────────────────────────────────────────────────────────────────────────────


def _make_entity(label: str, text: str, confidence: float = 0.95) -> dict:
    return {"label": label, "text": text, "confidence": confidence, "bbox": {"ymin": 0, "xmin": 0, "ymax": 100, "xmax": 100}}


def test_visual_consistency_all_match():
    """금액·가맹점·날짜 모두 일치 → score=100, issues 없음."""
    from agent.langgraph_nodes_review import _check_visual_consistency

    entities = [
        _make_entity("amount_total", "97,042"),
        _make_entity("merchant_name", "가온 식당"),
        _make_entity("date_occurrence", "2026-03-14"),
    ]
    body = {"amount": 97042, "merchantName": "가온 식당", "occurredAt": "2026-03-14T19:30:00"}
    score, issues = _check_visual_consistency(body, entities)
    assert score == 100
    assert issues == []


def test_visual_consistency_amount_mismatch_critical():
    """금액 5% 초과 불일치 → score=0, contradictory_evidence HIGH."""
    from agent.langgraph_nodes_review import _check_visual_consistency

    entities = [_make_entity("amount_total", "120,000")]
    body = {"amount": 97042}
    score, issues = _check_visual_consistency(body, entities)
    assert score == 0
    assert len(issues) == 1
    assert issues[0].taxonomy == "contradictory_evidence"
    assert issues[0].severity == "HIGH"
    assert "금액" in issues[0].claim


def test_visual_consistency_amount_within_tolerance():
    """금액 5% 이내 편차 → score=100, issues 없음 (반올림 허용)."""
    from agent.langgraph_nodes_review import _check_visual_consistency

    entities = [_make_entity("amount_total", "97,100")]  # 0.06% 차이
    body = {"amount": 97042}
    score, issues = _check_visual_consistency(body, entities)
    assert score == 100
    assert issues == []


def test_visual_consistency_merchant_mismatch():
    """가맹점명 불일치 → score=50, contradictory_evidence MEDIUM."""
    from agent.langgraph_nodes_review import _check_visual_consistency

    entities = [_make_entity("merchant_name", "완전히다른식당")]
    body = {"amount": 50000, "merchantName": "가온 식당"}
    score, issues = _check_visual_consistency(body, entities)
    assert score == 50
    assert any(i.taxonomy == "contradictory_evidence" and i.severity == "MEDIUM" for i in issues)


def test_visual_consistency_low_confidence_skipped():
    """confidence < 0.6 엔티티는 비교 생략 → score=100, issues 없음."""
    from agent.langgraph_nodes_review import _check_visual_consistency

    entities = [_make_entity("amount_total", "999999", confidence=0.3)]
    body = {"amount": 1000}
    score, issues = _check_visual_consistency(body, entities)
    assert score == 100
    assert issues == []


def test_visual_consistency_empty_entities():
    """엔티티 없음 → score=100, issues 없음 (비교 생략)."""
    from agent.langgraph_nodes_review import _check_visual_consistency

    score, issues = _check_visual_consistency({"amount": 10000}, [])
    assert score == 100
    assert issues == []


def test_visual_consistency_fidelity_update():
    """visual_consistency_score < 현재 fidelity → fidelity 하향 조정."""
    # 금액 불일치 시 visual_consistency_score=0, fidelity=min(기존, 0)=0
    from agent.langgraph_nodes_review import _check_visual_consistency

    entities = [_make_entity("amount_total", "500,000")]
    body = {"amount": 10000}
    score, issues = _check_visual_consistency(body, entities)
    assert score == 0

    # min(기존fidelity=75, visual_score=0) = 0
    current_fidelity = 75
    updated = min(current_fidelity, score)
    assert updated == 0


# ──────────────────────────────────────────────────────────────────────────────
# 6. render_image_with_bboxes: 좌표 변환 정확성 (단위 테스트)
# ──────────────────────────────────────────────────────────────────────────────


def test_clamp_bbox_value_normal():
    from utils.llm_azure import _clamp_bbox_value

    assert _clamp_bbox_value(500, "ymin") == 500
    assert _clamp_bbox_value(0, "xmin") == 0
    assert _clamp_bbox_value(1000, "ymax") == 1000


def test_clamp_bbox_value_out_of_range():
    from utils.llm_azure import _clamp_bbox_value

    assert _clamp_bbox_value(-10, "ymin") == 0
    assert _clamp_bbox_value(1500, "xmax") == 1000


def test_clamp_bbox_value_invalid_type():
    from utils.llm_azure import _clamp_bbox_value

    assert _clamp_bbox_value("abc", "ymin") == 0
    assert _clamp_bbox_value(None, "xmin") == 0


# ──────────────────────────────────────────────────────────────────────────────
# 7. OcrWord 픽셀→정규화 좌표 변환 및 split_key_value
# ──────────────────────────────────────────────────────────────────────────────


def test_ocr_word_norm_coords():
    """픽셀 좌표 → 0~1000 정규화 변환."""
    from utils.ocr_paddle import OcrWord

    w = OcrWord(text="가온 식당", xmin=100, ymin=200, xmax=300, ymax=250,
                img_width=1000, img_height=1000)
    assert w.norm_xmin == 100
    assert w.norm_ymin == 200
    assert w.norm_xmax == 300
    assert w.norm_ymax == 250


def test_ocr_word_norm_coords_scaled():
    """이미지 크기 비율에 맞게 정규화."""
    from utils.ocr_paddle import OcrWord

    w = OcrWord(text="test", xmin=500, ymin=250, xmax=1000, ymax=500,
                img_width=2000, img_height=2000)
    assert w.norm_xmin == 250
    assert w.norm_ymin == 125
    assert w.norm_xmax == 500
    assert w.norm_ymax == 250


def test_ocr_word_split_key_value_combined():
    """레이블+값 한 줄 텍스트를 비율로 분리."""
    from utils.ocr_paddle import OcrWord

    # "거래처명: 가온 식당" — xmin=0, xmax=1000, img=1000×1000
    w = OcrWord(text="거래처명: 가온 식당", xmin=0, ymin=100, xmax=1000, ymax=150,
                img_width=1000, img_height=1000)
    key, val = w.split_key_value("가온 식당")

    # 값 시작점이 키 끝점과 일치
    assert key.xmax == val.xmin
    # 키/값 y범위 동일
    assert key.ymin == val.ymin == 100
    assert key.ymax == val.ymax == 150
    # 전체 너비 보존
    assert key.xmin == 0
    assert val.xmax == 1000


def test_ocr_word_split_not_found_returns_self():
    """값 텍스트를 찾지 못하면 (self, self) 반환."""
    from utils.ocr_paddle import OcrWord

    w = OcrWord(text="hello", xmin=0, ymin=0, xmax=100, ymax=50,
                img_width=1000, img_height=1000)
    key, val = w.split_key_value("없는텍스트")
    assert key is w
    assert val is w


# ──────────────────────────────────────────────────────────────────────────────
# 8. analyze_visual_evidence: PaddleOCR 경로 단위 테스트 (mock)
# ──────────────────────────────────────────="──────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────────


def test_analyze_visual_evidence_uses_paddle_when_available():
    """PaddleOCR 설치 시 source='ocr_llm' 결과 반환."""
    import base64
    import sys
    import types
    from unittest.mock import MagicMock, patch

    from utils.llm_azure import analyze_visual_evidence
    from utils.ocr_paddle import OcrWord

    # 실제 OcrWord 객체 사용 (norm_* 프로퍼티 포함)
    fake_ocr_words = [
        OcrWord(text="거래처명: 가온 식당", xmin=50, ymin=300, xmax=600, ymax=340,
                confidence=0.99, img_width=700, img_height=1100),
        OcrWord(text="거래일자: 2026-03-14", xmin=50, ymin=200, xmax=600, ymax=240,
                confidence=0.98, img_width=700, img_height=1100),
        OcrWord(text="합계금액: 97,042원", xmin=50, ymin=900, xmax=700, ymax=940,
                confidence=0.97, img_width=700, img_height=1100),
    ]

    llm_json_response = """{
      "merchant_name": {"key_index": 0, "value_index": 0, "text": "가온 식당"},
      "date_occurrence": {"key_index": 1, "value_index": 1, "text": "2026-03-14"},
      "amount_total": {"key_index": 2, "value_index": 2, "text": "97042"}
    }"""

    fake_completion = MagicMock()
    fake_completion.choices[0].message.content = llm_json_response
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_completion

    # openai 미설치 환경 대응: fake openai 모듈 주입
    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = MagicMock(return_value=fake_client)  # type: ignore[attr-defined]
    fake_openai.AzureOpenAI = MagicMock(return_value=fake_client)  # type: ignore[attr-defined]

    dummy_b64 = base64.b64encode(b"fake-image").decode()

    with patch("utils.config.settings") as mock_settings, \
         patch("utils.ocr_paddle.run_paddle_ocr", return_value=fake_ocr_words), \
         patch.dict(sys.modules, {"openai": fake_openai}):
        mock_settings.openai_api_key = "fake-key"
        mock_settings.openai_base_url = ""
        mock_settings.openai_api_version = "2024-12-01-preview"
        result = analyze_visual_evidence(dummy_b64)

    assert result.source == "ocr_llm"
    assert result.fallback_used is False
    labels = {e.label for e in result.entities}
    assert "merchant_name" in labels
    assert "date_occurrence" in labels
    assert "amount_total" in labels
    merchant = next(e for e in result.entities if e.label == "merchant_name")
    assert merchant.text == "가온 식당"


# ──────────────────────────────────────────────────────────────────────────────
# time_occurrence: OCR 추출 + 날짜+시간 조합 로직
# ──────────────────────────────────────────────────────────────────────────────


def test_visual_entity_allows_time_occurrence_label():
    """VisualEntity가 time_occurrence 레이블을 허용해야 한다."""
    from agent.output_models import VisualBox, VisualEntity

    box = VisualBox(ymin=100, xmin=50, ymax=130, xmax=400)
    entity = VisualEntity(
        id="item_time",
        label="time_occurrence",
        text="19:45",
        bbox=box,
        confidence=0.95,
    )
    assert entity.label == "time_occurrence"
    assert entity.text == "19:45"


def test_combine_date_time_with_time():
    """날짜+시간 모두 있을 때 ISO 8601 형식으로 조합."""
    from services.demo_data_service import _combine_date_time

    result = _combine_date_time("2026-03-14", "19:45")
    assert result == "2026-03-14T19:45"


def test_combine_date_time_without_time():
    """시간이 없으면 날짜만 반환."""
    from services.demo_data_service import _combine_date_time

    assert _combine_date_time("2026-03-14", "") == "2026-03-14"
    assert _combine_date_time("2026-03-14", "   ") == "2026-03-14"


def test_combine_date_time_without_date():
    """날짜가 없으면 빈 문자열 반환."""
    from services.demo_data_service import _combine_date_time

    assert _combine_date_time("", "19:45") == ""
    assert _combine_date_time("   ", "19:45") == ""


def test_combine_date_time_with_seconds():
    """HH:MM:SS 형식도 HH:MM으로 잘라서 조합."""
    from services.demo_data_service import _combine_date_time

    result = _combine_date_time("2026-03-14", "19:45:00")
    assert result == "2026-03-14T19:45"


def test_combine_date_time_invalid_time():
    """시간 형식이 잘못된 경우 날짜만 반환."""
    from services.demo_data_service import _combine_date_time

    result = _combine_date_time("2026-03-14", "7:45 PM")
    assert result == "2026-03-14"


def test_save_custom_demo_case_stores_time_occurrence(tmp_path):
    """time_occurrence와 datetime_occurrence가 meta.json에 저장되어야 한다."""
    import json
    import services.demo_data_service as svc

    original_root = svc._EVIDENCE_UPLOAD_ROOT
    svc._EVIDENCE_UPLOAD_ROOT = tmp_path / "evidence_uploads"

    try:
        result = svc.save_custom_demo_case(
            payload={
                "case_type": "HOLIDAY_USAGE",
                "amount_total": "97042",
                "date_occurrence": "2026-03-14",
                "time_occurrence": "19:45",
                "merchant_name": "가온 식당",
                "bktxt": "휴일 야간 식대",
                "user_reason": "업무상 불가피한 상황",
            },
            image_bytes=b"",
            filename="",
        )

        meta_path = svc._EVIDENCE_UPLOAD_ROOT / result["case_uuid"] / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

        assert meta["edited_entities"]["time_occurrence"] == "19:45"
        assert meta["edited_entities"]["datetime_occurrence"] == "2026-03-14T19:45"

    finally:
        svc._EVIDENCE_UPLOAD_ROOT = original_root


def test_analyze_visual_evidence_extracts_time_occurrence():
    """OCR+LLM 파이프라인에서 time_occurrence 엔티티가 추출되어야 한다."""
    import base64
    import sys
    import types
    from unittest.mock import MagicMock, patch

    from utils.llm_azure import analyze_visual_evidence
    from utils.ocr_paddle import OcrWord

    fake_ocr_words = [
        OcrWord(text="거래처명: 가온 식당", xmin=50, ymin=300, xmax=600, ymax=340,
                confidence=0.99, img_width=700, img_height=1100),
        OcrWord(text="거래일자: 2026-03-14", xmin=50, ymin=200, xmax=600, ymax=240,
                confidence=0.98, img_width=700, img_height=1100),
        OcrWord(text="거래시간: 19:45 PM", xmin=50, ymin=250, xmax=600, ymax=290,
                confidence=0.97, img_width=700, img_height=1100),
        OcrWord(text="합계금액: 97,042원", xmin=50, ymin=900, xmax=700, ymax=940,
                confidence=0.97, img_width=700, img_height=1100),
    ]

    llm_json_response = """{
      "merchant_name": {"key_index": 0, "value_index": 0, "text": "가온 식당"},
      "date_occurrence": {"key_index": 1, "value_index": 1, "text": "2026-03-14"},
      "time_occurrence": {"key_index": 2, "value_index": 2, "text": "19:45"},
      "amount_total": {"key_index": 3, "value_index": 3, "text": "97042"}
    }"""

    fake_completion = MagicMock()
    fake_completion.choices[0].message.content = llm_json_response
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_completion

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = MagicMock(return_value=fake_client)  # type: ignore[attr-defined]
    fake_openai.AzureOpenAI = MagicMock(return_value=fake_client)  # type: ignore[attr-defined]

    dummy_b64 = base64.b64encode(b"fake-image").decode()

    with patch("utils.config.settings") as mock_settings, \
         patch("utils.ocr_paddle.run_paddle_ocr", return_value=fake_ocr_words), \
         patch.dict(sys.modules, {"openai": fake_openai}):
        mock_settings.openai_api_key = "fake-key"
        mock_settings.openai_base_url = ""
        mock_settings.openai_api_version = "2024-12-01-preview"
        result = analyze_visual_evidence(dummy_b64)

    assert result.source == "ocr_llm"
    labels = {e.label for e in result.entities}
    assert "time_occurrence" in labels
    time_entity = next(e for e in result.entities if e.label == "time_occurrence")
    assert time_entity.text == "19:45"
