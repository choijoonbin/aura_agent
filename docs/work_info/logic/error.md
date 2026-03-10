새로운 테스트 데이터를 만들고 워크스페이스에서 Hitl 확인을 체크하고 분석하기 눌렀습니다. 진행이 계속 되다 마지막에

"🔍 발견 interrupt

[최종] 담당자 검토 입력을 기다립니다.

[최종] 담당자 검토 입력을 기다립니다.

이 내용이 나왔습니다. 하지만 hitl 팝업이 뜨지 않았습니다.
원래 로직은 체크된채로 분석하면 팝업 띄워주기였습니다.


우리는 앞서 hitl 팝업 띄우고 분석 이어서하기 동작에 팝업에서 문제가 있어
팝업을 닫고 기본 화면에서 분석이어서하기 버튼을 클릭하기로 협의를 해서 수정된 상태입니다.
이 수정과 관련되어서 지금 이 문제가 나온건지... 확인해주세요 작업은 일단 하지마세요 원인 파악dl 먼저입니다

---

## [원인 파악] HITL 인터럽트 시 팝업이 뜨지 않는 현상

**증상**: "HITL 확인" 체크 후 분석 실행 → 스트림에 "🔍 발견 interrupt", "[최종] 담당자 검토 입력을 기다립니다." 표시 → **HITL 팝업이 자동으로 뜨지 않음**.

**코드 기준 원인 후보** (구현 변경 없이 파악만):

1. **팝업은 “열기” 버튼으로만 열리도록 되어 있음**  
   - `ui/workspace.py`에서 HITL 다이얼로그는 `st.session_state[open_key]`가 `True`일 때만 렌더된다.  
   - `open_key`가 `True`로 설정되는 곳은 **"HITL 팝업 열기" 버튼 클릭 시**뿐이다.  
   - 스트림에서 `HITL_PAUSE`/`completed`(status=HITL_REQUIRED) 수신 시에는 `dismissed=False`, `shown=True`만 세팅하고 **`open`은 세팅하지 않는다.**  
   - 따라서 **인터럽트 직후에는 팝업이 자동으로 열리지 않고**, 배너 + "HITL 팝업 열기" 버튼이 보이는 상태가 된다.  
   - 예전에 “팝업 닫고 → 기본 화면에서 분석 이어서하기”로 협의한 수정과는 별개로, **“인터럽트 시 최초 1회 팝업 자동 오픈”을 하려는 로직이 현재 코드에는 없다.**

2. **배너/버튼 자체가 안 보이는 경우**  
   - 배너와 "HITL 팝업 열기"는 `_need_review_ui or (hitl_ui_enabled and _need_hitl)`일 때만 나온다.  
   - `_need_hitl` = `_has_pending_hitl(latest_bundle)` → `latest_bundle.result.result.status`가 `HITL_REQUIRED` 등이거나, `hitl_request` 있고 `hitl_response` 없을 때 `True`.  
   - 스트림 종료 후 `fetch_case_bundle(vkey)`로 `latest_bundle`을 갱신하고 `mt_post_stream_bundle`에 넣은 뒤 `st.rerun()` 한다.  
   - 이때 **백엔드가 인터럽트 직후 run 상태를 DB/API에 아직 반영하지 않았다면** `GET /analysis/latest`가 예전 상태를 줄 수 있고, `_has_pending_hitl`이 `False`가 되어 배너·버튼이 아예 안 나올 수 있다.  
   - 또는 **다른 voucher가 선택된 상태로 rerun**되면 `post_stream.voucher_key != selected_key`라서 `mt_post_stream_bundle`을 쓰지 않고, `fetch_case_bundle(selected_key)`만 쓰게 되며, 선택된 케이스가 방금 분석한 run이 아니면 HITL UI가 안 나올 수 있다.

3. **정리**  
   - “인터럽트는 보이는데 팝업이 안 뜬다” → **의도된 동작**: 현재는 “HITL 팝업 열기”를 눌러야 팝업이 열린다.  
   - “인터럽트 메시지 후에 배너·버튼 자체가 없다” → **원인 후보**: (a) 스트림 종료 직후 `latest_bundle`에 `HITL_REQUIRED`/`hitl_request`가 아직 반영되지 않음, (b) rerun 시 선택 케이스/run_id 불일치로 HITL UI 분기가 타지 않음.  
   - 협의했던 “팝업 닫고 → 분석 이어서하기” 수정은 **제출 후** 흐름만 바꾼 것이고, **인터럽트 직후 최초에 팝업을 자동으로 띄울지 여부**는 별도 정책이다.

**적용된 동작 정책 (두 가지 방식)**  
1. **HITL 확인 체크하고 분석**: 인터럽트 발생 시 `open_key=True`를 세팅해 HITL 팝업을 자동으로 띄움. 사용자가 팝업에서 검토 입력 후 제출하거나 팝업을 닫고 기본 화면에서 "분석 이어가기"를 누르면 인터럽트가 풀리며 같은 run으로 재개됨.
2. **HITL 확인 체크 안 하고 분석**: 인터럽트 없이 분석이 끝까지 완료됨(기존과 동일). 이후 필요 시 "HITL 팝업 열기"로 팝업을 열어 기존과 동일하게 조회/동작 가능.

---

## 추론(Reasoning) LLM 미동작 시 로그로 원인 파악

스트림에 "추론은 LLM으로 생성되지 않았습니다"가 나오거나, 초안 문구만 나올 때 아래 로그 메시지로 원인을 확인할 수 있다.

| 로그 메시지(키워드) | 의미 | 조치 |
|---------------------|------|------|
| `reasoning_llm_skipped` `reason=enable_reasoning_live_llm_false` | 설정으로 LLM 비활성 | `ENABLE_REASONING_LIVE_LLM=true` 설정 |
| `reasoning_llm_skipped` `reason=openai_api_key_missing` | API 키 없음 | `OPENAI_API_KEY` 또는 Azure 설정 확인 |
| `reasoning_llm_failed` `error=...` `error_type=...` | LLM 호출 예외(길이 초과, 타임아웃, 인증 등) | `error`/`error_type` 내용으로 원인 확인, 스택은 exc_info=True 로 출력됨 |
| `reasoning_llm_json_parse_failed` | 스트림 응답 JSON 파싱 실패 | `raw_len` 등으로 잘림/형식 오류 여부 확인 |
| `reasoning_llm_empty_reasoning` | 응답은 왔으나 `reasoning` 필드 없음/빈 문자열 | 모델 출력 형식 또는 max_tokens 부족 여부 확인 |

fallback 시에도 **추론 결과는 반드시 전달**되며, 초안(각 노드에서 만든 요약)을 스트림에 넣고 `note_source=fallback`으로 내려준다.