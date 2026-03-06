# Cursor 프롬프트 — 에이전트 로직 버그 3종 수정

아래 3개 파일의 버그를 수정해 주세요.
각 수정은 독립적이므로 순서대로 진행하면 됩니다.

---

## 수정 1 — `agent/skills.py` : MCC 분류 조건 버그 + 매핑 테이블 교체

### 배경

`merchant_risk_probe()` 함수의 MCC 위험도 분류 로직에 두 가지 문제가 있습니다.

**문제 A — `or mcc_str` 조건이 항상 True**

```python
# 현재 코드 (버그)
elif mcc_str in medium_mcc or mcc_str:   # mcc_str이 비어있지 않으면 항상 True
    base_risk = "MEDIUM"
else:
    base_risk = "UNKNOWN"                 # 이 분기는 절대 실행되지 않음
```

MCC 코드가 있는 모든 전표는 high_mcc에 없으면 무조건 MEDIUM으로 분류됩니다.
UNKNOWN 분기가 죽은 코드(dead code)가 되어, 점수 계산에서 MEDIUM 가중치(`+10점`)가
실제 위험하지 않은 업종에도 항상 적용됩니다.

**문제 B — MCC 매핑이 `screener.py`와 불일치**

`screener.py`의 `_MCC_LEISURE` 카테고리(7992 골프장, 7997 클럽 등)가
`skills.py`에는 `high_mcc`로 잘못 분류되어 있습니다.
같은 전표가 스크리닝 단계와 실행 단계에서 다른 위험등급으로 판정됩니다.

| MCC | screener.py | skills.py (현재·버그) | 올바른 분류 |
|-----|-------------|----------------------|------------|
| 7992 (골프장) | LEISURE | HIGH | LEISURE → HIGH* |
| 7997 (클럽) | LEISURE | HIGH | LEISURE → HIGH* |
| 7993, 7994 (게임) | HIGH | 없음(MEDIUM으로 낙하) | HIGH |
| 5999 | 없음 | HIGH | HIGH 유지 |
| 5811 (급식) | MEDIUM | 없음(MEDIUM으로 낙하) | MEDIUM |

*레저 업종은 screener에서 `PRIVATE_USE_RISK`로 분류되므로, 실행 단계에서도
`HIGH`(단순 위험)가 아닌 별도 `LEISURE` 카테고리로 처리해야 합니다.
단, `compound` 판정(휴일+레저)은 `HIGH`로 격상하는 기존 로직은 유지합니다.

### 수정 내용

**파일**: `agent/skills.py`
**함수**: `merchant_risk_probe()`

```python
# ── 수정 전 ──────────────────────────────────────────────────────────────────
async def merchant_risk_probe(context: dict[str, Any]) -> dict[str, Any]:
    body = context["body_evidence"]
    mcc = body.get("mccCode")
    merchant = body.get("merchantName")

    high_mcc = {"5813", "7992", "5912", "7997", "5999"}
    medium_mcc = {"5812", "5814", "7011", "4722"}
    mcc_str = str(mcc or "")
    if mcc_str in high_mcc:
        base_risk = "HIGH"
    elif mcc_str in medium_mcc or mcc_str:     # ← 버그
        base_risk = "MEDIUM"
    else:
        base_risk = "UNKNOWN"
```

```python
# ── 수정 후 ──────────────────────────────────────────────────────────────────
async def merchant_risk_probe(context: dict[str, Any]) -> dict[str, Any]:
    body = context["body_evidence"]
    mcc = body.get("mccCode")
    merchant = body.get("merchantName")

    # screener.py의 MCC 분류와 일치시킴
    high_mcc    = {"5813", "7993", "7994", "5912", "5999"}   # 주점·게임·약국 등
    leisure_mcc = {"7992", "7996", "7997", "7941", "7011"}   # 골프·놀이공원·클럽·호텔 등
    medium_mcc  = {"5812", "5811", "5814", "4722"}           # 일반 식음료·여행사 등

    mcc_str = str(mcc or "")
    if mcc_str in high_mcc:
        base_risk = "HIGH"
    elif mcc_str in leisure_mcc:
        base_risk = "LEISURE"    # 레저 카테고리 신규 추가
    elif mcc_str in medium_mcc:  # or mcc_str 제거 — 핵심 버그 수정
        base_risk = "MEDIUM"
    else:
        base_risk = "UNKNOWN"    # 이제 실제로 실행되는 분기
```

그리고 같은 함수의 compound 판정 로직도 LEISURE 카테고리를 처리하도록 수정:

```python
# ── 수정 전 ──────────────────────────────────────────────────────────────────
    if holiday_risk and base_risk == "HIGH":
        risk = "CRITICAL"
        compound_flags.append("휴일+고위험업종 복합")
    elif holiday_risk and base_risk == "MEDIUM":
        risk = "HIGH"
        compound_flags.append("휴일+중간위험업종 복합")
    else:
        risk = base_risk
```

```python
# ── 수정 후 ──────────────────────────────────────────────────────────────────
    if holiday_risk and base_risk == "HIGH":
        risk = "CRITICAL"
        compound_flags.append("휴일+고위험업종 복합")
    elif holiday_risk and base_risk in {"LEISURE", "MEDIUM"}:
        risk = "HIGH"
        label = "레저업종" if base_risk == "LEISURE" else "중간위험업종"
        compound_flags.append(f"휴일+{label} 복합")
    elif base_risk == "LEISURE":
        risk = "MEDIUM"    # 레저 단독은 MEDIUM으로 처리
    else:
        risk = base_risk
```

`return` 블록의 `facts` 딕셔너리에 `mccCategory` 필드도 추가:

```python
# ── 수정 전 ──────────────────────────────────────────────────────────────────
    return {
        "skill": "merchant_risk_probe",
        "ok": True,
        "facts": {
            "mccCode": mcc,
            "merchantName": merchant,
            "merchantRisk": risk,
            "compoundRiskFlags": compound_flags,
            "holidayRiskConsidered": holiday_risk,
        },
```

```python
# ── 수정 후 ──────────────────────────────────────────────────────────────────
    return {
        "skill": "merchant_risk_probe",
        "ok": True,
        "facts": {
            "mccCode": mcc,
            "merchantName": merchant,
            "merchantRisk": risk,
            "mccCategory": base_risk,          # 복합 판정 전 원본 카테고리 추가
            "compoundRiskFlags": compound_flags,
            "holidayRiskConsidered": holiday_risk,
        },
```

---

## 수정 2 — `services/evidence_verification.py` : `_chunk_supports_claim()` 조항 번호 매칭 완화

### 배경

`_chunk_supports_claim()` 함수의 **규칙 1(조항 번호 일치)**이 너무 엄격해서
실제로 규정 DB에 해당 조항이 있어도 검증을 통과하지 못하는 문제가 있습니다.

**문제**: `_build_verification_targets()`가 주장(claim)을 생성할 때
`제23조 ③-1항`, `제38조 ②항`처럼 **항(項) 번호까지 포함된** 세부 텍스트를 만들어냅니다.
그런데 규정 DB의 청크는 대부분 `제23조`, `제38조`처럼 **조(條) 단위**로만 저장되어 있습니다.

결과적으로 규칙 1의 정규식 `제\s*(\d+)\s*조`는 조 번호를 추출해 비교하는데,
주장에서 추출한 `["23", "38"]`이 청크에서 추출한 `["23"]`과 비교될 때
"23"은 통과하지만 "38"이 없어서 `any()` 조건이 **항상 False**가 됩니다.

```
주장: "제23조 ③-1항 ... 및 제38조 ②항 ... 에 해당한다."
  → claim_articles = ["23", "38"]

청크: "제23조 (식대 기준) ..."
  → chunk_articles = ["23"]

any("23" in ["23"], "38" in ["23"]) → True  (올바름)
```

위 예시는 실제로 통과되므로 `any()`가 맞습니다.
진짜 문제는 **주장에 조항이 여러 개 있을 때 청크 하나가 그 중 하나도 포함하지 않는 경우**입니다.

예를 들어 규정 DB의 청크가 `제39조`만 있고 주장에 `제23조`와 `제38조`가 있으면,
`any("23" in ["39"], "38" in ["39"])` → `False`가 됩니다.
이 경우 해당 청크는 배제되어야 하므로 로직 자체는 맞습니다.

**실제 버그**: `③-1항` 같은 세부 항 번호가 포함된 텍스트에서
정규식이 `조` 단위 숫자만 추출하므로, `③`, `-1` 등의 항 번호 텍스트가
chunk_text에 있어도 매칭 가중치를 얻지 못합니다.
이를 규칙 3의 numeric_bonus가 보완해야 하는데,
`③-1`은 `_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")` 패턴에
**한글/영문/숫자 2자 이상** 조건을 충족하지 못해 토큰화도 안 됩니다.

결과: 조항 세부 항 번호가 claim에만 있고 chunk에는 없으면
규칙 2의 overlap 점수가 줄어 `weighted >= 3` 임계값을 넘지 못해 검증 실패.
→ `coverage_ratio` 하락 → `gate_policy = "hold"` → **불필요한 HITL 발동**.

### 수정 내용

**파일**: `services/evidence_verification.py`
**함수**: `_chunk_supports_claim()`

```python
# ── 수정 전 ──────────────────────────────────────────────────────────────────
_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")


def _chunk_supports_claim(claim: str, chunk: dict[str, Any]) -> bool:
    if not claim or not chunk:
        return False

    claim_lower = (claim or "").lower()
    text_parts = [
        str(chunk.get("chunk_text") or ""),
        str(chunk.get("parent_title") or ""),
        str(chunk.get("article") or chunk.get("regulation_article") or ""),
        str(chunk.get("regulation_clause") or ""),
    ]
    combined = " ".join(text_parts).lower()

    # 규칙 1: 조항 번호 명시 시 청크와 일치 필수
    claim_articles = re.findall(r"제\s*(\d+)\s*조", claim_lower)
    if claim_articles:
        chunk_articles = re.findall(r"제\s*(\d+)\s*조", combined)
        if not any(article in chunk_articles for article in claim_articles):
            return False

    # 규칙 2: 의미 단어 추출 (불용어 제거)
    stop_words = {
        "이", "가", "을", "를", "의", "에", "에서", "으로", "로", "와", "과",
        "이다", "있음", "있다", "수", "하며", "하여", "해당", "필요", "경우",
        "대상", "조항", "기준", "해야", "한다", "되어", "위반", "가능성",
    }
    claim_words = {word for word in _WORD_RE.findall(claim_lower) if len(word) >= 2 and word not in stop_words}
    chunk_words = {word for word in _WORD_RE.findall(combined) if len(word) >= 2 and word not in stop_words}

    if len(claim_words) < 2:
        return bool(chunk)

    overlap = len(claim_words & chunk_words)

    # 규칙 3: 숫자·코드 키워드 가중치
    numeric_bonus = len({word for word in claim_words if any(ch.isdigit() for ch in word)} & chunk_words)
    weighted = overlap + numeric_bonus

    return weighted >= 3
```

```python
# ── 수정 후 ──────────────────────────────────────────────────────────────────
_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")
# 항(項) 번호 패턴: ①②③ 원문자, -1/-2 세부항, ①항 등
_CLAUSE_RE = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩]|제\s*\d+\s*[항조]|\d+\s*항")


def _chunk_supports_claim(claim: str, chunk: dict[str, Any]) -> bool:
    if not claim or not chunk:
        return False

    claim_lower = (claim or "").lower()
    text_parts = [
        str(chunk.get("chunk_text") or ""),
        str(chunk.get("parent_title") or ""),
        str(chunk.get("article") or chunk.get("regulation_article") or ""),
        str(chunk.get("regulation_clause") or ""),
    ]
    combined = " ".join(text_parts).lower()

    # 규칙 1: 조항 번호 명시 시 — 조(條) 번호 기준으로만 비교 (항 번호는 제외)
    # 변경 이유: claim은 "제23조 ③-1항"처럼 세부 항까지 포함하지만,
    # 규정 DB 청크는 대부분 조(條) 단위로 저장되어 있어
    # 항 번호까지 요구하면 실제로 관련된 청크를 모두 탈락시키게 됨.
    # 조 번호 중 하나라도 청크에 있으면 통과 (any → 유지, 조 단위 매칭만)
    claim_articles = re.findall(r"제\s*(\d+)\s*조", claim_lower)
    if claim_articles:
        chunk_articles = re.findall(r"제\s*(\d+)\s*조", combined)
        if not any(article in chunk_articles for article in claim_articles):
            return False

    # 규칙 2: 의미 단어 추출 (불용어 제거)
    stop_words = {
        "이", "가", "을", "를", "의", "에", "에서", "으로", "로", "와", "과",
        "이다", "있음", "있다", "수", "하며", "하여", "해당", "필요", "경우",
        "대상", "조항", "기준", "해야", "한다", "되어", "위반", "가능성",
        # 추가 불용어: 세부 항 표기가 단독으로 매칭되는 것 방지
        "항", "호", "목",
    }
    claim_words = {word for word in _WORD_RE.findall(claim_lower) if len(word) >= 2 and word not in stop_words}
    chunk_words = {word for word in _WORD_RE.findall(combined) if len(word) >= 2 and word not in stop_words}

    if len(claim_words) < 2:
        return bool(chunk)

    overlap = len(claim_words & chunk_words)

    # 규칙 3: 숫자·코드 키워드 가중치 (기존 유지)
    numeric_bonus = len({word for word in claim_words if any(ch.isdigit() for ch in word)} & chunk_words)

    # 규칙 4 (신규): 핵심 도메인 키워드 가중치
    # 심야·휴일·예산·업종 같은 핵심 판단 키워드가 claim과 chunk 양쪽에 있으면 추가 가중치
    domain_keywords = {"심야", "휴일", "주말", "공휴일", "식대", "예산", "초과", "승인", "업종", "가맹점", "한도"}
    domain_bonus = len({w for w in claim_words if w in domain_keywords} & chunk_words)

    weighted = overlap + numeric_bonus + domain_bonus

    # 임계값: 기존 3 유지 (규칙 4 추가로 통과율 적절히 상승)
    return weighted >= 3
```

---

## 수정 3 — `agent/langgraph_agent.py` : `_build_verification_targets()` 조항 번호 동적 참조

### 배경

`_build_verification_targets()` 함수가 검증 대상 주장(claim)을 생성할 때
**`제23조`, `제38조`, `제39조`** 등의 조항 번호가 **소스코드에 하드코딩**되어 있습니다.

이 조항 번호들은 특정 규정집 버전의 번호이며, 규정집이 개정되어 조항 번호가 바뀌면
생성된 주장이 항상 검증 실패하게 됩니다.
또한 `policy_rulebook_probe`가 이미 실제 DB에서 관련 조항을 조회해서
`policy_refs`에 담아두는데, 이를 활용하지 않고 하드코딩된 번호를 쓰는 것은 낭비입니다.

**수정 전략**: 하드코딩 조항 번호를 완전히 제거하는 것은 주장 품질을 낮출 수 있으므로,
대신 **`policy_refs`에서 관련 조항이 있으면 그 번호를 우선 사용**하고,
없을 때만 하드코딩 번호를 fallback으로 사용하도록 변경합니다.

### 수정 내용

**파일**: `agent/langgraph_agent.py`
**함수**: `_build_verification_targets()`

함수 시작 부분에 `policy_refs`에서 조항 번호를 추출하는 헬퍼를 추가하고,
각 claim 생성 블록에서 동적 조항 번호를 우선 사용합니다.

```python
# ── 수정 전 (심야 위반 claim 블록) ──────────────────────────────────────────
    if is_night and time_part:
        claims.append((
            _CLAIM_PRIORITY["night_violation"],
            f"{date_part} {time_part} 심야 시간대에 {merchant}에서 {amount_str} 결제가 발생하여 "
            f"제23조 ③-1항 '23:00~06:00 심야 식대 경고 대상' 및 "
            f"제38조 ②항 '심야 시간대 지출 검토 대상'에 해당한다.",
        ))
```

```python
# ── 수정 후 (함수 상단에 헬퍼 추출 로직 추가 후) ────────────────────────────

# _build_verification_targets() 함수 내 policy_refs 조회 직후에 아래 헬퍼 추가:

def _pick_article(policy_refs: list[dict], keywords: list[str], fallback: str) -> str:
    """
    policy_refs에서 keywords 중 하나라도 포함하는 조항을 찾아 반환.
    없으면 fallback 문자열 반환.
    """
    for ref in policy_refs:
        article = str(ref.get("article") or "")
        title = str(ref.get("parent_title") or "")
        combined = (article + " " + title).lower()
        if any(kw.lower() in combined for kw in keywords):
            return article
    return fallback

# 심야 위반 claim — 동적 조항 번호 사용
    if is_night and time_part:
        night_article = _pick_article(policy_refs, ["심야", "식대", "23조"], "제23조")
        time_article  = _pick_article(policy_refs, ["시간대", "지출", "38조"], "제38조")
        claims.append((
            _CLAIM_PRIORITY["night_violation"],
            f"{date_part} {time_part} 심야 시간대에 {merchant}에서 {amount_str} 결제가 발생하여 "
            f"{night_article} 심야 식대 경고 대상 및 "
            f"{time_article} 심야 시간대 지출 검토 대상에 해당한다.",
        ))
```

같은 방식으로 나머지 claim 블록도 수정합니다:

```python
# ── 수정 전 (휴일+근태 claim 블록) ──────────────────────────────────────────
    if is_holiday and hr_status in {"LEAVE", "OFF", "VACATION"}:
        ...
        claims.append((
            _CLAIM_PRIORITY["holiday_hr_conflict"],
            f"결제일({date_part}) 근태 상태 {hr_status}({hr_label}) 및 휴일 결제가 동시에 확인되어 "
            f"제39조 ①항 주말·공휴일 지출 제한과 "
            f"제23조 ③-2항 '주말/공휴일 식대(예외 승인 없는 경우)' 경고 조건 모두에 해당한다.",
        ))
```

```python
# ── 수정 후 ──────────────────────────────────────────────────────────────────
    if is_holiday and hr_status in {"LEAVE", "OFF", "VACATION"}:
        ...
        holiday_article = _pick_article(policy_refs, ["주말", "공휴일", "지출", "39조"], "제39조")
        food_article    = _pick_article(policy_refs, ["식대", "공휴일", "주말", "23조"], "제23조")
        claims.append((
            _CLAIM_PRIORITY["holiday_hr_conflict"],
            f"결제일({date_part}) 근태 상태 {hr_status}({hr_label}) 및 휴일 결제가 동시에 확인되어 "
            f"{holiday_article} 주말·공휴일 지출 제한과 "
            f"{food_article} 주말/공휴일 식대 경고 조건에 해당한다.",
        ))
```

```python
# ── 수정 전 (고위험 업종 claim 블록) ─────────────────────────────────────────
    if merchant_risk in {"HIGH", "CRITICAL"} and mcc_code:
        ...
        claims.append((
            _CLAIM_PRIORITY["merchant_high_risk"],
            f"{merchant}({mcc_display})은 제42조 {compound} 업종으로 분류되어 "
            f"금액과 무관하게 강화 승인 대상이며, 제11조 ③항 고위험 업종 거래 강화 승인 조건을 충족한다.",
        ))
```

```python
# ── 수정 후 ──────────────────────────────────────────────────────────────────
    if merchant_risk in {"HIGH", "CRITICAL"} and mcc_code:
        ...
        mcc_article      = _pick_article(policy_refs, ["업종", "가맹점", "42조"], "제42조")
        approval_article = _pick_article(policy_refs, ["강화승인", "고위험", "11조"], "제11조")
        claims.append((
            _CLAIM_PRIORITY["merchant_high_risk"],
            f"{merchant}({mcc_display})은 {mcc_article} {compound} 업종으로 분류되어 "
            f"금액과 무관하게 강화 승인 대상이며, {approval_article} 고위험 업종 거래 강화 승인 조건을 충족한다.",
        ))
```

```python
# ── 수정 전 (예산 초과 claim 블록) ───────────────────────────────────────────
    if budget_exceeded:
        claims.append((
            _CLAIM_PRIORITY["budget_exceeded"],
            f"{amount_str} 결제가 예산 한도를 초과하여 제40조 ①항 금액·누적한도 제약 및 "
            f"제19조 ①항 예산 초과 처리 기준에 따른 상위 승인이 필요하다.",
        ))
```

```python
# ── 수정 후 ──────────────────────────────────────────────────────────────────
    if budget_exceeded:
        budget_article   = _pick_article(policy_refs, ["한도", "초과", "40조"], "제40조")
        process_article  = _pick_article(policy_refs, ["예산", "처리", "19조"], "제19조")
        claims.append((
            _CLAIM_PRIORITY["budget_exceeded"],
            f"{amount_str} 결제가 예산 한도를 초과하여 {budget_article} 금액·누적한도 제약 및 "
            f"{process_article} 예산 초과 처리 기준에 따른 상위 승인이 필요하다.",
        ))
```

```python
# ── 수정 전 (금액 승인 구간 claim 블록) ──────────────────────────────────────
    if amount and not budget_exceeded:
        if amount >= 2_000_000:
            claims.append((
                _CLAIM_PRIORITY["amount_approval_tier"],
                f"{amount_str}은 제11조 ②-4항 임원·CFO 승인 구간(200만원 초과)에 해당하며 "
                f"증빙 완결성과 결재권자 확인이 필수이다.",
            ))
        elif amount >= 500_000:
            claims.append((
                _CLAIM_PRIORITY["amount_approval_tier"],
                f"{amount_str}은 제11조 ②-3항 본부장 승인 구간(50만~200만원)에 해당한다.",
            ))
        elif amount >= 100_000:
            claims.append((
                _CLAIM_PRIORITY["amount_approval_tier"] - 1,
                f"{amount_str}은 제11조 ②-2항 부서장 승인 구간(10만~50만원)에 해당한다.",
            ))
```

```python
# ── 수정 후 ──────────────────────────────────────────────────────────────────
    if amount and not budget_exceeded:
        tier_article = _pick_article(policy_refs, ["승인", "구간", "결재", "11조"], "제11조")
        if amount >= 2_000_000:
            claims.append((
                _CLAIM_PRIORITY["amount_approval_tier"],
                f"{amount_str}은 {tier_article} 임원·CFO 승인 구간(200만원 초과)에 해당하며 "
                f"증빙 완결성과 결재권자 확인이 필수이다.",
            ))
        elif amount >= 500_000:
            claims.append((
                _CLAIM_PRIORITY["amount_approval_tier"],
                f"{amount_str}은 {tier_article} 본부장 승인 구간(50만~200만원)에 해당한다.",
            ))
        elif amount >= 100_000:
            claims.append((
                _CLAIM_PRIORITY["amount_approval_tier"] - 1,
                f"{amount_str}은 {tier_article} 부서장 승인 구간(10만~50만원)에 해당한다.",
            ))
```

> **주의**: `_pick_article()` 헬퍼는 `_build_verification_targets()` 함수 **내부**에
> 중첩 함수(nested function)로 정의하거나, 같은 파일의 모듈 레벨 유틸리티로 추가하면 됩니다.
> 함수 내부에 정의할 경우 `policy_refs`를 클로저로 캡처하므로 인자를 줄일 수 있습니다.

---

## 수정 검증 방법

각 수정 완료 후 아래 조건으로 동작을 확인합니다.

### 수정 1 검증 (skills.py)

```python
# MCC가 없는 전표 → UNKNOWN 이어야 함 (버그 수정 확인)
result = await merchant_risk_probe({
    "body_evidence": {"mccCode": None, "merchantName": "테스트"},
    "prior_tool_results": []
})
assert result["facts"]["merchantRisk"] == "UNKNOWN"

# 골프장(7992) 단독 → MEDIUM 이어야 함 (LEISURE → MEDIUM 변환)
result = await merchant_risk_probe({
    "body_evidence": {"mccCode": "7992", "merchantName": "OO골프장"},
    "prior_tool_results": []
})
assert result["facts"]["merchantRisk"] == "MEDIUM"
assert result["facts"]["mccCategory"] == "LEISURE"

# 골프장(7992) + 휴일 → HIGH 이어야 함 (복합 위험)
result = await merchant_risk_probe({
    "body_evidence": {"mccCode": "7992", "merchantName": "OO골프장"},
    "prior_tool_results": [
        {"skill": "holiday_compliance_probe", "facts": {"holidayRisk": True}}
    ]
})
assert result["facts"]["merchantRisk"] == "HIGH"
```

### 수정 2 검증 (evidence_verification.py)

```python
# 조 번호가 claim에만 있고 chunk에 없으면 → False
assert not _chunk_supports_claim(
    "제99조 조항 위반이다.",
    {"chunk_text": "제23조 식대 기준 관련 내용", "regulation_article": "제23조"}
)

# 조 번호가 일치하고 도메인 키워드가 있으면 → True
assert _chunk_supports_claim(
    "제23조 심야 식대 위반 경고 대상이다.",
    {"chunk_text": "제23조 심야 식대 기준 내용", "regulation_article": "제23조"}
)

# 조 번호 없고 도메인 키워드 3개 이상 중복 → True
assert _chunk_supports_claim(
    "심야 휴일 식대 사용은 예산 승인 필요하다.",
    {"chunk_text": "심야 휴일 식대 규정 예산 승인 기준"}
)
```

### 수정 3 검증 (langgraph_agent.py)

```python
# policy_refs에 관련 조항이 있으면 claim에 동적 조항 번호가 반영되는지 확인
from agent.langgraph_agent import _build_verification_targets
state = {
    "body_evidence": {"occurredAt": "2024-01-06T23:30:00", "amount": 50000,
                      "merchantName": "테스트식당", "isHoliday": False},
    "flags": {"isNight": True, "isHoliday": False, "budgetExceeded": False},
    "tool_results": [
        {"skill": "policy_rulebook_probe", "facts": {
            "policy_refs": [
                {"article": "제5조", "parent_title": "심야 식대 기준", "adoption_reason": "심야 적용"}
            ]
        }}
    ]
}
claims = _build_verification_targets(state)
# 하드코딩 "제23조"가 아닌 DB에서 온 "제5조"가 사용되어야 함
assert any("제5조" in c for c in claims), f"동적 조항 번호 미반영: {claims}"
```

---

## 수정 범위 요약

| 파일 | 변경 유형 | 변경 규모 |
|------|-----------|-----------|
| `agent/skills.py` | 버그 수정 + MCC 테이블 교체 | ~15줄 |
| `services/evidence_verification.py` | 로직 개선 (규칙 4 추가, 불용어 보강) | ~10줄 |
| `agent/langgraph_agent.py` | 헬퍼 함수 추가 + claim 생성 5개 블록 수정 | ~40줄 |

기존 함수 시그니처와 반환 타입은 모두 유지됩니다.
DB 마이그레이션이나 환경변수 변경은 필요하지 않습니다.