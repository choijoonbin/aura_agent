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
