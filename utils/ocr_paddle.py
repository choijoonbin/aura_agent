"""PaddleOCR 기반 텍스트 + 픽셀 bbox 추출.

Vision LLM 좌표 추정 대신 전용 OCR 엔진으로 픽셀 단위 정확한 bbox를 확보한다.
analyze_visual_evidence의 Stage-1 (좌표 담당) 역할.

설치:
    pip install paddlepaddle paddleocr
    # GPU 환경: pip install paddlepaddle-gpu paddleocr
"""
from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from typing import Any

# paddleocr/paddlex import 전에 반드시 설정해야 함.
# 이 시점 이후에 설정하면 "Checking connectivity to the model hosters"에서
# 무한 블로킹이 발생한다 (import 단계에서 체크가 실행됨).
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

logger = logging.getLogger(__name__)

# PaddleOCR 싱글톤 — 모델 로드는 프로세스 당 1회만 수행
_paddle_instance: Any | None = None


@dataclass
class OcrWord:
    """OCR로 추출된 텍스트 한 줄 단위 (픽셀 bbox 포함)."""

    text: str
    xmin: int           # 픽셀 좌표 (원본 이미지 기준)
    ymin: int
    xmax: int
    ymax: int
    confidence: float = 1.0
    img_width: int = 0
    img_height: int = 0

    # ── 정규화 좌표 프로퍼티 (0~1000, VisualBox 호환) ──────────────────────

    @property
    def norm_xmin(self) -> int:
        return _px_to_norm(self.xmin, self.img_width)

    @property
    def norm_ymin(self) -> int:
        return _px_to_norm(self.ymin, self.img_height)

    @property
    def norm_xmax(self) -> int:
        return _px_to_norm(self.xmax, self.img_width)

    @property
    def norm_ymax(self) -> int:
        return _px_to_norm(self.ymax, self.img_height)

    def split_key_value(self, value_text: str) -> "tuple[OcrWord, OcrWord]":
        """레이블+값이 한 줄로 합쳐진 경우 문자 비율로 xmin/xmax를 분리한다.

        예: "거래처명: 가온 식당" → (key_word="거래처명:", value_word="가온 식당")
        정확한 폰트 측정이 아닌 비율 추정이므로 ±1~2% 오차 허용.

        Returns:
            (key_word, value_word) — 둘 다 동일한 ymin/ymax 공유.
        """
        full = self.text
        val_idx = full.find(value_text)
        if val_idx < 0:
            return self, self

        total_len = max(len(full), 1)
        split_ratio = val_idx / total_len
        split_x = self.xmin + int((self.xmax - self.xmin) * split_ratio)

        key_word = OcrWord(
            text=full[:val_idx].strip(),
            xmin=self.xmin,
            ymin=self.ymin,
            xmax=split_x,
            ymax=self.ymax,
            confidence=self.confidence,
            img_width=self.img_width,
            img_height=self.img_height,
        )
        val_word = OcrWord(
            text=value_text,
            xmin=split_x,
            ymin=self.ymin,
            xmax=self.xmax,
            ymax=self.ymax,
            confidence=self.confidence,
            img_width=self.img_width,
            img_height=self.img_height,
        )
        return key_word, val_word


def _px_to_norm(px: int, dim: int) -> int:
    """픽셀 좌표를 0~1000 정규화 정수로 변환."""
    if dim <= 0:
        return 0
    return max(0, min(1000, int(px / dim * 1000)))


def _paddle_version() -> tuple[int, int]:
    """paddleocr 패키지의 (major, minor) 버전을 반환한다. 파싱 실패 시 (2, 0)."""
    try:
        import paddleocr  # type: ignore[import]
        ver = getattr(paddleocr, "__version__", "2.0.0")
        parts = str(ver).split(".")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return 2, 0


def get_paddle_ocr(lang: str = "korean") -> Any:
    """PaddleOCR 싱글톤 인스턴스를 반환한다.

    최초 호출 시 모델 파일을 다운로드한다 (~100 MB, 1회만).
    PaddleOCR 2.x / 3.x 모두 지원.

    Raises:
        ImportError: paddleocr 패키지가 설치되지 않은 경우.
        RuntimeError: 초기화 실패.
    """
    global _paddle_instance
    if _paddle_instance is not None:
        return _paddle_instance

    from paddleocr import PaddleOCR  # type: ignore[import]

    major, minor = _paddle_version()
    logger.info("PaddleOCR v%d.%d 모델 로드 중 (최초 1회) ...", major, minor)

    last_exc: Exception | None = None

    if major >= 3:
        # ── 3.x: use_angle_cls / use_gpu / show_log 파라미터 제거됨 ──────────
        # device='cpu' 사용; 일부 빌드에서 지원 안 할 수 있으므로 순차 시도
        for kwargs in [
            {"lang": lang, "device": "cpu"},
            {"lang": lang},
        ]:
            try:
                _paddle_instance = PaddleOCR(**kwargs)
                logger.info("PaddleOCR 3.x 초기화 성공: %s", kwargs)
                return _paddle_instance
            except TypeError as e:
                last_exc = e
                logger.debug("PaddleOCR 3.x 초기화 시도 실패 (%s): %s", kwargs, e)
    else:
        # ── 2.x 기존 방식 ─────────────────────────────────────────────────────
        for kwargs in [
            {"use_angle_cls": True, "lang": lang, "use_gpu": False, "show_log": False},
            {"use_angle_cls": True, "lang": lang, "use_gpu": False},
            {"lang": lang},
        ]:
            try:
                _paddle_instance = PaddleOCR(**kwargs)
                logger.info("PaddleOCR 2.x 초기화 성공: %s", kwargs)
                return _paddle_instance
            except TypeError as e:
                last_exc = e
                logger.debug("PaddleOCR 2.x 초기화 시도 실패 (%s): %s", kwargs, e)

    raise RuntimeError(f"PaddleOCR 초기화 모든 시도 실패: {last_exc}")


def _parse_line(line: Any) -> "tuple[list, str, float] | None":
    """PaddleOCR 2.x / 3.x 결과 한 줄을 (box_points, text, confidence)로 파싱한다.

    2.x 형식: [[[x1,y1],[x2,y2],[x3,y3],[x4,y4]], ('text', 0.99)]
    3.x dict: {'transcription': text, 'points': [...], 'score': conf}
              {'rec_text': text, 'det_poly': [...], 'rec_score': conf}
    """
    try:
        # ── dict 형식 (3.x) ─────────────────────────────────────────────────
        if isinstance(line, dict):
            text = (
                line.get("transcription")
                or line.get("rec_text")
                or line.get("text")
                or ""
            )
            conf = float(
                line.get("score")
                or line.get("rec_score")
                or line.get("confidence")
                or 1.0
            )
            box = (
                line.get("points")
                or line.get("det_poly")
                or line.get("bbox")
                or []
            )
            if box and not isinstance(box[0], (list, tuple)):
                # [xmin, ymin, xmax, ymax] → 4-point 변환
                xmin, ymin, xmax, ymax = (
                    float(box[0]), float(box[1]), float(box[2]), float(box[3])
                )
                box = [[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax]]
            return box, str(text), conf

        # ── list/tuple 형식 (2.x 및 3.x 호환) ──────────────────────────────
        if isinstance(line, (list, tuple)) and len(line) == 2:
            box_points, text_info = line
            if isinstance(text_info, (list, tuple)) and len(text_info) == 2:
                text, conf = text_info
            else:
                text, conf = str(text_info), 1.0
            return box_points, str(text), float(conf)

    except Exception as exc:
        logger.debug("OCR 라인 파싱 실패: %s | 입력: %r", exc, line)
    return None


def _parse_result_obj(result_obj: Any, w: int, h: int) -> list[OcrWord]:
    """paddleocr 3.x OCRResult 객체(속성 기반) 파싱.

    rec_texts / rec_scores / rec_polys 속성을 가진 객체를 처리한다.
    """
    words: list[OcrWord] = []
    rec_texts = getattr(result_obj, "rec_texts", None)
    rec_polys = getattr(result_obj, "rec_polys", None)
    rec_scores = getattr(result_obj, "rec_scores", None)

    if rec_texts is None or rec_polys is None:
        return words

    scores_iter = rec_scores if rec_scores is not None else [1.0] * len(rec_texts)
    for text, score, poly in zip(rec_texts, scores_iter, rec_polys):
        stripped = str(text).strip()
        if not stripped:
            continue
        xs = [float(p[0]) for p in poly]
        ys = [float(p[1]) for p in poly]
        words.append(
            OcrWord(
                text=stripped,
                xmin=max(0, int(min(xs))),
                ymin=max(0, int(min(ys))),
                xmax=min(w, int(max(xs))),
                ymax=min(h, int(max(ys))),
                confidence=float(score),
                img_width=w,
                img_height=h,
            )
        )
    return words


def run_paddle_ocr(image_bytes: bytes, lang: str = "korean") -> list[OcrWord]:
    """이미지 bytes에서 PaddleOCR로 전체 텍스트와 픽셀 bbox를 추출한다.

    PaddleOCR 2.x / 3.x 결과 포맷(list, dict, 속성 객체)을 모두 처리한다.
    이미지는 bytes → numpy array 변환 후 전달 (버전 간 최대 호환).

    Args:
        image_bytes: JPEG/PNG 등 이미지 raw bytes.
        lang: OCR 언어 코드 (기본 'korean').

    Returns:
        OcrWord 목록 — ymin(위→아래) 순 정렬.

    Raises:
        ImportError: paddleocr 미설치.
        RuntimeError: OCR 초기화 또는 실행 중 예외.
    """
    import numpy as np  # type: ignore[import]
    from PIL import Image  # type: ignore[import]

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    img_array = np.array(img)

    ocr = get_paddle_ocr(lang=lang)  # RuntimeError 발생 시 그대로 전파

    # ── OCR 실행 (cls 파라미터 지원 여부 자동 감지) ──────────────────────────
    results: Any = None
    for call_kwargs in [{"cls": True}, {}]:
        try:
            results = ocr.ocr(img_array, **call_kwargs)
            break
        except TypeError:
            continue
        except Exception as exc:
            raise RuntimeError(f"PaddleOCR ocr() 실행 실패: {exc}") from exc

    if results is None:
        raise RuntimeError("PaddleOCR ocr() 호출 방법을 찾지 못했습니다.")

    logger.debug("PaddleOCR raw results type: %s, len: %s", type(results), len(results) if results else 0)

    words: list[OcrWord] = []

    for page in results or []:
        if page is None:
            continue

        # ── Case A: 속성 기반 객체 (paddleocr 3.x 일부 빌드) ─────────────────
        if hasattr(page, "rec_texts"):
            words.extend(_parse_result_obj(page, w, h))
            continue

        # ── Case B: list/dict 형식 ────────────────────────────────────────────
        if not isinstance(page, (list, tuple)):
            # 단일 dict인 경우 (드문 케이스)
            parsed = _parse_line(page)
            if parsed:
                _append_word(words, parsed, w, h)
            continue

        for line in page:
            if line is None:
                continue
            # 속성 기반 객체가 list 내부에 있는 경우
            if hasattr(line, "rec_texts"):
                words.extend(_parse_result_obj(line, w, h))
                continue
            parsed = _parse_line(line)
            if parsed is None:
                continue
            _append_word(words, parsed, w, h)

    words.sort(key=lambda ww: ww.ymin)
    logger.info("PaddleOCR 완료: %d개 텍스트 블록 추출", len(words))
    return words


def _append_word(
    words: list[OcrWord],
    parsed: "tuple[list, str, float]",
    w: int,
    h: int,
) -> None:
    box_points, text, conf = parsed
    stripped = text.strip()
    if not stripped:
        return
    xs = [float(p[0]) for p in box_points]
    ys = [float(p[1]) for p in box_points]
    words.append(
        OcrWord(
            text=stripped,
            xmin=max(0, int(min(xs))),
            ymin=max(0, int(min(ys))),
            xmax=min(w, int(max(xs))),
            ymax=min(h, int(max(ys))),
            confidence=conf,
            img_width=w,
            img_height=h,
        )
    )
