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


def get_paddle_ocr(lang: str = "korean") -> "Any":
    """PaddleOCR 싱글톤 인스턴스를 반환한다.

    최초 호출 시 모델 파일을 다운로드한다 (~100 MB, 1회만).

    Raises:
        ImportError: paddleocr 패키지가 설치되지 않은 경우.
    """
    global _paddle_instance
    if _paddle_instance is None:
        from paddleocr import PaddleOCR  # type: ignore[import]

        logger.info("PaddleOCR 모델 로드 중 (최초 1회, 약 100 MB 다운로드 가능) ...")
        _paddle_instance = PaddleOCR(
            use_angle_cls=True,
            lang=lang,
            use_gpu=False,   # CPU 기본; GPU 환경에서는 True로 변경 가능
            show_log=False,
        )
        logger.info("PaddleOCR 모델 로드 완료.")
    return _paddle_instance


def run_paddle_ocr(image_bytes: bytes, lang: str = "korean") -> list[OcrWord]:
    """이미지 bytes에서 PaddleOCR로 전체 텍스트와 픽셀 bbox를 추출한다.

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
    except Exception as exc:
        raise RuntimeError(f"PaddleOCR 실행 실패: {exc}") from exc

    words: list[OcrWord] = []
    for page in results or []:
        for line in page or []:
            box_points, (text, conf) = line
            xs = [float(p[0]) for p in box_points]
            ys = [float(p[1]) for p in box_points]
            stripped = (text or "").strip()
            if not stripped:
                continue
            words.append(
                OcrWord(
                    text=stripped,
                    xmin=max(0, int(min(xs))),
                    ymin=max(0, int(min(ys))),
                    xmax=min(w, int(max(xs))),
                    ymax=min(h, int(max(ys))),
                    confidence=float(conf),
                    img_width=w,
                    img_height=h,
                )
            )

    # 위→아래 순 정렬
    words.sort(key=lambda ww: ww.ymin)
    return words
