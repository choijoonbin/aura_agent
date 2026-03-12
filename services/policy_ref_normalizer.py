from __future__ import annotations

import re
from typing import Any


def _clean(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _article_token(article: str) -> str:
    return "".join(article.split())


def normalize_policy_parent_title(article: Any, parent_title: Any) -> str:
    """
    채택 조항의 표시 제목을 조항 기준으로 정규화한다.

    예)
    - article=제39조, parent_title='제39조 (...) ~ 제40조 (...)' -> '제39조 (...)'
    - article=제12조, parent_title='제12조' -> '제12조'
    """
    art = _clean(article)
    title = _clean(parent_title)

    if not art:
        return title
    if not title:
        return art

    art_tok = _article_token(art)
    title_tok = _article_token(title)

    # 병합 타이틀( ~ )에서 현재 article이 포함된 조각만 선택
    if "~" in title:
        parts = [_clean(p) for p in re.split(r"\s*~\s*", title) if _clean(p)]
        for p in parts:
            if art_tok and art_tok in _article_token(p):
                return p
        # article을 찾지 못하면 첫 조각 반환(안정 fallback)
        if parts:
            return parts[0]

    # title이 article로 시작하지 않으면 article prefix를 보정
    if art_tok and art_tok not in title_tok:
        return f"{art} ({title})"

    return title


def policy_display_label(article: Any, clause: Any, parent_title: Any) -> str:
    """UI/그래프 노드에서 사용할 Policy 라벨."""
    art = _clean(article)
    cl = _clean(clause)
    title = normalize_policy_parent_title(art, parent_title)

    if cl:
        # title이 article과 동일하면 clause를 붙여 정보 손실 방지
        if _article_token(title) == _article_token(art):
            return f"{art} {cl}".strip()
    return title or (f"{art} {cl}".strip() if (art or cl) else "-")
