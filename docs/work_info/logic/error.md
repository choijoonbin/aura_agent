INFO services.demo_data_service save_custom_demo_case: image saved to data/evidence_uploads/27ab3f33-41f2-4229-b9b9-dee3a202a67d/evidence.png
INFO agent.screener screening llm request: model=gpt-4o-mini response_format={'type': 'json_object'} messages_contain_json=True
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-4o-mini/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO agent.screener screening llm proposal: model=gpt-4o-mini llm_case_type=LIMIT_EXCEED llm_confidence=0.9 llm_reason=예산 초과 사용이 발생하였습니다.
INFO agent.screener screening guardrail input: deterministic_case_type=LIMIT_EXCEED deterministic_score=60 deterministic_severity=HIGH llm_case_type=LIMIT_EXCEED llm_confidence=0.9 min_override_confidence=0.75 signals={"amount": 184000.0, "budget_exceeded": true, "hour": 14, "hr_status": "WORK", "is_holiday": true, "is_leave": false, "is_night": false, "mcc_code": "5812", "mcc_high_risk": false, "mcc_leisure": false, "mcc_medium_risk": true}
INFO agent.screener screening guardrail decision: final_case_type=LIMIT_EXCEED align_reason=llm_rule_agree deterministic_case_type=LIMIT_EXCEED deterministic_score=60 llm_case_type=LIMIT_EXCEED llm_confidence=0.9
INFO agent.screener screening hybrid result: llm_case_type=LIMIT_EXCEED final_case_type=LIMIT_EXCEED score=60 severity=HIGH align_reason=llm_rule_agree
INFO services.demo_data_service save_custom_demo_case: DB voucher created (voucher_key=1000-BL00000001-2026, uuid=27ab3f33-41f2-4229-b9b9-dee3a202a67d)
INFO services.demo_data_service save_custom_demo_case: meta saved to data/evidence_uploads/27ab3f33-41f2-4229-b9b9-dee3a202a67d/meta.json (uuid=27ab3f33-41f2-4229-b9b9-dee3a202a67d)
INFO services.demo_data_service save_custom_demo_case: image saved to data/evidence_uploads/1a7ac3d8-bff4-497f-ba3e-0b6fd0e18f99/evidence.png
INFO agent.screener screening llm request: model=gpt-4o-mini response_format={'type': 'json_object'} messages_contain_json=True
INFO httpx HTTP Request: POST https://skcc-atl-master-openai-01.openai.azure.com/openai/deployments/gpt-4o-mini/chat/completions?api-version=2024-12-01-preview "HTTP/1.1 200 OK"
INFO agent.screener screening llm proposal: model=gpt-4o-mini llm_case_type=LIMIT_EXCEED llm_confidence=0.85 llm_reason=예산이 초과된 경비 전표입니다.
INFO agent.screener screening guardrail input: deterministic_case_type=LIMIT_EXCEED deterministic_score=60 deterministic_severity=HIGH llm_case_type=LIMIT_EXCEED llm_confidence=0.85 min_override_confidence=0.75 signals={"amount": 184000.0, "budget_exceeded": true, "hour": 14, "hr_status": "WORK", "is_holiday": true, "is_leave": false, "is_night": false, "mcc_code": "5812", "mcc_high_risk": false, "mcc_leisure": false, "mcc_medium_risk": true}
INFO agent.screener screening guardrail decision: final_case_type=LIMIT_EXCEED align_reason=llm_rule_agree deterministic_case_type=LIMIT_EXCEED deterministic_score=60 llm_case_type=LIMIT_EXCEED llm_confidence=0.85
INFO agent.screener screening hybrid result: llm_case_type=LIMIT_EXCEED final_case_type=LIMIT_EXCEED score=60 severity=HIGH align_reason=llm_rule_agree
INFO services.demo_data_service save_custom_demo_case: DB voucher created (voucher_key=1000-BL00000002-2026, uuid=1a7ac3d8-bff4-497f-ba3e-0b6fd0e18f99)
INFO services.demo_data_service save_custom_demo_case: meta saved to data/evidence_uploads/1a7ac3d8-bff4-497f-ba3e-0b6fd0e18f99/meta.json (uuid=1a7ac3d8-bff4-497f-ba3e-0b6fd0e18f99)
