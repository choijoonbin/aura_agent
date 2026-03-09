
---

#프론트
INFO ui.workspace [HITL_CLOSE] run 시작: mt_pending_review_submit=False mt_review_submit_api_pending=False
INFO ui.workspace [ui] 스트림 구독 시작 vkey=1000-N000000001-2026 run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 url=http://localhost:8010/api/v1/analysis-runs/63c2510f-ba4b-4aff-b481-6c99f4589a90/stream
INFO ui.workspace [RESUME_TRACE] UI SSE 스트림 구독 시작 (분석 이어가기 후 재개 스트림일 수 있음) vkey=1000-N000000001-2026 run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90
INFO ui.workspace [ui] 스트림 for 루프 종료 vkey=1000-N000000001-2026 — fetch_case_bundle 후 mt_post_stream_bundle 저장 및 st.rerun() 예정
INFO ui.workspace [ui] fetch_case_bundle 완료 vkey=1000-N000000001-2026 run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 has_result=True result.status=REVIEW_REQUIRED
INFO ui.workspace [ui] mt_post_stream_bundle 저장 vkey=1000-N000000001-2026 — rerun 시 판단요약 탭이 이 번들로 그려짐
INFO ui.workspace [ui] 스트림 종료 후 st.rerun() 호출 직전 vkey=1000-N000000001-2026
INFO ui.workspace [ui] render_ai_workspace_page mt_post_stream_bundle 사용 selected_key=1000-N000000001-2026 run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 — 하단 탭(판단요약 등) 이 번들로 렌더
INFO ui.workspace [HITL_CLOSE] run 시작: mt_pending_review_submit=False mt_review_submit_api_pending=False
INFO ui.workspace [HITL_CLOSE] 다이얼로그 분기: run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 skip_dialog_run_id=None open_key=None
INFO ui.workspace [HITL_CLOSE] 다이얼로그 미렌더 run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 (open_key=False)





#백앤드터미널로그
INFO:     127.0.0.1:50804 - "GET /api/v1/vouchers?queue=all&limit=50 HTTP/1.1" 200 OK
INFO:     127.0.0.1:50807 - "GET /api/v1/cases/1000-N000000001-2026/analysis/latest HTTP/1.1" 200 OK
INFO:     127.0.0.1:50809 - "GET /api/v1/cases/1000-N000000001-2026/analysis/history HTTP/1.1" 200 OK
INFO main [analysis] start_analysis voucher_key=1000-N000000001-2026 run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 case_id=POC-1000-N000000001-2026 enable_hitl=False
INFO main [analysis] _run_analysis_task run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 case_id=POC-1000-N000000001-2026 resume=False (resume_value keys=None)
INFO main [RESUME_TRACE] _run_analysis_task run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 case_id=POC-1000-N000000001-2026 → resume_value 없음, 스크리닝부터 전체 재실행
INFO agent.langgraph_agent [RESUME_TRACE] run_langgraph 진입: run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 case_id=POC-1000-N000000001-2026 resume_value=없음 (None이면 처음부터, 있으면 1차 checkpoint 재개 시도)
INFO agent.langgraph_agent [RESUME_TRACE] run_langgraph run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 경로: 스크리닝부터 전체 실행 (resume_value 없음)
INFO:     127.0.0.1:50811 - "POST /api/v1/cases/1000-N000000001-2026/analysis-runs HTTP/1.1" 200 OK
INFO agent.langgraph_agent [RESUME_TRACE] run_langgraph 노드 실행: run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 node=start_router
INFO:     127.0.0.1:50814 - "GET /api/v1/analysis-runs/63c2510f-ba4b-4aff-b481-6c99f4589a90/stream HTTP/1.1" 200 OK
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO agent.langgraph_agent [RESUME_TRACE] run_langgraph 노드 실행: run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 node=intake
INFO agent.langgraph_agent [agent] node=intake pending_events=4 (THINKING_*=1)
INFO main [RESUME_TRACE] _run_analysis_task run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 첫 스트림 이벤트: ev_type=AGENT_EVENT
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO agent.langgraph_agent [RESUME_TRACE] run_langgraph 노드 실행: run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 node=planner
INFO agent.langgraph_agent [agent] node=planner pending_events=4 (THINKING_*=1)
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/text-embedding-3-large/embeddings?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 400 Bad Request"
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO agent.langgraph_agent [RESUME_TRACE] run_langgraph 노드 실행: run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 node=execute
INFO agent.langgraph_agent [agent] node=execute pending_events=10 (THINKING_*=1)
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO agent.langgraph_agent [RESUME_TRACE] run_langgraph 노드 실행: run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 node=critic
INFO agent.langgraph_agent [agent] node=critic pending_events=3 (THINKING_*=1)
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO agent.langgraph_agent [RESUME_TRACE] run_langgraph 노드 실행: run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 node=verify
INFO agent.langgraph_agent [agent] node=verify pending_events=4 (THINKING_*=1)
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO agent.langgraph_agent [RESUME_TRACE] run_langgraph 노드 실행: run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 node=reporter
INFO agent.langgraph_agent [agent] node=reporter pending_events=3 (THINKING_*=1)
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-5/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO agent.langgraph_agent [RESUME_TRACE] run_langgraph 노드 실행: run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 node=finalizer
INFO agent.langgraph_agent [agent] node=finalizer pending_events=3 (THINKING_*=1)
INFO main [RESUME_TRACE] _run_analysis_task run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 터미널 이벤트: ev_type=completed status=None
INFO main [analysis] task ev_type=completed run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 status=REVIEW_REQUIRED
INFO main [analysis] task done run_id=63c2510f-ba4b-4aff-b481-6c99f4589a90 final_status=REVIEW_REQUIRED — closing stream (done)
INFO:     127.0.0.1:51004 - "GET /api/v1/cases/1000-N000000001-2026/analysis/latest HTTP/1.1" 200 OK
INFO:     127.0.0.1:51006 - "GET /api/v1/cases/1000-N000000001-2026/analysis/history HTTP/1.1" 200 OK
INFO:     127.0.0.1:51008 - "GET /api/v1/analysis-runs/63c2510f-ba4b-4aff-b481-6c99f4589a90/events HTTP/1.1" 200 OK
INFO:     127.0.0.1:51010 - "GET /api/v1/vouchers?queue=all&limit=50 HTTP/1.1" 200 OK
INFO:     127.0.0.1:51013 - "GET /api/v1/analysis-runs/63c2510f-ba4b-4aff-b481-6c99f4589a90/diagnostics HTTP/1.1" 200 OK



