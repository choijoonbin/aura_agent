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
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# PaddleOCR 싱글톤 — 모델 로드는 프로세스 당 1회만 수행
_paddle_instance: "Any | None" = None


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
            # 찾지 못하면 전체를 value, bbox_key는 None 처리용으로 self 반환
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


def get_paddle_ocr(lang: str = "korean") -> "Any":
    """PaddleOCR 싱글톤 인스턴스를 반환한다.

    최초 호출 시 모델 파일을 다운로드한다 (~100 MB, 1회만).
    PaddleOCR 2.x / 3.x 모두 지원.

    Raises:
        ImportError: paddleocr 패키지가 설치되지 않은 경우.
    """
    global _paddle_instance
    if _paddle_instance is None:
        from paddleocr import PaddleOCR  # type: ignore[import]

        major, _ = _paddle_version()
        logger.info("PaddleOCR %s 모델 로드 중 (최초 1회, 약 100 MB 다운로드 가능) ...", major)

        if major >= 3:
            # 3.x: use_angle_cls 파라미터 제거됨, lang 방식도 변경
            # 한국어+영어 복합 인식: lang='korean' 또는 'ch' (중·영 포함) 사용
            _paddle_instance = PaddleOCR(
                lang=lang,
                use_gpu=False,
                show_log=False,
            )
        else:
            # 2.x 기존 방식
            _paddle_instance = PaddleOCR(
                use_angle_cls=True,
                lang=lang,
                use_gpu=False,
                show_log=False,
            )
        logger.info("PaddleOCR 모델 로드 완료.")
    return _paddle_instance


def _parse_line(line: "Any") -> "tuple[list, str, float] | None":
    """PaddleOCR 2.x / 3.x 결과 한 줄을 (box_points, text, confidence)로 파싱한다.

    2.x 형식: [[x1,y1], [x2,y2], [x3,y3], [x4,y4]], ('text', 0.99)
    3.x 형식: {'transcription': 'text', 'points': [...], 'score': 0.99}
              또는 {'rec_text': 'text', 'det_poly': [...], 'rec_score': 0.99}
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
            # bbox가 [xmin, ymin, xmax, ymax] 형식인 경우 4-point로 변환
            if box and not isinstance(box[0], (list, tuple)):
                xmin, ymin, xmax, ymax = float(box[0]), float(box[1]), float(box[2]), float(box[3])
                box = [[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax]]
            return box, str(text), conf

        # ── list/tuple 형식 (2.x 호환) ──────────────────────────────────────
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


def run_paddle_ocr(image_bytes: bytes, lang: str = "korean") -> list[OcrWord]:
    """이미지 bytes에서 PaddleOCR로 전체 텍스트와 픽셀 bbox를 추출한다.

    PaddleOCR 2.x / 3.x 결과 포맷을 모두 처리한다.

    Args:
        image_bytes: JPEG/PNG 등 이미지 raw bytes.
        lang: OCR 언어 코드 (기본 'korean'; 영문 포함 처리됨).

    Returns:
        OcrWord 목록 — ymin(위→아래) 순 정렬.

    Raises:
        ImportError: paddleocr 미설치.
        RuntimeError: OCR 실행 중 예외.
    """
    from PIL import Image  # type: ignore[import]

    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size

    ocr = get_paddle_ocr(lang=lang)

    try:
        results = ocr.ocr(image_bytes, cls=True)
    except TypeError:
        # 3.x 일부 버전: cls 파라미터 미지원
        try:
            results = ocr.ocr(image_bytes)
        except Exception as exc:
            raise RuntimeError(f"PaddleOCR 실행 실패: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"PaddleOCR 실행 실패: {exc}") from exc

    words: list[OcrWord] = []
    for page in results or []:
        if page is None:
            continue
        for line in page:
            parsed = _parse_line(line)
            if parsed is None:
                continue
            box_points, text, conf = parsed
            stripped = text.strip()
            if not stripped:
                continue
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

    # 위→아래 순 정렬
    words.sort(key=lambda ww: ww.ymin)
    return words
