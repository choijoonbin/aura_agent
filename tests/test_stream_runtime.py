"""
StreamRuntime purge 동작 검증.
시연 데이터 삭제 후 동일 case_id 재사용 시 과거 run 상태가 남지 않아야 한다.
"""
import asyncio
import unittest

from services.stream_runtime import StreamRuntime


class TestStreamRuntimePurge(unittest.TestCase):
    def test_purge_case_removes_all_runtime_state(self):
        runtime = StreamRuntime()
        case_id = "POC-1000-H000000001-2026"
        run_id = "run-a"

        runtime.create_run(case_id, run_id)
        runtime.set_result(run_id, {"result": {"status": "COMPLETED"}})
        runtime.set_hitl_request(run_id, {"reasons": ["review"]})
        runtime.set_hitl_response(run_id, {"approved": True})
        runtime.set_hitl_draft(run_id, {"comment": "ok"})
        asyncio.run(runtime.publish(run_id, "AGENT_EVENT", {"event_type": "NODE_END"}))

        self.assertEqual(runtime.latest_run_of_case(case_id), run_id)
        self.assertEqual(runtime.list_runs_of_case(case_id), [run_id])
        self.assertIsNotNone(runtime.get_queue(run_id))
        self.assertTrue(runtime.get_timeline(run_id))
        self.assertIsNotNone(runtime.get_lineage(run_id))

        runtime.purge_case(case_id)

        self.assertIsNone(runtime.latest_run_of_case(case_id))
        self.assertEqual(runtime.list_runs_of_case(case_id), [])
        self.assertIsNone(runtime.get_queue(run_id))
        self.assertIsNone(runtime.get_result(run_id))
        self.assertEqual(runtime.get_timeline(run_id), [])
        self.assertIsNone(runtime.get_hitl_request(run_id))
        self.assertIsNone(runtime.get_hitl_response(run_id))
        self.assertIsNone(runtime.get_hitl_draft(run_id))
        self.assertIsNone(runtime.get_lineage(run_id))

    def test_purge_cases_keeps_other_case_intact(self):
        runtime = StreamRuntime()
        case_a = "POC-1000-H000000001-2026"
        case_b = "POC-1000-H000000002-2026"
        run_a = "run-a"
        run_b = "run-b"

        runtime.create_run(case_a, run_a)
        runtime.create_run(case_b, run_b)
        runtime.set_result(run_a, {"result": {"status": "COMPLETED"}})
        runtime.set_result(run_b, {"result": {"status": "HITL_REQUIRED"}})

        runtime.purge_cases([case_a, case_a])

        self.assertIsNone(runtime.latest_run_of_case(case_a))
        self.assertIsNone(runtime.get_result(run_a))
        self.assertEqual(runtime.latest_run_of_case(case_b), run_b)
        self.assertIsNotNone(runtime.get_result(run_b))
        self.assertEqual(runtime.list_runs_of_case(case_b), [run_b])


if __name__ == "__main__":
    unittest.main()
