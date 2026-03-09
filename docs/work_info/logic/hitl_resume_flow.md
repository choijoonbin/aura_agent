# HITL 팝업 → 분석 이어가기 → 스트림 재개 흐름

## 기대 동작
1. 사용자가 HITL 팝업에서 승인 선택 후 "검토 반영 후 분석 이어가기" 클릭
2. 팝업이 닫힘
3. 에이전트 스트림 영역에 재개 분석 로그가 실시간으로 표시됨

## 문제였던 점 (수정 전)

### 1. 팝업이 닫히지 않고 멈춘 것처럼 보이던 이유
- **armed 이중 rerun**: 제출 시 `mt_pending_review_submit`을 넣고 `open_key`를 제거한 뒤 `st.rerun()` 한 번만 하면 되는데, 다음 run에서 "armed"가 없으면 `armed=True`로 저장하고 **한 번 더 `st.rerun()`** 하고 있었음.
- 그 결과, 제출 후 **두 번 연속 rerun**이 일어나고, 그 사이에 팝업이 닫힌 상태가 제대로 보이지 않거나, 두 번째 rerun 직전에 잠깐만 닫혔다가 다시 그려지는 것처럼 보일 수 있었음.
- 팝업을 닫기 위해 `open_key`를 제거만 했는데, 일부 경로에서 다시 True가 되거나, rerun 순서 때문에 **한 run에서는 여전히 다이얼로그를 그리도록** 조건이 맞을 수 있었음.

### 2. "start 생각중"만 보이고 진행이 안 되는 것처럼 보이던 이유
- 제출 후 **두 번째 run**에서야 `review-submit` API를 호출하고 `pending_stream`을 세팅함.
- 그 run에서 스트림 루프(`sse_node_block_generator_with_idle`)에 들어가면, 백엔드는 `asyncio.create_task(_run_analysis_task(...))`로 재개 태스크를 **비동기로만** 올려둔 상태에서 곧바로 200을 반환함.
- 클라이언트가 곧바로 GET `/stream`으로 연결하는데, 이 시점에 이벤트 루프가 재개 태스크를 아직 한 번도 실행하지 않았을 수 있어, **스트림 쪽에서는 이벤트가 잠깐 안 오고** → 1초 후 idle 로직에 의해 "생각 중" 메시지만 반복될 수 있었음.
- 즉, **한 번의 run에서** 팝업 닫기 + API 호출 + 스트림 시작이 이어지지 않고, rerun과 API 호출 타이밍 때문에 체감상 “멈춤”처럼 보였을 수 있음.

## 수정 내용

### 1. UI (workspace.py)

- **armed 이중 rerun 제거**
  - `mt_pending_review_submit`이 있으면 **그 run에서 바로** `review-submit` API를 호출하고, `stream_path`가 오면 `pending_stream` / `mt_resume_stream`을 세팅한 뒤, 같은 run에서 스트림 블록으로 진입하도록 함.
  - 제출 후 rerun은 **한 번만** 발생 (팝업에서 제출 시 한 번).

- **팝업이 반드시 닫히도록**
  - 제출 시: `open_key`를 제거(`pop`)한 뒤, **같은 run_id에 대해 `open_key = False`를 한 번 더 설정**해, rerun 후에도 다이얼로그가 다시 열리지 않도록 함.
  - `mt_pending_review_submit`을 처리하는 블록 진입 시, 해당 `run_id`에 대해 **`open_key`를 False로 설정**해, 그 run에서는 다이얼로그를 그리지 않도록 함.

- **payload 단순화**
  - `mt_pending_review_submit`에서 `armed` 필드를 제거 (이중 rerun이 없으므로 불필요).

### 2. 백엔드 (main.py)

- **재개 태스크가 빨리 시작되도록**
  - `review_submit`에서 `asyncio.create_task(_run_analysis_task(...))` 호출 직후 **`await asyncio.sleep(0)`** 한 번 호출해, 이벤트 루프에 제어를 넘겨 재개 태스크가 그 턴에 최대한 빨리 실행되도록 함.  
  - 이렇게 하면 클라이언트가 GET `/stream`에 연결했을 때, 이미 재개 태스크가 큐에 이벤트를 넣기 시작했을 가능성이 높아짐.

## 수정 후 흐름 요약

1. 사용자가 팝업에서 "검토 반영 후 분석 이어가기" 클릭  
   → `mt_pending_review_submit` 설정, `open_key` 제거 후 `open_key=False` 설정, `st.rerun()` **1회**
2. 다음 run  
   → `mt_pending_review_submit` 존재 → 해당 run_id에 대해 `open_key=False` 유지  
   → **같은 run에서** `POST /api/v1/analysis-runs/{run_id}/review-submit` 호출  
   → 백엔드에서 `create_task` 후 `await asyncio.sleep(0)`  
   → 200과 `stream_path` 반환  
   → UI에서 `pending_stream` 설정, `mt_pending_review_submit` 제거  
   → `open_key`가 False이므로 **다이얼로그 미표시** (팝업 닫힌 상태 유지)  
   → 스트림 영역으로 진입, `pending_stream`으로 GET `/stream` 연결  
   → 재개 태스크가 이미/곧 이벤트를 퍼블리시 → 스트림에 재개 분석 로그 표시

## 관련 코드 위치

| 구분 | 파일 | 내용 |
|------|------|------|
| 팝업 제출 | ui/workspace.py | `render_hitl_panel` 내 submit 클릭 시 `mt_pending_review_submit` 설정, `open_key` 제거 및 False 설정 후 `st.rerun()` |
| 제출 처리 | ui/workspace.py | `mt_pending_review_submit` 처리 시 armed 제거, 즉시 `review-submit` 호출, `open_key=False` 설정 |
| 다이얼로그 표시 | ui/workspace.py | `if st.session_state.get(open_key):` 후 `render_hitl_dialog` 호출 (open_key가 False면 호출 안 함) |
| 재개 API | main.py | `review_submit`: `create_task(_run_analysis_task(...))` 후 `await asyncio.sleep(0)` |
| 스트림 소비 | ui/workspace.py | `pending_stream`이 있을 때 `sse_node_block_generator_with_idle(stream_url, ...)` 루프 |
