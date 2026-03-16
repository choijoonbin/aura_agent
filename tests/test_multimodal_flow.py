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
        dummy_image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50  # 비정상 케이스: 이미지 필수
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
            image_bytes=dummy_image,
            filename="receipt.png",
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


# ──────────────────────────────────────────────────────────────────────────────
# 결제일시 3분할 fix + amount nearby fix
# ──────────────────────────────────────────────────────────────────────────────


def test_apply_combined_datetime_fix_splits_bboxes():
    """결제일시 같은 줄 → 라벨/날짜/시간 3분할 bbox."""
    import sys
    import types
    from unittest.mock import MagicMock, patch

    from utils.llm_azure import analyze_visual_evidence
    from utils.ocr_paddle import OcrWord

    # "결제일시 : 2026-03-14 23:42 (토요일)" — xmin=0, xmax=700 (img_width=700)
    fake_ocr_words = [
        OcrWord(text="가맹점명 : 가온식당 강남점",
                xmin=50, ymin=100, xmax=600, ymax=140,
                confidence=0.99, img_width=700, img_height=1100),
        OcrWord(text="결제일시 : 2026-03-14 23:42 (토요일)",
                xmin=0, ymin=200, xmax=700, ymax=240,
                confidence=0.98, img_width=700, img_height=1100),
        OcrWord(text="합계금액",
                xmin=50, ymin=800, xmax=300, ymax=840,
                confidence=0.97, img_width=700, img_height=1100),
        OcrWord(text="68,000원",
                xmin=400, ymin=800, xmax=680, ymax=840,
                confidence=0.97, img_width=700, img_height=1100),
    ]

    llm_json_response = """{
      "merchant_name": {"key_index": 0, "value_index": 0, "text": "가온식당 강남점"},
      "date_occurrence": {"key_index": 1, "value_index": 1, "text": "2026-03-14"},
      "time_occurrence": {"key_index": 1, "value_index": 1, "text": "23:42"},
      "amount_total": {"key_index": 2, "value_index": 3, "text": "68000"}
    }"""

    fake_completion = MagicMock()
    fake_completion.choices[0].message.content = llm_json_response
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_completion

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = MagicMock(return_value=fake_client)  # type: ignore
    fake_openai.AzureOpenAI = MagicMock(return_value=fake_client)  # type: ignore

    import base64
    dummy_b64 = base64.b64encode(b"fake-image").decode()

    with patch("utils.config.settings") as mock_settings, \
         patch("utils.ocr_paddle.run_paddle_ocr", return_value=fake_ocr_words), \
         patch.dict(sys.modules, {"openai": fake_openai}):
        mock_settings.openai_api_key = "fake-key"
        mock_settings.openai_base_url = ""
        mock_settings.openai_api_version = "2024-12-01-preview"
        result = analyze_visual_evidence(dummy_b64)

    date_e = next(e for e in result.entities if e.label == "date_occurrence")
    time_e = next(e for e in result.entities if e.label == "time_occurrence")

    # 1) 두 엔티티의 bbox_key(라벨 구간)가 동일해야 함
    assert date_e.bbox_key is not None
    assert time_e.bbox_key is not None
    assert date_e.bbox_key.xmin == time_e.bbox_key.xmin
    assert date_e.bbox_key.xmax == time_e.bbox_key.xmax, "라벨 bbox가 동일해야 함"

    # 2) 날짜 bbox는 시간 bbox보다 왼쪽이어야 함
    assert date_e.bbox.xmin < time_e.bbox.xmin, "날짜가 시간보다 왼쪽이어야 함"
    assert date_e.bbox.xmax <= time_e.bbox.xmin + 10, "날짜 bbox가 시간 bbox와 겹치지 않아야 함"

    # 3) 라벨 bbox는 날짜 bbox보다 왼쪽이어야 함
    assert date_e.bbox_key.xmax <= date_e.bbox.xmin + 10, "라벨이 날짜보다 왼쪽이어야 함"


def test_fix_amount_nearby_corrects_distant_value():
    """합계금액 value_index가 key_index와 멀면 가까운 금액 블록으로 교체."""
    import sys
    import types
    from unittest.mock import MagicMock, patch

    from utils.llm_azure import analyze_visual_evidence
    from utils.ocr_paddle import OcrWord

    # 합계금액(idx=2) 옆 68000원(idx=3) — LLM이 footer의 idx=8을 잘못 선택
    fake_ocr_words = [
        OcrWord(text="가맹점명 : 가온식당", xmin=50, ymin=100, xmax=600, ymax=140,
                confidence=0.99, img_width=700, img_height=1100),
        OcrWord(text="결제일시 : 2026-03-14 23:42",
                xmin=0, ymin=200, xmax=700, ymax=240,
                confidence=0.98, img_width=700, img_height=1100),
        OcrWord(text="합계금액", xmin=50, ymin=800, xmax=300, ymax=850,
                confidence=0.97, img_width=700, img_height=1100),
        OcrWord(text="68,000원", xmin=400, ymin=800, xmax=680, ymax=850,
                confidence=0.97, img_width=700, img_height=1100),
        OcrWord(text="공급가액", xmin=50, ymin=860, xmax=300, ymax=900,
                confidence=0.96, img_width=700, img_height=1100),
        OcrWord(text="61,818원", xmin=400, ymin=860, xmax=680, ymax=900,
                confidence=0.96, img_width=700, img_height=1100),
        OcrWord(text="부가가치세", xmin=50, ymin=910, xmax=300, ymax=950,
                confidence=0.95, img_width=700, img_height=1100),
        OcrWord(text="6,182원", xmin=400, ymin=910, xmax=680, ymax=950,
                confidence=0.95, img_width=700, img_height=1100),
        OcrWord(text="합계:", xmin=50, ymin=1000, xmax=200, ymax=1040,
                confidence=0.94, img_width=700, img_height=1100),
        OcrWord(text="68,000원", xmin=400, ymin=1000, xmax=680, ymax=1040,
                confidence=0.94, img_width=700, img_height=1100),
    ]

    # LLM이 잘못 footer(idx=9) 선택
    llm_json_response = """{
      "merchant_name": {"key_index": 0, "value_index": 0, "text": "가온식당"},
      "date_occurrence": {"key_index": 1, "value_index": 1, "text": "2026-03-14"},
      "time_occurrence": {"key_index": 1, "value_index": 1, "text": "23:42"},
      "amount_total": {"key_index": 2, "value_index": 9, "text": "68000"}
    }"""

    fake_completion = MagicMock()
    fake_completion.choices[0].message.content = llm_json_response
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_completion

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = MagicMock(return_value=fake_client)  # type: ignore
    fake_openai.AzureOpenAI = MagicMock(return_value=fake_client)  # type: ignore

    import base64
    dummy_b64 = base64.b64encode(b"fake-image").decode()

    with patch("utils.config.settings") as mock_settings, \
         patch("utils.ocr_paddle.run_paddle_ocr", return_value=fake_ocr_words), \
         patch.dict(sys.modules, {"openai": fake_openai}):
        mock_settings.openai_api_key = "fake-key"
        mock_settings.openai_base_url = ""
        mock_settings.openai_api_version = "2024-12-01-preview"
        result = analyze_visual_evidence(dummy_b64)

    amount_e = next(e for e in result.entities if e.label == "amount_total")
    # idx=3 의 "68,000원" → "68000" 으로 교체되어야 함
    assert amount_e.text == "68000"
    # 올바른 y좌표(idx=3, ymin=800)에 있어야 함 — footer(ymin=1000)가 아님
    assert amount_e.bbox.ymin < 850  # 0~1000 정규화 기준 800/1100*1000 ≈ 727


def test_apply_combined_datetime_fix_separate_key_value_blocks():
    """OCR이 '결제일시:' / '2026-03-14 23:42' 두 블록으로 분리한 경우 처리."""
    import sys
    import types
    from unittest.mock import MagicMock, patch

    from utils.llm_azure import analyze_visual_evidence
    from utils.ocr_paddle import OcrWord

    # PaddleOCR가 라벨/값을 별도 블록으로 분리한 케이스
    fake_ocr_words = [
        OcrWord(text="가맹점명 : 가온식당 강남점",
                xmin=50, ymin=100, xmax=600, ymax=140,
                confidence=0.99, img_width=700, img_height=1100),
        OcrWord(text="결제일시 :",          # key block (idx=1)
                xmin=0, ymin=200, xmax=180, ymax=240,
                confidence=0.98, img_width=700, img_height=1100),
        OcrWord(text="2026-03-14 23:42 (토요일)",  # val block (idx=2)
                xmin=190, ymin=200, xmax=700, ymax=240,
                confidence=0.98, img_width=700, img_height=1100),
        OcrWord(text="합계금액",
                xmin=50, ymin=800, xmax=300, ymax=850,
                confidence=0.97, img_width=700, img_height=1100),
        OcrWord(text="68,000원",
                xmin=400, ymin=800, xmax=680, ymax=850,
                confidence=0.97, img_width=700, img_height=1100),
    ]

    # LLM이 date_occurrence만 반환, time_occurrence는 null
    llm_json_response = """{
      "merchant_name": {"key_index": 0, "value_index": 0, "text": "가온식당 강남점"},
      "date_occurrence": {"key_index": 1, "value_index": 2, "text": "2026-03-14"},
      "time_occurrence": null,
      "amount_total": {"key_index": 3, "value_index": 4, "text": "68000"}
    }"""

    fake_completion = MagicMock()
    fake_completion.choices[0].message.content = llm_json_response
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_completion

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = MagicMock(return_value=fake_client)  # type: ignore
    fake_openai.AzureOpenAI = MagicMock(return_value=fake_client)  # type: ignore

    import base64
    dummy_b64 = base64.b64encode(b"fake-image").decode()

    with patch("utils.config.settings") as mock_settings, \
         patch("utils.ocr_paddle.run_paddle_ocr", return_value=fake_ocr_words), \
         patch.dict(sys.modules, {"openai": fake_openai}):
        mock_settings.openai_api_key = "fake-key"
        mock_settings.openai_base_url = ""
        mock_settings.openai_api_version = "2024-12-01-preview"
        result = analyze_visual_evidence(dummy_b64)

    labels = {e.label for e in result.entities}
    assert "date_occurrence" in labels
    # auto-detect로 time_occurrence도 생성되어야 함
    assert "time_occurrence" in labels, "time_occurrence가 자동 감지되어야 함"

    date_e = next(e for e in result.entities if e.label == "date_occurrence")
    time_e = next(e for e in result.entities if e.label == "time_occurrence")

    assert time_e.text == "23:42"

    # 날짜와 시간의 bbox_key는 동일한 "결제일시 :" 블록이어야 함 (Case B)
    assert date_e.bbox_key is not None
    assert time_e.bbox_key is not None
    assert date_e.bbox_key.xmin == time_e.bbox_key.xmin
    assert date_e.bbox_key.xmax == time_e.bbox_key.xmax

    # 날짜 bbox는 시간 bbox보다 왼쪽이어야 함
    assert date_e.bbox.xmin < time_e.bbox.xmin


def test_fix_amount_same_y_line_trusted():
    """합계금액 라벨과 같은 Y 줄의 값은 LLM 선택을 그대로 신뢰한다."""
    import sys
    import types
    from unittest.mock import MagicMock, patch

    from utils.llm_azure import analyze_visual_evidence
    from utils.ocr_paddle import OcrWord

    fake_ocr_words = [
        OcrWord(text="합계금액", xmin=50, ymin=800, xmax=300, ymax=850,
                confidence=0.97, img_width=700, img_height=1100),
        OcrWord(text="68,000원", xmin=400, ymin=805, xmax=680, ymax=845,
                confidence=0.97, img_width=700, img_height=1100),
        OcrWord(text="61,818원", xmin=400, ymin=860, xmax=680, ymax=900,
                confidence=0.96, img_width=700, img_height=1100),
    ]

    # LLM이 같은 줄의 올바른 값 선택
    llm_json_response = """{
      "merchant_name": null,
      "date_occurrence": null,
      "time_occurrence": null,
      "amount_total": {"key_index": 0, "value_index": 1, "text": "68000"}
    }"""

    fake_completion = MagicMock()
    fake_completion.choices[0].message.content = llm_json_response
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_completion

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = MagicMock(return_value=fake_client)  # type: ignore
    fake_openai.AzureOpenAI = MagicMock(return_value=fake_client)  # type: ignore

    import base64
    dummy_b64 = base64.b64encode(b"fake-image").decode()

    with patch("utils.config.settings") as mock_settings, \
         patch("utils.ocr_paddle.run_paddle_ocr", return_value=fake_ocr_words), \
         patch.dict(sys.modules, {"openai": fake_openai}):
        mock_settings.openai_api_key = "fake-key"
        mock_settings.openai_base_url = ""
        mock_settings.openai_api_version = "2024-12-01-preview"
        result = analyze_visual_evidence(dummy_b64)

    amount_e = next((e for e in result.entities if e.label == "amount_total"), None)
    assert amount_e is not None
    # 같은 줄 올바른 값 유지
    assert amount_e.text == "68000"
    assert amount_e.bbox.ymin < 800  # ymin=805/1100*1000 ≈ 732


# ──────────────────────────────────────────────────────────────────────────────
# Beta 경로 회귀 테스트 (DB 전표 생성 + 스크리닝 + 서버 검증)
# ──────────────────────────────────────────────────────────────────────────────


def test_create_beta_voucher_calls_db_and_screening():
    """_create_beta_voucher(): FiDocHeader/FiDocItem add 및 run_case_screening 호출 확인."""
    from unittest.mock import MagicMock, patch

    import services.demo_data_service as svc

    mock_db = MagicMock()
    mock_db.scalar.return_value = 0  # 기존 전표 없음

    screening_result = {
        "case_type": "HOLIDAY_USAGE",
        "severity": "HIGH",
        "score": 85,
        "reason_text": "휴일 사용 의심",
        "voucher_key": "1000-BH00000001-2026",
        "screening_meta": None,
    }

    with patch("services.case_service.run_case_screening", return_value=screening_result):
        voucher_key = svc._create_beta_voucher(
            db=mock_db,
            payload={
                "case_type": "HOLIDAY_USAGE",
                "amount_total": "97042",
                "date_occurrence": "2026-03-14",
                "time_occurrence": "19:45",
                "merchant_name": "가온 식당",
                "bktxt": "휴일 야간 식대",
            },
            case_type="HOLIDAY_USAGE",
            case_uuid="test-uuid-holiday",
        )

    # voucher_key 형식 확인
    assert voucher_key.startswith("1000-BH"), f"unexpected voucher_key: {voucher_key}"
    assert "2026" in voucher_key

    # FiDocHeader + FiDocItem 2회 add
    assert mock_db.add.call_count == 2
    # flush → commit 순서
    mock_db.flush.assert_called_once()
    mock_db.commit.assert_called_once()

    # FiDocHeader 파라미터 확인
    header_call_args = mock_db.add.call_args_list[0][0][0]
    assert header_call_args.hr_status == "LEAVE"   # HOLIDAY_USAGE 프로파일
    assert header_call_args.mcc_code == "5813"
    assert header_call_args.budget_exceeded_flag == "N"
    assert header_call_args.blart == "SA"
    assert header_call_args.doc_source == "BETA"

    # FiDocItem 파라미터 확인
    item_call_args = mock_db.add.call_args_list[1][0][0]
    assert item_call_args.wrbtr == 97042.0


def test_create_beta_voucher_normal_baseline_uses_profile_defaults():
    """NORMAL_BASELINE: SCENARIO_PROFILES 기본값(WORK/5816/N) 사용 확인."""
    from unittest.mock import MagicMock, patch

    import services.demo_data_service as svc

    mock_db = MagicMock()
    mock_db.scalar.return_value = 0

    with patch("services.case_service.run_case_screening", return_value={
        "case_type": "NORMAL", "severity": "LOW", "score": 10,
        "reason_text": "정상", "voucher_key": "1000-BN00000001-2026", "screening_meta": None,
    }):
        voucher_key = svc._create_beta_voucher(
            db=mock_db,
            payload={
                "case_type": "NORMAL_BASELINE",
                "amount_total": "15000",
                "date_occurrence": "2026-03-17",
                "time_occurrence": "12:00",
                "merchant_name": "일반 식당",
                "bktxt": "점심 식대",
            },
            case_type="NORMAL_BASELINE",
            case_uuid="test-uuid-normal",
        )

    assert voucher_key.startswith("1000-BN")
    header = mock_db.add.call_args_list[0][0][0]
    assert header.hr_status == "WORK"
    assert header.mcc_code == "5816"
    assert header.budget_exceeded_flag == "N"


def test_create_beta_voucher_amount_fallback_on_invalid():
    """amount_total 파싱 불가 시 시나리오 기본값 사용."""
    from unittest.mock import MagicMock, patch

    import services.demo_data_service as svc
    from services.demo_data_service import SCENARIO_PROFILES

    mock_db = MagicMock()
    mock_db.scalar.return_value = 0

    with patch("services.case_service.run_case_screening", return_value={
        "case_type": "LIMIT_EXCEED", "severity": "HIGH", "score": 90,
        "reason_text": "한도 초과", "voucher_key": "1000-BL00000001-2026", "screening_meta": None,
    }):
        svc._create_beta_voucher(
            db=mock_db,
            payload={
                "case_type": "LIMIT_EXCEED",
                "amount_total": "abc",          # 파싱 불가
                "date_occurrence": "2026-03-17",
                "merchant_name": "고액 식당",
                "bktxt": "접대비",
            },
            case_type="LIMIT_EXCEED",
            case_uuid="test-uuid-limit",
        )

    item = mock_db.add.call_args_list[1][0][0]
    lo, hi = SCENARIO_PROFILES["LIMIT_EXCEED"]["amount_range"]
    assert lo <= item.wrbtr <= hi, f"fallback amount should be in range {lo}~{hi}, got {item.wrbtr}"


def test_validate_beta_payload_passes_valid():
    """모든 필수값이 있으면 검증 통과."""
    from services.demo_data_service import _validate_beta_payload

    _validate_beta_payload(
        payload={
            "case_type": "HOLIDAY_USAGE",
            "amount_total": "97042",
            "date_occurrence": "2026-03-14",
            "merchant_name": "가온 식당",
            "bktxt": "휴일 야간 식대",
            "user_reason": "업무상 불가피",
        },
        image_bytes=b"\x89PNG",
    )  # 예외 없으면 통과


def test_validate_beta_payload_raises_on_missing_amount():
    """amount_total 누락 → ValueError."""
    from services.demo_data_service import _validate_beta_payload

    with pytest.raises(ValueError, match="amount_total"):
        _validate_beta_payload(
            payload={
                "case_type": "HOLIDAY_USAGE",
                "amount_total": "",
                "date_occurrence": "2026-03-14",
                "merchant_name": "가온 식당",
                "bktxt": "휴일 야간 식대",
                "user_reason": "업무 목적",
            },
            image_bytes=b"\x89PNG",
        )


def test_validate_beta_payload_raises_abnormal_no_image():
    """비정상 케이스에서 이미지 없으면 → ValueError."""
    from services.demo_data_service import _validate_beta_payload

    with pytest.raises(ValueError, match="이미지"):
        _validate_beta_payload(
            payload={
                "case_type": "HOLIDAY_USAGE",
                "amount_total": "50000",
                "date_occurrence": "2026-03-14",
                "merchant_name": "가온 식당",
                "bktxt": "휴일 야간 식대",
                "user_reason": "업무 목적",
            },
            image_bytes=b"",  # 이미지 없음
        )


def test_validate_beta_payload_normal_baseline_allows_no_image():
    """NORMAL_BASELINE은 이미지 없어도 저장 가능."""
    from services.demo_data_service import _validate_beta_payload

    _validate_beta_payload(
        payload={
            "case_type": "NORMAL_BASELINE",
            "amount_total": "15000",
            "date_occurrence": "2026-03-17",
            "merchant_name": "일반 식당",
            "bktxt": "점심 식대",
            "user_reason": "정상 업무",
        },
        image_bytes=b"",  # 이미지 없어도 OK
    )


def test_save_custom_demo_case_rejects_invalid_payload(tmp_path):
    """서버단 검증: 비정상 케이스에서 이미지 없으면 저장 실패."""
    import services.demo_data_service as svc

    original_root = svc._EVIDENCE_UPLOAD_ROOT
    svc._EVIDENCE_UPLOAD_ROOT = tmp_path / "evidence_uploads"

    try:
        with pytest.raises(ValueError, match="이미지"):
            svc.save_custom_demo_case(
                payload={
                    "case_type": "LIMIT_EXCEED",
                    "amount_total": "500000",
                    "date_occurrence": "2026-03-17",
                    "merchant_name": "고액 식당",
                    "bktxt": "접대비",
                    "user_reason": "업무 목적",
                },
                image_bytes=b"",
                filename="",
            )
    finally:
        svc._EVIDENCE_UPLOAD_ROOT = original_root


def test_save_custom_demo_case_meta_contains_answer_type(tmp_path):
    """meta.json에 answer_type='combined'이 기록되어야 한다."""
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
                "bktxt": "점심 식대",
                "user_reason": "정상 업무",
            },
            image_bytes=b"",
            filename="",
        )
        meta = json.loads((tmp_path / "evidence_uploads" / result["case_uuid"] / "meta.json").read_text())
        assert meta.get("answer_type") == "combined"
    finally:
        svc._EVIDENCE_UPLOAD_ROOT = original_root


def test_merchant_name_for_header_beta_prefix():
    """_merchant_name_for_header(): BETA- xblnr → SCENARIO_PROFILES 가맹점명 반환."""
    from unittest.mock import MagicMock

    from services.case_service import _merchant_name_for_header

    header = MagicMock()
    header.xblnr = "BETA-HOLIDAY_-BH00000001"
    header.bktxt = "휴일 야간 식대를 위한 시연 데이터"

    name = _merchant_name_for_header(header)
    assert name == "가온 식당", f"expected '가온 식당', got {name!r}"


def test_merchant_name_for_header_beta_limit_exceed():
    """BETA-LIMIT_EX- xblnr → '고액 식대' 반환."""
    from unittest.mock import MagicMock

    from services.case_service import _merchant_name_for_header

    header = MagicMock()
    header.xblnr = "BETA-LIMIT_EX-BL00000001"
    header.bktxt = "고액 접대비를 위한 시연 데이터"

    name = _merchant_name_for_header(header)
    assert name == "고액 식대", f"expected '고액 식대', got {name!r}"
