# Cursor 작업 프롬프트 [1/2] — Verifier 검증 타겟 생성 고도화

## 현황 진단 (소스 직접 분석)

### 현재 `_build_verification_targets()` 전체 코드 (L589~606)

```python
def _build_verification_targets(state: AgentState) -> list[str]:
    probe = _find_tool_result(state.get("tool_results", []), "policy_rulebook_probe")
    refs = (probe or {}).get("facts", {}).get("policy_refs") or []
    targets: list[str] = []
    for ref in refs[:3]:
        art = ref.get("article") or ""
        title = (ref.get("parent_title") or "")[:50]
        if art or title:
            targets.append(f"{art} {title} 조항이 해당 사례에 적용될 수 있음.".strip())
    if targets:
        return targets
    plan = state.get("planner_output") or {}
    for step in (plan.get("steps") or [])[:3]:
        purpose = (step.get("purpose") or "").strip()
        if purpose:
            targets.append(purpose)
    return targets[:3]
```

### `_chunk_supports_claim()` 현재 판정 기준 (evidence_verification.py L23~37)

```python
def _chunk_supports_claim(claim: str, chunk: dict) -> bool:
    words = set(_WORD_RE.findall((claim or "").lower()))
    if len(words) < 2:
        return bool(chunk)
    combined = chunk_text + parent_title + article + ...
    chunk_words = set(_WORD_RE.findall(combined.lower()))
    overlap = len(words & chunk_words)
    return overlap >= 2   # ← 단어 2개만 겹쳐도 "검증됨"
```

---

### 3가지 문제의 실제 영향

**문제 1 — 타겟이 항상 `coverage_ratio = 1.0`을 만든다**

현재 생성되는 타겟:
```
"제23조 (식대) 조항이 해당 사례에 적용될 수 있음."
```
- 타겟 단어: `{"제23조", "식대", "조항", "해당", "사례", "적용"}`
- 청크 단어: `{"제23조", "식대", "인당", "기준한도", "참석자"}`
- overlap = `{"제23조", "식대"}` = **2개 → 즉시 covered=True**

→ Verifier가 "검증 완료"를 선언하지만 **아무것도 검증하지 않음**

**문제 2 — tool_results의 실제 사실값이 타겟에 전혀 반영 안 됨**

이미 실행 완료된 도구 결과:
```
holiday_compliance_probe → {"holidayRisk": True, "hrStatus": "LEAVE"}   ← 무시
merchant_risk_probe      → {"merchantRisk": "HIGH", "mccCode": "5813"}  ← 무시
```
타겟 생성 시 `policy_rulebook_probe`의 `article`/`parent_title`만 참조

**문제 3 — 전표 맥락(시간·금액·근태·MCC) 완전 부재**

좋은 검증 타겟 = **"누가 언제 얼마를 어디서 → 어떤 규정 몇 조 몇 항을 위반"** 형태여야 함

---

## 작업 범위

| 파일 | 함수 | 작업 유형 |
|------|------|---------|
| `agent/langgraph_agent.py` | `_build_verification_targets()` | 전면 교체 |
| `agent/output_models.py` | `ClaimVerificationResult` 신규 + `VerifierOutput` 필드 추가 | 모델 확장 |
| `agent/langgraph_agent.py` | `verify_node()` | claim별 추적 블록 삽입 |
| `services/evidence_verification.py` | `_chunk_supports_claim()` | 판정 기준 강화 |
| `tests/test_graph.py` | `TestVerificationTargets` 클래스 신규 | 단위 테스트 |

---

## 상세 구현 명세

---

### ① `agent/langgraph_agent.py` — `_build_verification_targets()` 전면 교체

기존 함수(L589~606) 전체를 아래 코드로 교체한다.

```python
# ── 검증 타겟 우선순위 상수 ──────────────────────────────────────────────────
_CLAIM_PRIORITY: dict[str, int] = {
    "night_violation":      10,
    "holiday_hr_conflict":   9,
    "merchant_high_risk":    8,
    "budget_exceeded":       7,
    "amount_approval_tier":  6,
    "policy_ref_direct":     5,
}


def _build_verification_targets(state: AgentState) -> list[str]:
    """
    Verifier가 검증할 구체적·반박 가능한 주장 문장 최대 4개 생성.

    설계 원칙:
    ① 전표 사실(시간·금액·근태·MCC)을 주장에 직접 삽입
    ② 특정 조항 번호(제XX조 ③항)까지 명시
    ③ "적용될 수 있음" 대신 "해당한다 / 위반 가능성" 수준의 주장
    ④ _chunk_supports_claim()이 단순 단어 중복만으로 통과하지 못하도록
       충분히 구체적으로 작성 → 실제 coverage_ratio가 의미 있게 변동함
    ⑤ tool_results의 실제 facts 값을 반드시 참조
    """
    body         = state["body_evidence"]
    flags        = state.get("flags") or {}
    tool_results = state.get("tool_results") or []

    # ── 전표 사실 추출 ──────────────────────────────────────────────────────
    occurred_at = str(body.get("occurredAt") or "")
    date_part   = occurred_at[:10] if len(occurred_at) >= 10 else "날짜 미상"
    time_part   = occurred_at[11:16] if len(occurred_at) >= 16 else ""
    amount      = body.get("amount")
    amount_str  = f"{int(amount):,}원" if amount else "금액 미상"
    merchant    = body.get("merchantName") or "거래처 미상"
    mcc_code    = body.get("mccCode") or flags.get("mccCode") or ""
    mcc_name    = body.get("mccName") or ""
    hr_status   = str(flags.get("hrStatus") or body.get("hrStatus") or "").upper()
    is_holiday  = bool(flags.get("isHoliday") or body.get("isHoliday"))
    is_night    = bool(flags.get("isNight"))
    budget_exceeded = bool(flags.get("budgetExceeded"))

    # ── 도구 결과 facts 추출 ─────────────────────────────────────────────────
    holiday_facts  = (_find_tool_result(tool_results, "holiday_compliance_probe") or {}).get("facts") or {}
    merchant_facts = (_find_tool_result(tool_results, "merchant_risk_probe")      or {}).get("facts") or {}
    policy_facts   = (_find_tool_result(tool_results, "policy_rulebook_probe")    or {}).get("facts") or {}

    merchant_risk = str(merchant_facts.get("merchantRisk") or "").upper()
    holiday_risk  = bool(holiday_facts.get("holidayRisk"))
    policy_refs   = policy_facts.get("policy_refs") or []

    # ── 주장 생성 ────────────────────────────────────────────────────────────
    claims: list[tuple[int, str]] = []

    # 1. 심야 시간대 위반
    if is_night and time_part:
        claims.append((
            _CLAIM_PRIORITY["night_violation"],
            f"{date_part} {time_part} 심야 시간대에 {merchant}에서 {amount_str} 결제가 발생하여 "
            f"제23조 ③-1항 '23:00~06:00 심야 식대 경고 대상' 및 "
            f"제38조 ②항 '심야 시간대 지출 검토 대상'에 해당한다.",
        ))

    # 2. 휴일 + 근태 충돌
    if is_holiday and hr_status in {"LEAVE", "OFF", "VACATION"}:
        hr_label = {"LEAVE": "휴가·결근", "OFF": "휴무", "VACATION": "휴가"}.get(hr_status, hr_status)
        claims.append((
            _CLAIM_PRIORITY["holiday_hr_conflict"],
            f"결제일({date_part}) 근태 상태 {hr_status}({hr_label}) 및 휴일 결제가 동시에 확인되어 "
            f"제39조 ①항 주말·공휴일 지출 제한과 "
            f"제23조 ③-2항 '주말/공휴일 식대(예외 승인 없는 경우)' 경고 조건 모두에 해당한다.",
        ))
    elif (is_holiday or holiday_risk) and not hr_status:
        claims.append((
            _CLAIM_PRIORITY["holiday_hr_conflict"] - 1,
            f"결제일({date_part})이 휴일로 확인되나 근태 상태 데이터가 누락되어 "
            f"제39조 주말·공휴일 지출 제한 적용 여부 완전 판단이 불가하다. 근태 보완 후 재검토 필요.",
        ))

    # 3. 고위험 업종(MCC)
    if merchant_risk in {"HIGH", "CRITICAL"} and mcc_code:
        mcc_display = f"MCC {mcc_code}({mcc_name})" if mcc_name else f"MCC {mcc_code}"
        compound = "복합 위험" if merchant_risk == "CRITICAL" else "고위험"
        claims.append((
            _CLAIM_PRIORITY["merchant_high_risk"],
            f"{merchant}({mcc_display})은 제42조 {compound} 업종으로 분류되어 "
            f"금액과 무관하게 강화 승인 대상이며, 제11조 ③항 고위험 업종 거래 강화 승인 조건을 충족한다.",
        ))
    elif merchant_risk == "MEDIUM" and mcc_code:
        claims.append((
            _CLAIM_PRIORITY["merchant_high_risk"] - 2,
            f"{merchant}(MCC {mcc_code}) 업종 위험도 MEDIUM으로 제42조 업종 제한 기준 검토 대상이다.",
        ))

    # 4. 예산 초과
    if budget_exceeded:
        claims.append((
            _CLAIM_PRIORITY["budget_exceeded"],
            f"{amount_str} 결제가 예산 한도를 초과하여 제40조 ①항 금액·누적한도 제약 및 "
            f"제19조 ①항 예산 초과 처리 기준에 따른 상위 승인이 필요하다.",
        ))

    # 5. 금액 구간 승인 기준
    if amount and not budget_exceeded:
        if amount >= 2_000_000:
            claims.append((_CLAIM_PRIORITY["amount_approval_tier"],
                f"{amount_str}은 제11조 ②-4항 임원·CFO 승인 구간(200만원 초과)에 해당하며 "
                f"증빙 완결성과 결재권자 확인이 필수이다."))
        elif amount >= 500_000:
            claims.append((_CLAIM_PRIORITY["amount_approval_tier"],
                f"{amount_str}은 제11조 ②-3항 본부장 승인 구간(50만~200만원)에 해당한다."))
        elif amount >= 100_000:
            claims.append((_CLAIM_PRIORITY["amount_approval_tier"] - 1,
                f"{amount_str}은 제11조 ②-2항 부서장 승인 구간(10만~50만원)에 해당한다."))

    # 6. 채택된 policy_refs 직접 인용
    for ref in policy_refs[:2]:
        art    = ref.get("article") or ""
        ptitle = (ref.get("parent_title") or "")[:35]
        reason = ref.get("adoption_reason") or ""
        if art:
            reason_part = f" ({reason})" if reason else ""
            claims.append((
                _CLAIM_PRIORITY["policy_ref_direct"],
                f"policy_rulebook_probe 채택 조항 {art}({ptitle}){reason_part}이 "
                f"{merchant} {amount_str} 전표에 직접 적용 가능한 위반 근거를 갖는다.",
            ))

    # ── 우선순위 정렬 후 최대 4개 반환 ───────────────────────────────────────
    claims.sort(key=lambda x: x[0], reverse=True)
    if claims:
        return [text for _, text in claims[:4]]

    return [
        f"{merchant} {amount_str} 전표({date_part})가 사내 경비 지출 관리 규정 위반 여부 "
        f"검토 대상으로 판정되었으며 세부 조항 적용 근거 확인이 필요하다."
    ]
```

---

### ② `agent/output_models.py` — `ClaimVerificationResult` 신규 + `VerifierOutput` 필드 추가

`VerifierOutput` 클래스 **정의 바로 앞**에 새 모델 추가:

```python
class ClaimVerificationResult(BaseModel):
    """개별 검증 타겟 주장에 대한 검증 결과."""
    claim: str = Field(description="검증 대상 주장 문장 전체")
    covered: bool = Field(description="retrieval 청크로 뒷받침 가능 여부")
    supporting_articles: list[str] = Field(
        default_factory=list,
        description="이 주장을 실제로 뒷받침한 규정 조항 번호 목록"
    )
    gap: str = Field(
        default="",
        description="covered=False일 때 어떤 근거가 부족한지 설명"
    )
```

`VerifierOutput` 기존 클래스에 아래 필드 추가:
```python
class VerifierOutput(BaseModel):
    # ... 기존 필드 전체 유지 ...
    claim_results: list[ClaimVerificationResult] = Field(
        default_factory=list,
        description="주장(claim)별 개별 검증 결과 목록 (신규)"
    )
```

---

### ③ `agent/langgraph_agent.py` — `verify_node()` claim별 추적 블록 삽입

`verify_node()` 내부, `verification_summary` 계산 직후 ~
`verifier_output = VerifierOutput(...)` 직전에 삽입:

```python
    # ── 주장별 개별 검증 결과 생성 (신규) ─────────────────────────────────
    import re as _re_v
    from agent.output_models import ClaimVerificationResult

    _WP = _re_v.compile(r"[가-힣A-Za-z0-9]{2,}")
    _SW = {"이", "가", "을", "를", "의", "에", "으로", "로", "와", "과",
           "이다", "있음", "있다", "수", "하며", "하여", "해당", "필요",
           "대상", "조항", "기준", "한다", "되어", "위반", "가능성"}

    claim_results: list[ClaimVerificationResult] = []
    for detail in (verification_summary.get("details") or []):
        idx        = detail.get("index", 0)
        claim_text = verification_targets[idx] if idx < len(verification_targets) else ""
        is_covered = bool(detail.get("covered"))

        supporting: list[str] = []
        if is_covered and retrieved_chunks:
            claim_words = {w for w in _WP.findall(claim_text.lower()) if w not in _SW}
            for chunk in retrieved_chunks:
                chunk_combined = " ".join([
                    str(chunk.get("chunk_text") or ""),
                    str(chunk.get("parent_title") or ""),
                    str(chunk.get("article") or chunk.get("regulation_article") or ""),
                ])
                chunk_words = {w for w in _WP.findall(chunk_combined.lower()) if w not in _SW}
                if len(claim_words & chunk_words) >= 3:
                    art = chunk.get("article") or chunk.get("regulation_article")
                    if art and art not in supporting:
                        supporting.append(art)

        gap_text = ""
        if not is_covered:
            if "심야" in claim_text or "23:00" in claim_text:
                gap_text = "심야 시간대 규정 조항(제23조/제38조)이 retrieval 결과에 포함되지 않음"
            elif "LEAVE" in claim_text or "휴일" in claim_text or "근태" in claim_text:
                gap_text = "근태·휴일 지출 연계 규정 청크 부족"
            elif "MCC" in claim_text or "업종" in claim_text:
                gap_text = "고위험 업종 관련 조항(제42조)이 retrieval 결과에 부재"
            elif "예산" in claim_text:
                gap_text = "예산 초과 관련 조항(제40조/제19조) 청크 미확보"
            else:
                gap_text = "해당 주장을 뒷받침할 규정 청크를 retrieval에서 찾지 못함"

        claim_results.append(ClaimVerificationResult(
            claim=claim_text,
            covered=is_covered,
            supporting_articles=supporting[:3],
            gap=gap_text,
        ))
```

`VerifierOutput(...)` 생성 시 `claim_results=claim_results` 추가:
```python
    verifier_output = VerifierOutput(
        grounded=not needs_hitl,
        needs_hitl=needs_hitl,
        missing_evidence=...,
        gate=gate,
        rationale=...,
        quality_signals=...,
        claim_results=claim_results,   # ← 신규
    )
```

---

### ④ `services/evidence_verification.py` — `_chunk_supports_claim()` 전면 교체

```python
def _chunk_supports_claim(claim: str, chunk: dict[str, Any]) -> bool:
    """
    강화된 판정 기준:
    규칙 1. 주장에 '제XX조'가 명시된 경우 청크도 해당 조항을 포함해야 함 (필수)
    규칙 2. 불용어 제거 후 의미 단어 3개 이상 중복 (기존 2개에서 상향)
    규칙 3. 숫자·코드 키워드 추가 가중치
    """
    if not claim or not chunk:
        return False

    import re as _re_ec
    claim_lower = (claim or "").lower()
    text_parts  = [
        str(chunk.get("chunk_text") or ""),
        str(chunk.get("parent_title") or ""),
        str(chunk.get("article") or chunk.get("regulation_article") or ""),
        str(chunk.get("regulation_clause") or ""),
    ]
    combined = " ".join(text_parts).lower()

    # 규칙 1: 조항 번호 명시 시 청크와 일치 필수
    claim_articles = _re_ec.findall(r"제\s*(\d+)\s*조", claim_lower)
    if claim_articles:
        chunk_articles = _re_ec.findall(r"제\s*(\d+)\s*조", combined)
        if not any(a in chunk_articles for a in claim_articles):
            return False

    # 규칙 2: 의미 단어 추출 (불용어 제거)
    _STOP = {
        "이", "가", "을", "를", "의", "에", "에서", "으로", "로", "와", "과",
        "이다", "있음", "있다", "수", "하며", "하여", "해당", "필요", "경우",
        "대상", "조항", "기준", "해야", "한다", "되어", "위반", "가능성",
    }
    claim_words = {w for w in _WORD_RE.findall(claim_lower) if len(w) >= 2 and w not in _STOP}
    chunk_words = {w for w in _WORD_RE.findall(combined)     if len(w) >= 2 and w not in _STOP}

    if len(claim_words) < 2:
        return bool(chunk)

    overlap = len(claim_words & chunk_words)

    # 규칙 3: 숫자·코드 키워드 가중치
    numeric_bonus = len({w for w in claim_words if any(c.isdigit() for c in w)} & chunk_words)
    weighted = overlap + numeric_bonus

    return weighted >= 3
```

---

### ⑤ `tests/test_graph.py` — `TestVerificationTargets` 클래스 추가

```python
class TestVerificationTargets(unittest.TestCase):

    def _state(self, *, night=False, holiday=False, hr="WORKING",
               mcc=None, amount=140937, m_risk="MEDIUM", h_risk=False, refs=None):
        return {
            "body_evidence": {
                "occurredAt": "2026-03-07T22:24:00", "amount": amount,
                "merchantName": "POC 심야 식대", "mccCode": mcc,
                "hrStatus": hr, "isHoliday": holiday,
            },
            "flags": {
                "isHoliday": holiday, "isNight": night, "hrStatus": hr,
                "mccCode": mcc, "budgetExceeded": False, "amount": amount,
            },
            "tool_results": [
                {"skill": "holiday_compliance_probe", "ok": True,
                 "facts": {"holidayRisk": h_risk, "isHoliday": holiday}},
                {"skill": "merchant_risk_probe", "ok": True,
                 "facts": {"merchantRisk": m_risk, "mccCode": mcc}},
                {"skill": "policy_rulebook_probe", "ok": True,
                 "facts": {"ref_count": len(refs or []), "policy_refs": refs or []}},
            ],
            "planner_output": {"steps": []},
        }

    def test_night_claim_contains_article_and_time(self):
        from agent.langgraph_agent import _build_verification_targets
        targets = _build_verification_targets(
            self._state(night=True, holiday=True, hr="LEAVE", mcc="5813", m_risk="HIGH", h_risk=True))
        combined = " ".join(targets)
        self.assertTrue("제23조" in combined or "심야" in combined)
        self.assertTrue("22:24" in combined or "심야" in combined)

    def test_holiday_hr_claim_contains_leave_and_article(self):
        from agent.langgraph_agent import _build_verification_targets
        targets = _build_verification_targets(self._state(holiday=True, hr="LEAVE", h_risk=True))
        combined = " ".join(targets)
        self.assertTrue("LEAVE" in combined or "휴가" in combined)
        self.assertTrue("제39조" in combined or "주말" in combined)

    def test_high_mcc_claim_contains_code_and_article(self):
        from agent.langgraph_agent import _build_verification_targets
        targets = _build_verification_targets(self._state(mcc="5813", m_risk="HIGH"))
        combined = " ".join(targets)
        self.assertTrue("5813" in combined or "42조" in combined or "고위험" in combined)

    def test_max_4_targets(self):
        from agent.langgraph_agent import _build_verification_targets
        targets = _build_verification_targets(
            self._state(night=True, holiday=True, hr="LEAVE", mcc="5813",
                        m_risk="HIGH", h_risk=True, amount=600000,
                        refs=[{"article": "제23조", "parent_title": "식대"},
                              {"article": "제39조", "parent_title": "주말공휴일"}]))
        self.assertLessEqual(len(targets), 4)

    def test_no_weak_applicable_pattern(self):
        import re
        from agent.langgraph_agent import _build_verification_targets
        targets = _build_verification_targets(
            self._state(night=True, holiday=True, hr="LEAVE", mcc="5813", m_risk="HIGH"))
        for t in targets:
            self.assertFalse(
                bool(re.match(r"^.{0,40}조항이 해당 사례에 적용될 수 있음\.$", t)),
                f"약한 주장 감지: {t}")

    def test_chunk_supports_requires_article_match(self):
        from services.evidence_verification import _chunk_supports_claim
        claim = "제23조 심야 시간대 식대 위반 가능"
        wrong_art = {"chunk_text": "제38조 심야 시간대 지출 검토 대상이다", "article": "제38조"}
        self.assertFalse(_chunk_supports_claim(claim, wrong_art))

    def test_chunk_supports_requires_3_words(self):
        from services.evidence_verification import _chunk_supports_claim
        claim = "2026-03-07 22:24 심야 시간대 제23조 식대 위반 가능"
        good = {"chunk_text": "제23조 심야 시간대 식대 경고 대상이다", "article": "제23조"}
        bad  = {"chunk_text": "전표 입력 기준을 준수해야 한다", "article": "제17조"}
        self.assertTrue(_chunk_supports_claim(claim, good))
        self.assertFalse(_chunk_supports_claim(claim, bad))
```

---

## 검증 커맨드

```bash
python -m pytest tests/test_graph.py::TestVerificationTargets -v
python -m pytest tests/ -v
```

---

## 변경 전/후 비교

| 항목 | 변경 전 | 변경 후 |
|------|---------|---------|
| 타겟 예시 | "제23조 식대 조항이 해당 사례에 적용될 수 있음." | "2026-03-07 22:24 심야 MCC 5813 → 제23조 ③-1항 해당한다." |
| 조항 명시 수준 | 조(條) 단위만 | 제XX조 ③-1항까지 |
| 전표 사실 포함 | 없음 | 시간·금액·MCC·근태 전부 |
| tool_results 활용 | policy_refs article/title만 | holidayRisk·merchantRisk facts 직접 참조 |
| coverage_ratio | 항상 1.0 (무의미) | 실제 gap 감지 가능 |
| claim별 gap 추적 | 없음 | `ClaimVerificationResult.gap` 필드 |
| 판정 기준 | 단어 2개 겹침 | 조항 번호 일치 + 의미 단어 3개 (강화) |