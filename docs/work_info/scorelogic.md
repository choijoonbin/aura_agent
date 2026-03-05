# Cursor 작업 프롬프트 — 점수 산출 엔진 고도화

## 배경 및 목적

### 현재 문제

`agent/langgraph_agent.py`의 `_score()` 함수는 아래와 같이 **완전히 하드코딩된 규칙 기반**으로만 동작한다:

```python
def _score(flags, tool_results):
    policy_score = 0
    evidence_score = 30          # 기본 30점 무조건 부여
    if flags.get("isHoliday"):
        policy_score += 35       # 휴일이면 무조건 +35
    if flags.get("hrStatus") in {"LEAVE", "OFF", "VACATION"}:
        policy_score += 20       # 근태 충돌이면 무조건 +20
    if flags.get("isNight"):
        policy_score += 10
    if flags.get("budgetExceeded"):
        policy_score += 10
    # 도구 결과에서 추출하는 것: 라인아이템 유무(0/1), 규정 조항 유무(0/1)만 체크
    ...
    final_score = policy_score * 0.6 + evidence_score * 0.4
```

### 무엇이 잘못됐는가

1. **도구 실행 결과가 점수에 실질적으로 반영되지 않는다**
   - `holiday_compliance_probe`가 `holidayRisk=HIGH`를 반환해도 점수 계산에 사용 안 함
   - `merchant_risk_probe`가 `merchantRisk=HIGH`를 반환해도 무시
   - `policy_rulebook_probe`가 규정 조항 5건을 찾아도 "1건 이상 존재"(0/1) 여부만 체크

2. **금액 규모가 점수에 전혀 반영되지 않는다**
   - 14만원 거래와 500만원 거래가 동일한 가중치를 받는다
   - 규정집 제11조에 금액 구간별 승인 기준이 명시돼 있으나 무시됨

3. **복합 위험 시너지가 없다**
   - 휴일+심야+고위험업종이 동시에 해당해도 단순 덧셈만 적용
   - 실제 위험은 복합 조건일 때 기하급수적으로 높아지는 특성이 있음

4. **최종 점수 가중치가 60:40으로 고정돼 있다**
   - 증거 품질과 무관하게 항상 policy_score 60%, evidence_score 40%
   - 도구 결과의 신뢰도에 따라 가중치가 달라져야 한다

### 목표

**5단계 구조**로 `_score()` 함수를 재설계한다:

```
Step 1: 기본 신호 점수 (기존 로직 유지, 경미한 조정)
Step 2: 도구 결과 심화 반영 (신규)
Step 3: 복합 위험 승수 적용 (신규)
Step 4: 금액 구간 가중 (신규)
Step 5: 증거 품질 기반 가중치 동적 조정 (신규)
```

---

## 작업 범위 (수정 파일 목록)

| 파일 | 작업 내용 |
|------|----------|
| `agent/langgraph_agent.py` | ① `_score()` 함수 전면 재설계, ② `ScoreBreakdown` 반환 구조 확장 |
| `agent/output_models.py` | ③ `ScoreBreakdown` Pydantic 모델 신규 추가 |
| `utils/config.py` | ④ 점수 가중치 환경변수 설정 추가 |
| `tests/test_graph.py` | ⑤ 점수 산출 단위 테스트 추가 |

---

## 상세 구현 명세

---

### ① `output_models.py` — `ScoreBreakdown` 모델 신규 추가

**파일:** `agent/output_models.py`
**위치:** 파일 하단 (기존 모델들 아래)

```python
# ----- Score Breakdown -----

class ScoreSignalDetail(BaseModel):
    """개별 점수 신호 항목."""
    signal: str = Field(description="신호 식별자 (예: 'isHoliday', 'merchantRisk_HIGH')")
    label: str = Field(description="사용자 표시용 한글 레이블")
    raw_value: Any = Field(default=None, description="원본 값")
    points: float = Field(description="이 신호로 부여된 점수")
    category: str = Field(description="'policy' | 'evidence' | 'multiplier' | 'amount'")


class ScoreBreakdown(BaseModel):
    """점수 산출 전체 분해 결과."""
    policy_score: float = Field(description="정책 위반 점수 (0~100)")
    evidence_score: float = Field(description="증거 품질 점수 (0~100)")
    amount_weight: float = Field(default=1.0, description="금액 구간 가중 승수 (1.0~1.3)")
    compound_multiplier: float = Field(default=1.0, description="복합 위험 승수 (1.0~1.5)")
    policy_weight: float = Field(default=0.6, description="최종 점수 산출 시 policy_score 가중치")
    evidence_weight: float = Field(default=0.4, description="최종 점수 산출 시 evidence_score 가중치")
    final_score: float = Field(description="최종 점수 (0~100)")
    severity: str = Field(description="'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'")
    signals: list[ScoreSignalDetail] = Field(default_factory=list, description="점수를 구성하는 신호 목록")
    reasons: list[str] = Field(default_factory=list, description="기존 호환용 이유 목록")
    calculation_trace: str = Field(default="", description="최종 점수 계산식 문자열 (디버깅용)")
```

---

### ② `utils/config.py` — 점수 가중치 환경변수 추가

**파일:** `utils/config.py`
**위치:** `@dataclass(frozen=True) class Settings:` 내부, 기존 필드 하단에 추가

```python
@dataclass(frozen=True)
class Settings:
    # ... 기존 필드 유지 ...

    # ── 점수 산출 가중치 (환경변수로 조정 가능) ──────────────────────────
    # policy_score와 evidence_score의 최종 합산 비율 (합계 1.0)
    score_policy_weight: float = float(os.getenv("SCORE_POLICY_WEIGHT", "0.6"))
    score_evidence_weight: float = float(os.getenv("SCORE_EVIDENCE_WEIGHT", "0.4"))

    # 복합 위험 승수 상한 (이 값을 초과하지 않음)
    score_compound_multiplier_max: float = float(os.getenv("SCORE_COMPOUND_MULTIPLIER_MAX", "1.5"))

    # 금액 구간 승수 상한
    score_amount_multiplier_max: float = float(os.getenv("SCORE_AMOUNT_MULTIPLIER_MAX", "1.3"))
```

---

### ③ `agent/langgraph_agent.py` — `_score()` 함수 전면 재설계

**파일:** `agent/langgraph_agent.py`
**위치:** 기존 `def _score(flags, tool_results)` 함수를 아래 코드로 **완전 교체**한다.

기존 함수 시그니처와 반환 키 이름(`policy_score`, `evidence_score`, `final_score`, `reasons`)은
**반드시 하위 호환성 유지**. 추가 키만 덧붙인다.

```python
# ─────────────────────────────────────────────────────────────────────────────
# 점수 산출 상수 — settings로 이전 가능, 현재는 모듈 상수로 관리
# ─────────────────────────────────────────────────────────────────────────────

# [Step 1] 기본 신호 점수표
_POLICY_SIGNAL_POINTS: dict[str, float] = {
    "isHoliday":         35.0,   # 휴일/주말 사용
    "hrStatus_conflict": 20.0,   # 근태 충돌 (LEAVE/OFF/VACATION)
    "isNight":           10.0,   # 심야 시간대 (22시~06시)
    "budgetExceeded":    15.0,   # 예산 초과 (기존 10 → 15로 상향, 규정 제40조 근거)
}

# [Step 2] 도구 결과 심화 점수표
# holiday_compliance_probe 결과 반영
_HOLIDAY_RISK_POLICY_DELTA: dict[str, float] = {
    "HIGH":    10.0,   # 휴일+근태 중복 확인 시 추가
    "MEDIUM":   5.0,
    "LOW":      0.0,
}

# merchant_risk_probe 결과 반영 (규정 제23조, 제41조 근거)
_MERCHANT_RISK_POLICY_DELTA: dict[str, float] = {
    "HIGH":   20.0,   # MCC 5813(주류), 7992(골프) 등
    "MEDIUM": 10.0,   # 일반 식음료
    "LOW":     3.0,
    "UNKNOWN": 0.0,
}

# policy_rulebook_probe 규정 조항 수에 따른 evidence 점수
# ref_count가 많을수록 규정 위반 근거가 충분히 확보된 것
_POLICY_REF_EVIDENCE_POINTS: list[tuple[int, float]] = [
    (5, 30.0),   # 5건 이상: 최대 30점
    (3, 22.0),   # 3~4건
    (2, 15.0),   # 2건
    (1, 10.0),   # 1건
    (0,  0.0),   # 0건
]

# document_evidence_probe 라인아이템 수에 따른 evidence 점수
_LINE_ITEM_EVIDENCE_POINTS: list[tuple[int, float]] = [
    (3, 20.0),   # 3건 이상: 최대 20점
    (2, 15.0),
    (1, 10.0),
    (0,  0.0),
]


def _lookup_tiered(value: int, table: list[tuple[int, float]]) -> float:
    """tiered 점수 테이블에서 value에 해당하는 점수를 반환. 내림차순 기준."""
    for threshold, points in table:
        if value >= threshold:
            return points
    return 0.0


def _score(flags: dict[str, Any], tool_results: list[dict[str, Any]]) -> dict[str, Any]:
    """
    5단계 점수 산출 엔진.

    Step 1: 기본 신호 점수 (flags 기반)
    Step 2: 도구 결과 심화 반영 (tool_results 기반, 실제 도구 출력값 사용)
    Step 3: 복합 위험 승수 (2개 이상 고위험 신호 동시 발생 시 승수 적용)
    Step 4: 금액 구간 가중 (규정집 제11조 승인권한 구간 근거)
    Step 5: 증거 품질 기반 가중치 동적 조정
    """
    from agent.output_models import ScoreSignalDetail

    signals: list[ScoreSignalDetail] = []
    reasons: list[str] = []

    # ─────────────────────────────────────────────────────────────────────────
    # Step 1: 기본 신호 점수 (flags 기반)
    # ─────────────────────────────────────────────────────────────────────────
    base_policy_score: float = 0.0
    base_evidence_score: float = 20.0   # 전표 존재 자체의 기본 증거 점수 (기존 30 → 20으로 조정, 도구 결과 반영분 확보)

    if flags.get("isHoliday"):
        pts = _POLICY_SIGNAL_POINTS["isHoliday"]
        base_policy_score += pts
        reasons.append("휴일 사용 정황")
        signals.append(ScoreSignalDetail(
            signal="isHoliday", label="휴일/주말 사용",
            raw_value=True, points=pts, category="policy"
        ))

    hr = str(flags.get("hrStatus") or "").upper()
    if hr in {"LEAVE", "OFF", "VACATION"}:
        pts = _POLICY_SIGNAL_POINTS["hrStatus_conflict"]
        base_policy_score += pts
        reasons.append(f"근태 상태 충돌 ({hr})")
        signals.append(ScoreSignalDetail(
            signal="hrStatus_conflict", label=f"근태 충돌({hr})",
            raw_value=hr, points=pts, category="policy"
        ))

    if flags.get("isNight"):
        pts = _POLICY_SIGNAL_POINTS["isNight"]
        base_policy_score += pts
        reasons.append("심야 시간대")
        signals.append(ScoreSignalDetail(
            signal="isNight", label="심야 시간대(22시~06시)",
            raw_value=True, points=pts, category="policy"
        ))

    if flags.get("budgetExceeded"):
        pts = _POLICY_SIGNAL_POINTS["budgetExceeded"]
        base_policy_score += pts
        reasons.append("예산 초과")
        signals.append(ScoreSignalDetail(
            signal="budgetExceeded", label="예산 한도 초과",
            raw_value=True, points=pts, category="policy"
        ))

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: 도구 결과 심화 반영
    # ─────────────────────────────────────────────────────────────────────────
    tool_policy_delta: float = 0.0
    tool_evidence_delta: float = 0.0

    # 2-A: holiday_compliance_probe 결과 반영
    holiday_result = _find_tool_result(tool_results, "holiday_compliance_probe")
    if holiday_result:
        h_facts = holiday_result.get("facts") or {}
        holiday_risk = h_facts.get("holidayRisk")
        # holidayRisk가 bool로 반환되므로 HIGH/MEDIUM 분기 처리
        if holiday_risk is True and flags.get("isHoliday") and hr in {"LEAVE", "OFF", "VACATION"}:
            # 휴일 + 근태 충돌 동시 확인: HIGH
            delta = _HOLIDAY_RISK_POLICY_DELTA["HIGH"]
            tool_policy_delta += delta
            reasons.append("도구 확인: 휴일+근태 중복 위험(HIGH)")
            signals.append(ScoreSignalDetail(
                signal="holidayRisk_HIGH", label="도구 확인 - 휴일+근태 중복",
                raw_value="HIGH", points=delta, category="policy"
            ))
        elif holiday_risk is True:
            delta = _HOLIDAY_RISK_POLICY_DELTA["MEDIUM"]
            tool_policy_delta += delta
            reasons.append("도구 확인: 휴일 위험(MEDIUM)")
            signals.append(ScoreSignalDetail(
                signal="holidayRisk_MEDIUM", label="도구 확인 - 휴일 위험",
                raw_value="MEDIUM", points=delta, category="policy"
            ))

    # 2-B: merchant_risk_probe 결과 반영
    merchant_result = _find_tool_result(tool_results, "merchant_risk_probe")
    if merchant_result:
        m_facts = merchant_result.get("facts") or {}
        merchant_risk = str(m_facts.get("merchantRisk") or "UNKNOWN").upper()
        delta = _MERCHANT_RISK_POLICY_DELTA.get(merchant_risk, 0.0)
        if delta > 0:
            tool_policy_delta += delta
            reasons.append(f"도구 확인: 가맹점 위험도 {merchant_risk}")
            signals.append(ScoreSignalDetail(
                signal=f"merchantRisk_{merchant_risk}",
                label=f"가맹점/업종 위험도({merchant_risk})",
                raw_value=merchant_risk, points=delta, category="policy"
            ))

    # 2-C: policy_rulebook_probe 규정 조항 수 반영 (evidence)
    policy_result = _find_tool_result(tool_results, "policy_rulebook_probe")
    if policy_result:
        p_facts = policy_result.get("facts") or {}
        ref_count = int(p_facts.get("ref_count") or 0)
        delta = _lookup_tiered(ref_count, _POLICY_REF_EVIDENCE_POINTS)
        if delta > 0:
            tool_evidence_delta += delta
            reasons.append(f"규정 조항 {ref_count}건 확보")
            signals.append(ScoreSignalDetail(
                signal="policyRefs", label=f"규정 조항 {ref_count}건",
                raw_value=ref_count, points=delta, category="evidence"
            ))

    # 2-D: document_evidence_probe 라인아이템 수 반영 (evidence)
    doc_result = _find_tool_result(tool_results, "document_evidence_probe")
    if doc_result:
        d_facts = doc_result.get("facts") or {}
        line_count = int(d_facts.get("lineItemCount") or 0)
        delta = _lookup_tiered(line_count, _LINE_ITEM_EVIDENCE_POINTS)
        if delta > 0:
            tool_evidence_delta += delta
            reasons.append(f"전표 라인아이템 {line_count}건 확보")
            signals.append(ScoreSignalDetail(
                signal="lineItems", label=f"전표 라인 {line_count}건",
                raw_value=line_count, points=delta, category="evidence"
            ))

    # 2-E: legacy_aura_deep_audit 결과 반영 (evidence)
    if any(r.get("skill") == "legacy_aura_deep_audit" and r.get("facts") for r in tool_results):
        tool_evidence_delta += 15.0
        reasons.append("심층 감사 결과 확보")
        signals.append(ScoreSignalDetail(
            signal="legacyAudit", label="심층 감사 결과",
            raw_value=True, points=15.0, category="evidence"
        ))

    # 2-F: HITL 응답 반영
    if flags.get("hasHitlResponse"):
        tool_evidence_delta += 10.0
        reasons.append("사람 검토 응답 확보")
        signals.append(ScoreSignalDetail(
            signal="hitlResponse", label="사람 검토 응답",
            raw_value=True, points=10.0, category="evidence"
        ))

    # 중간 합산
    policy_score = min(100.0, base_policy_score + tool_policy_delta)
    evidence_score = min(100.0, base_evidence_score + tool_evidence_delta)

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3: 복합 위험 승수
    # 2개 이상의 고위험 신호가 동시에 발생하면 실질 위험은 단순 합산보다 크다.
    # 규정집 제23조(식대 검토 대상), 제39조(주말/공휴일 제약), 제41조(고위험 거래처) 근거.
    # ─────────────────────────────────────────────────────────────────────────
    high_risk_count = sum([
        bool(flags.get("isHoliday")),
        bool(hr in {"LEAVE", "OFF", "VACATION"}),
        bool(flags.get("isNight")),
        bool(flags.get("budgetExceeded")),
        bool(merchant_result and str((merchant_result.get("facts") or {}).get("merchantRisk") or "").upper() == "HIGH"),
    ])

    compound_multiplier = 1.0
    if high_risk_count >= 4:
        compound_multiplier = settings.score_compound_multiplier_max          # 최대값 (기본 1.5)
        reasons.append(f"복합 위험 승수 적용 ({high_risk_count}개 고위험 신호)")
        signals.append(ScoreSignalDetail(
            signal="compound_multiplier", label=f"복합 위험({high_risk_count}개)",
            raw_value=high_risk_count, points=0.0,
            category="multiplier"
        ))
    elif high_risk_count == 3:
        compound_multiplier = 1.3
        reasons.append(f"복합 위험 승수 적용 ({high_risk_count}개 고위험 신호)")
        signals.append(ScoreSignalDetail(
            signal="compound_multiplier", label=f"복합 위험({high_risk_count}개)",
            raw_value=high_risk_count, points=0.0,
            category="multiplier"
        ))
    elif high_risk_count == 2:
        compound_multiplier = 1.15
        reasons.append(f"복합 위험 승수 적용 ({high_risk_count}개 고위험 신호)")
        signals.append(ScoreSignalDetail(
            signal="compound_multiplier", label=f"복합 위험({high_risk_count}개)",
            raw_value=high_risk_count, points=0.0,
            category="multiplier"
        ))

    policy_score = min(100.0, policy_score * compound_multiplier)

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4: 금액 구간 가중
    # 규정집 제11조 승인권한 구간 (10만/50만/200만원) 기준.
    # 금액이 클수록 위험 노출 규모가 크므로 policy_score에 승수 적용.
    # ─────────────────────────────────────────────────────────────────────────
    amount = float(flags.get("amount") or 0)
    amount_multiplier = 1.0

    if amount >= 2_000_000:       # 200만원 초과: 임원/CFO 승인 구간
        amount_multiplier = min(settings.score_amount_multiplier_max, 1.3)
        reasons.append("금액 200만원 초과 (임원급 승인 구간)")
        signals.append(ScoreSignalDetail(
            signal="amount_tier_4", label=f"금액 구간 200만원 초과({int(amount):,}원)",
            raw_value=amount, points=0.0, category="amount"
        ))
    elif amount >= 500_000:       # 50만원 초과: 본부장 승인 구간
        amount_multiplier = 1.15
        reasons.append("금액 50만원 초과 (본부장 승인 구간)")
        signals.append(ScoreSignalDetail(
            signal="amount_tier_3", label=f"금액 구간 50만원 초과({int(amount):,}원)",
            raw_value=amount, points=0.0, category="amount"
        ))
    elif amount >= 100_000:       # 10만원 초과: 부서장 승인 구간
        amount_multiplier = 1.07
        reasons.append("금액 10만원 초과 (부서장 승인 구간)")
        signals.append(ScoreSignalDetail(
            signal="amount_tier_2", label=f"금액 구간 10만원 초과({int(amount):,}원)",
            raw_value=amount, points=0.0, category="amount"
        ))
    # 10만원 이하: 팀장 승인 구간, 기본값 1.0 유지

    policy_score = min(100.0, policy_score * amount_multiplier)

    # ─────────────────────────────────────────────────────────────────────────
    # Step 5: 증거 품질 기반 가중치 동적 조정
    # 도구가 충분한 증거를 확보했다면 evidence_score의 반영 비중을 높인다.
    # 증거가 부족하면 policy_score 비중을 높여 보수적 판정을 유지한다.
    # ─────────────────────────────────────────────────────────────────────────
    # 증거 충족 여부 판단
    has_strong_evidence = (
        bool(policy_result and int((policy_result.get("facts") or {}).get("ref_count") or 0) >= 3)
        and bool(doc_result and int((doc_result.get("facts") or {}).get("lineItemCount") or 0) >= 1)
    )

    if has_strong_evidence:
        # 증거 충분: evidence_score 반영 비중 상향 (50:50)
        policy_weight = 0.5
        evidence_weight = 0.5
    else:
        # 증거 부족: policy 중심 보수적 판정 (70:30)
        policy_weight = settings.score_policy_weight     # 기본 0.6 → 증거 부족 시 0.7 적용
        evidence_weight = settings.score_evidence_weight  # 기본 0.4 → 증거 부족 시 0.3 적용
        if not has_strong_evidence and (policy_result is None or doc_result is None):
            policy_weight = 0.7
            evidence_weight = 0.3

    # ─────────────────────────────────────────────────────────────────────────
    # 최종 점수 산출
    # ─────────────────────────────────────────────────────────────────────────
    final_score_raw = policy_score * policy_weight + evidence_score * evidence_weight
    final_score = min(100.0, round(final_score_raw, 1))

    # Severity 분류 (기존 finalizer_node 로직과 동기화)
    if final_score >= 75:
        severity = "CRITICAL"
    elif final_score >= 55:
        severity = "HIGH"
    elif final_score >= 35:
        severity = "MEDIUM"
    else:
        severity = "LOW"

    # 계산식 trace 문자열 (디버깅용)
    calculation_trace = (
        f"policy({policy_score:.1f}) × {policy_weight} + "
        f"evidence({evidence_score:.1f}) × {evidence_weight} = {final_score:.1f} "
        f"[compound×{compound_multiplier:.2f}, amount×{amount_multiplier:.2f}]"
    )

    return {
        # ── 하위 호환 키 (기존 코드가 이 키를 직접 참조하므로 유지 필수) ──
        "policy_score": int(round(policy_score)),
        "evidence_score": int(round(evidence_score)),
        "final_score": int(round(final_score)),
        "reasons": reasons,
        # ── 신규 확장 키 ──────────────────────────────────────────────────
        "amount_weight": amount_multiplier,
        "compound_multiplier": compound_multiplier,
        "policy_weight": policy_weight,
        "evidence_weight": evidence_weight,
        "severity": severity,
        "signals": [s.model_dump() for s in signals],
        "calculation_trace": calculation_trace,
    }
```

---

### ④ `execute_node` — SCORE_BREAKDOWN 이벤트 메시지 개선

**파일:** `agent/langgraph_agent.py`
**위치:** `execute_node` 함수 내부, 기존 `SCORE_BREAKDOWN` 이벤트 추가 부분

기존:
```python
    pending_events.append({
        "event_type": "SCORE_BREAKDOWN",
        "message": f"정책점수 {score['policy_score']}, 근거점수 {score['evidence_score']}, 최종점수 {score['final_score']}",
        ...
    })
```

변경 후:
```python
    trace = score.get("calculation_trace", "")
    pending_events.append({
        "event_type": "SCORE_BREAKDOWN",
        "message": (
            f"정책점수 {score['policy_score']}점 / 근거점수 {score['evidence_score']}점 / "
            f"최종 {score['final_score']}점 [{score.get('severity', '-')}] "
            f"— {trace}"
        ),
        "node": "executor",
        "phase": "execute",
        "metadata": score,
    })
```

---

### ⑤ `tests/test_graph.py` — 점수 산출 단위 테스트 추가

**파일:** `tests/test_graph.py`
**위치:** 기존 `TestAgentGraph` 클래스 하단에 아래 테스트 클래스를 **별도 클래스**로 추가

```python
class TestScoreEngine(unittest.TestCase):
    """_score() 함수 5단계 점수 산출 검증."""

    def _make_tool_results(
        self,
        merchant_risk="MEDIUM",
        ref_count=2,
        line_count=1,
        holiday_risk=False,
    ):
        return [
            {
                "skill": "holiday_compliance_probe",
                "ok": True,
                "facts": {"holidayRisk": holiday_risk, "isHoliday": holiday_risk},
            },
            {
                "skill": "merchant_risk_probe",
                "ok": True,
                "facts": {"merchantRisk": merchant_risk, "mccCode": "5813"},
            },
            {
                "skill": "policy_rulebook_probe",
                "ok": True,
                "facts": {"ref_count": ref_count, "policy_refs": [{}] * ref_count},
            },
            {
                "skill": "document_evidence_probe",
                "ok": True,
                "facts": {"lineItemCount": line_count},
            },
        ]

    def test_holiday_only_base_score(self):
        """휴일만 있는 경우 policy_score >= 35."""
        from agent.langgraph_agent import _score
        flags = {"isHoliday": True, "hrStatus": "WORKING", "isNight": False,
                 "budgetExceeded": False, "amount": 50000, "hasHitlResponse": False}
        result = _score(flags, [])
        self.assertGreaterEqual(result["policy_score"], 35)

    def test_high_merchant_risk_adds_policy_score(self):
        """merchant_risk=HIGH일 때 policy_score가 MEDIUM보다 높아야 한다."""
        from agent.langgraph_agent import _score
        flags = {"isHoliday": False, "hrStatus": "WORKING", "isNight": False,
                 "budgetExceeded": False, "amount": 100000, "hasHitlResponse": False}
        result_high = _score(flags, self._make_tool_results(merchant_risk="HIGH"))
        result_medium = _score(flags, self._make_tool_results(merchant_risk="MEDIUM"))
        self.assertGreater(result_high["policy_score"], result_medium["policy_score"])

    def test_more_policy_refs_higher_evidence_score(self):
        """규정 조항 5건 > 1건일 때 evidence_score가 더 높아야 한다."""
        from agent.langgraph_agent import _score
        flags = {"isHoliday": False, "hrStatus": "WORKING", "isNight": False,
                 "budgetExceeded": False, "amount": 100000, "hasHitlResponse": False}
        result_5 = _score(flags, self._make_tool_results(ref_count=5))
        result_1 = _score(flags, self._make_tool_results(ref_count=1))
        self.assertGreater(result_5["evidence_score"], result_1["evidence_score"])

    def test_compound_multiplier_applied_for_multiple_risks(self):
        """4개 이상 고위험 신호 시 compound_multiplier > 1.0이어야 한다."""
        from agent.langgraph_agent import _score
        flags = {
            "isHoliday": True, "hrStatus": "LEAVE", "isNight": True,
            "budgetExceeded": True, "amount": 300000, "hasHitlResponse": False,
        }
        result = _score(flags, self._make_tool_results(merchant_risk="HIGH", holiday_risk=True))
        self.assertGreater(result.get("compound_multiplier", 1.0), 1.0)

    def test_large_amount_increases_final_score(self):
        """동일 조건에서 금액이 크면 final_score가 더 높아야 한다."""
        from agent.langgraph_agent import _score
        base_flags = {"isHoliday": True, "hrStatus": "LEAVE", "isNight": False,
                      "budgetExceeded": False, "hasHitlResponse": False}
        tool_results = self._make_tool_results()
        result_large = _score({**base_flags, "amount": 3_000_000}, tool_results)
        result_small = _score({**base_flags, "amount": 30_000}, tool_results)
        self.assertGreater(result_large["final_score"], result_small["final_score"])

    def test_final_score_max_100(self):
        """최종 점수는 100을 초과해서는 안 된다."""
        from agent.langgraph_agent import _score
        flags = {
            "isHoliday": True, "hrStatus": "LEAVE", "isNight": True,
            "budgetExceeded": True, "amount": 10_000_000, "hasHitlResponse": False,
        }
        result = _score(flags, self._make_tool_results(
            merchant_risk="HIGH", ref_count=5, line_count=5, holiday_risk=True
        ))
        self.assertLessEqual(result["final_score"], 100)

    def test_backward_compat_keys_present(self):
        """기존 코드 호환을 위한 키가 모두 존재해야 한다."""
        from agent.langgraph_agent import _score
        flags = {"isHoliday": False, "hrStatus": "WORKING", "isNight": False,
                 "budgetExceeded": False, "amount": 0, "hasHitlResponse": False}
        result = _score(flags, [])
        for key in ("policy_score", "evidence_score", "final_score", "reasons"):
            self.assertIn(key, result, f"필수 키 '{key}' 누락")

    def test_signals_list_populated(self):
        """신호가 있을 때 signals 목록이 채워져야 한다."""
        from agent.langgraph_agent import _score
        flags = {"isHoliday": True, "hrStatus": "LEAVE", "isNight": True,
                 "budgetExceeded": False, "amount": 200000, "hasHitlResponse": False}
        result = _score(flags, self._make_tool_results(merchant_risk="HIGH"))
        self.assertGreater(len(result.get("signals", [])), 0)

    def test_calculation_trace_present(self):
        """calculation_trace 문자열이 반환되어야 한다."""
        from agent.langgraph_agent import _score
        flags = {"isHoliday": True, "hrStatus": "WORKING", "isNight": False,
                 "budgetExceeded": False, "amount": 100000, "hasHitlResponse": False}
        result = _score(flags, self._make_tool_results())
        self.assertIn("calculation_trace", result)
        self.assertIn("policy", result["calculation_trace"])
```

---

## 구현 완료 검증 체크리스트

```bash
# 1. 단위 테스트 전체 실행
python -m pytest tests/test_graph.py::TestScoreEngine -v

# 2. 기존 테스트 영향 없음 확인
python -m pytest tests/ -v

# 3. 동작 확인 — 최소 실행 코드
python -c "
from agent.langgraph_agent import _score

# 시나리오: 휴일+근태충돌+심야+고위험업종+200만원
flags = {
    'isHoliday': True, 'hrStatus': 'LEAVE', 'isNight': True,
    'budgetExceeded': False, 'amount': 2_100_000, 'hasHitlResponse': False
}
tool_results = [
    {'skill': 'holiday_compliance_probe', 'ok': True,
     'facts': {'holidayRisk': True, 'isHoliday': True, 'hrStatus': 'LEAVE'}},
    {'skill': 'merchant_risk_probe', 'ok': True,
     'facts': {'merchantRisk': 'HIGH', 'mccCode': '5813'}},
    {'skill': 'policy_rulebook_probe', 'ok': True,
     'facts': {'ref_count': 4, 'policy_refs': [{}, {}, {}, {}]}},
    {'skill': 'document_evidence_probe', 'ok': True,
     'facts': {'lineItemCount': 2}},
]
result = _score(flags, tool_results)
print('policy_score :', result['policy_score'])
print('evidence_score:', result['evidence_score'])
print('final_score   :', result['final_score'])
print('severity      :', result.get('severity'))
print('compound×     :', result.get('compound_multiplier'))
print('amount×       :', result.get('amount_weight'))
print('trace         :', result.get('calculation_trace'))
print('--- signals ---')
for s in result.get('signals', []):
    print(f\"  {s['signal']:30s} {s['points']:+.1f}  [{s['category']}]\")
"
```

---

## 주의사항 및 사이드이펙트

| 항목 | 내용 |
|------|------|
| **하위 호환성 필수** | `_score()`의 반환값에서 `policy_score`, `evidence_score`, `final_score`, `reasons`는 키 이름과 타입(`int`) 유지. `finalizer_node`, `reporter_node`, `_score_with_hitl_adjustment()` 등이 이 키를 직접 참조함 |
| **`_find_tool_result()` 의존** | Step 2에서 `_find_tool_result()`를 사용함. 이 함수는 파일 상단에 이미 정의되어 있으므로 추가 구현 불필요 |
| **`settings` import** | `_score()` 함수 내에서 `settings.score_compound_multiplier_max` 등을 참조. `from utils.config import settings`가 모듈 상단에 이미 있으므로 추가 import 불필요 |
| **`ScoreSignalDetail` import** | `_score()` 내부에서 `from agent.output_models import ScoreSignalDetail`로 지연 import 처리. 순환 import 방지 목적 |
| **Severity 기준 변경** | 기존 `finalizer_node`의 severity 분류(`HIGH >= 70, MEDIUM >= 40`)와 신규 `_score()` 내 기준(`CRITICAL >= 75, HIGH >= 55, MEDIUM >= 35`)이 다름. **`finalizer_node`의 severity 분류를 `_score()` 반환값의 `severity`로 대체**해야 일관성이 유지됨. 아래 코드 참고 |

**`finalizer_node` severity 동기화 (추가 수정 필요):**

```python
# finalizer_node 내부, 기존 코드 (변경 전):
"severity": "HIGH" if score["final_score"] >= 70 else ("MEDIUM" if score["final_score"] >= 40 else "LOW"),

# 변경 후:
"severity": score.get("severity") or (
    "HIGH" if score["final_score"] >= 70 else ("MEDIUM" if score["final_score"] >= 40 else "LOW")
),
```

---

## 병행 작업 제안 (단위 테스트를 위해 함께 진행 권장)

### A. UI — `SCORE_BREAKDOWN` 이벤트 시각화 (독립 작업, 병행 가능)

`execute_node`가 반환하는 `SCORE_BREAKDOWN` 이벤트에 이제 `signals` 배열이 포함된다.
`ui/workspace.py`의 스트림 표시 부분에서 이를 활용하면 점수 산출 근거를 시각적으로 보여줄 수 있다.

**표시 예시 (스트림 카드):**
```
[SCORE_BREAKDOWN]
정책점수 87점 / 근거점수 65점 / 최종 78점 [CRITICAL]
policy(87.0) × 0.5 + evidence(65.0) × 0.5 = 76.0 [compound×1.30, amount×1.15]

신호 분해:
● 휴일/주말 사용            +35.0  [policy]
● 근태 충돌(LEAVE)          +20.0  [policy]
● 심야 시간대               +10.0  [policy]
● 도구확인-가맹점위험(HIGH)  +20.0  [policy]
● 복합 위험(3개)            ×1.30  [multiplier]
● 규정 조항 4건             +22.0  [evidence]
● 전표 라인 2건             +15.0  [evidence]
```

### B. `_score_with_hitl_adjustment()` 동기화 (필수 병행)

HITL 승인/거부 반영 함수도 `severity` 필드를 재계산하도록 수정이 필요하다.
현재 `_score_with_hitl_adjustment()`는 `policy_score`, `evidence_score`, `final_score`만 조정하고
`severity`를 재계산하지 않는다. HITL 반영 후 최종 점수가 바뀌었는데 severity가 구버전 값으로 남는 버그가 생길 수 있다.