# HITL 팝업: 검토 필요 사유 / 검토자가 답해야 할 질문 로직

## 흐름 요약

1. **hitl.py `build_hitl_request`**  
   검증 결과(covered/total, gate_policy 등)로 `blocking_reasons`, `why_hitl`, `reasons`, `auto_finalize_blockers`를 채움.  
   `review_questions`는 규정 기반(regulation_driven)에서만 채워지며, 대부분 비어 있음.

2. **langgraph_agent verify_node**  
   HITL 필요 시 `_generate_hitl_review_content`로 LLM이 `review_reasons` / `review_questions` 생성.  
   LLM 결과로 `hitl_request["unresolved_claims"]`, `hitl_request["review_questions"]`를 덮어씀.  
   **비어 있으면** `why_hitl` 기반으로 사유/질문을 채우는 fallback이 여러 단계에서 동일하게 적용됨.

3. **ui/workspace.py `_build_hitl_summary_sections`**  
   `hitl_request`에서 `review_reasons`(검토 필요 사유), `stop_reasons`(자동 확정 중단 이유), `questions`(검토자가 답해야 할 질문)를 꺼내서 표시.  
   **질문이 비어 있으면** `"다음 사유가 해소되었는지 검토해 주세요: " + why_hitl` 로 1개 생성.

---

## 왜 세 영역에 같은 문구가 나오는가

- **같은 데이터 소스**  
  `blocking_reasons`, `why_hitl`, `auto_finalize_blockers`가 모두 **covered/total(예: 2/4)** 와 gate 판정에서 나옴.

- **검토 필요 사유 (review_reasons)**  
  - 우선: `hitl_request["unresolved_claims"]` 또는 `hitl_request["reasons"]`  
  - 없으면: `why_hitl` → 스크리닝 `reasonText` → `auto_finalize_blockers`를 `_plain_stop_reason`으로 변환  
  - `_plain_stop_reason`은 "근거 연결률이 2/4 ..." 를 `_format_covered_shortage(2, 4)`로 바꿔  
    **"검증 대상 4개 중 2개만 규정 근거와 연결되어, 2개가 부족해 자동 확정을 보류했습니다. 담당자 검토가 필요합니다."** 로 통일.

- **자동 확정 중단 이유 (stop_reasons)**  
  - `auto_finalize_blockers` 또는 `verification_summary`의 covered/total로 `_format_covered_shortage(2, 4)` 호출  
  → 위와 **동일 문장**.

- **검토자가 답해야 할 질문 (questions)**  
  - 우선: `hitl_request["review_questions"]` 또는 `hitl_request["questions"]`  
  - **비어 있을 때 fallback (3곳에서 동일 패턴):**  
    1. **langgraph_agent `_generate_hitl_review_content`** (L2037):  
       `questions = ["다음 사유가 해소되었는지 검토해 주세요: " + why[:180]]`  
    2. **verify_node** (L2296-2300):  
       `review_questions = ["다음 사유가 해소되었는지 검토해 주세요: " + why_hitl[:200]]`  
    3. **ui workspace `_build_hitl_summary_sections`** (L1434-1437):  
       `questions = ["다음 사유가 해소되었는지 검토해 주세요: " + why_hitl[:200]]`  
  - 즉, **질문 = “사유” 앞에 접두어만 붙인 형태**가 되므로, 내용이 거의 같아짐.

정리하면, **한 가지 사실(2/4 부족)** 이  
- 검토 필요 사유 → 자동 확정 중단 이유 → 검토자 질문  
으로 같은 문장/같은 맥락으로 반복되는 구조입니다.

---

## 코드 위치

| 역할 | 파일 | 대략 위치 |
|------|------|-----------|
| HITL payload 생성 (reasons, why_hitl, review_questions 초기값) | `agent/hitl.py` | `build_hitl_request` (L61~199) |
| LLM으로 review_reasons / review_questions 생성 | `agent/langgraph_agent.py` | `_generate_hitl_review_content` (L1950~2041) |
| review_questions 비었을 때 why_hitl 기반 fallback | `agent/langgraph_agent.py` | verify_node (L2288~2318), `_generate_hitl_review_content` (L2036~2037) |
| UI에서 검토 사유/중단 이유/질문 추출 및 fallback | `ui/workspace.py` | `_build_hitl_summary_sections` (L1393~1482) |
| "검증 대상 N개 중 M개만…" 문장 생성 | `ui/workspace.py` | `_format_covered_shortage` (L1313~1321), `_plain_stop_reason` (L1324~1356) |

---

## 개선 방향 (참고)

- **질문 fallback을 “사유 복붙”이 아니라 행동 지향으로:**  
  예: "미연결된 2개 주장에 대해 규정 근거를 추가했는지, 또는 검토 후 통과/보류를 선택했는지 기록해 주세요."
- **LLM 프롬프트 강화:**  
  `review_questions`는 “사유 요약”이 아니라 “검토자가 반드시 답해야 할 구체적 질문/체크리스트”로만 생성하도록 지시.
- **중복 완화:**  
  “자동 확정 중단 이유”는 한 줄 요약만 두고, “검토 필요 사유”는 그 요약 + 세부(주장별 gap 등)로 구분하는 방식 검토.
