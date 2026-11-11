"""V4 Alpha 全部使用 Mock Provider；安全执行集成测试仍走真实本地子进程。"""

import json
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from src.agent_report import report_packet
from src.agent_runner import AgentError, AgentRunner, AgentState
from src.agent_tools import TOOLS, inspect_schema, run_safe_sql, summarize_results
from src.ai_analysis import AIAnalysisError, SQLProposal
from src.llm_provider import CallMetrics, LLMError, RequestBudget
from src.sql_analysis import QueryResult, SQLAnalysisError


def plan(count=3):
    return json.dumps({"steps": [{"title": f"分析{i}", "question": "统计记录数"}
                                  for i in range(count)]}, ensure_ascii=False)


def sql(text="SELECT COUNT(*) AS total FROM uploaded_data"):
    return json.dumps({"sql": text, "explanation": "统计记录数"}, ensure_ascii=False)


EXPLANATION = json.dumps({"facts": [{"kind": "cell", "row": 0, "column": "值"}],
                          "inferences": ["insufficient_context"],
                          "suggestions": ["review_full_results"]})


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.frame = pd.DataFrame({"location": ["虚构甲", "虚构乙", None],
                                   "income": [100, 80, 20], "hours": [5, 4, 0]})
        self.provider = Mock()
        self.provider.last_metrics = None
        self.provider.complete_json.side_effect = [plan(), sql(), sql(), sql(), EXPLANATION]
        self.budget = RequestBudget()
        self.runner = AgentRunner(self.frame, "dataset-A", "分析收入和工时", self.provider, self.budget)

    def call(self, method, command_id, **kwargs):
        return getattr(self.runner, method)(dataset_id="dataset-A", command_id=command_id, **kwargs)

    def ready(self):
        self.call("plan", "plan", confirmed=True)
        self.call("approve_plan", "approve", confirmed=True)

    def draft(self, index=0):
        return self.call("generate_step", f"generate{index}", confirmed=True)

    def execute(self, snapshot, index=0):
        return self.call("execute_step", f"execute{index}",
                         proposal_id=snapshot.proposal_id, confirmed=True)

    def complete_mock(self):
        self.ready()
        result = QueryResult(pd.DataFrame({"total": [3]}), False)
        with patch("src.agent_tools.execute_proposal", return_value=result):
            for i in range(3):
                snapshot = self.execute(self.draft(i), i)
        return snapshot

    def test_three_steps_use_real_safe_executor(self):
        self.ready()
        for i in range(3):
            snapshot = self.execute(self.draft(i), i)
            self.assertEqual(snapshot.results[i].data.iloc[0, 0], 3)
        self.assertEqual(snapshot.state, AgentState.COMPLETED)
        self.assertEqual(len(snapshot.sql_history), 3)
        self.assertEqual(self.budget.used, 4)
        states = {event.state for event in snapshot.audit}
        self.assertTrue({AgentState.CREATED, AgentState.PLANNED, AgentState.RUNNING,
                         AgentState.STEP_COMPLETED, AgentState.COMPLETED} <= states)
        self.assertEqual(snapshot.facts[0]["returned_rows"], 1)

    def test_no_external_call_without_confirmation(self):
        with self.assertRaises(AgentError):
            self.call("plan", "unconfirmed")
        self.provider.complete_json.assert_not_called()
        self.assertEqual(self.budget.used, 0)
        self.assertEqual(self.runner.snapshot().state, AgentState.CREATED)

    def test_plan_approval_cannot_be_skipped(self):
        self.call("plan", "plan", confirmed=True)
        with self.assertRaises(AgentError):
            self.draft()
        self.assertEqual(self.budget.used, 1)

    def test_each_sql_requires_confirmation_and_correct_identity(self):
        self.ready()
        snapshot = self.draft()
        with patch("src.agent_tools.execute_proposal") as execute:
            for args in ({"proposal_id": snapshot.proposal_id},
                         {"proposal_id": "stale", "confirmed": True}):
                with self.assertRaises(AgentError):
                    self.call("execute_step", "denied", **args)
            execute.assert_not_called()
        self.assertEqual(self.runner.snapshot().state, AgentState.AWAITING_APPROVAL)

    def test_approval_from_previous_step_is_invalid(self):
        self.ready()
        old = self.draft()
        with patch("src.agent_tools.execute_proposal", return_value=QueryResult(pd.DataFrame({"n": [3]}), False)):
            self.execute(old)
            self.draft(1)
            with self.assertRaises(AgentError):
                self.call("execute_step", "stale", proposal_id=old.proposal_id, confirmed=True)

    def test_reject_sql_leaves_it_unexecuted_then_cancel(self):
        self.ready()
        draft = self.draft()
        with patch("src.agent_tools.execute_proposal") as execute:
            with self.assertRaises(AgentError):
                self.call("execute_step", "reject", proposal_id=draft.proposal_id, confirmed=False)
            self.runner.cancel(dataset_id="dataset-A")
            execute.assert_not_called()
        self.assertEqual(self.runner.snapshot().state, AgentState.CANCELLED)

    def test_malicious_invalid_and_wrong_column_sql(self):
        for query in ("DROP TABLE uploaded_data", "SELECT * FROM read_csv('secret.csv')",
                      "SELECT missing_field FROM uploaded_data", "SELECT FROM"):
            with self.subTest(query=query):
                self.setUp()
                self.provider.complete_json.side_effect = [plan(), sql(query)]
                self.ready()
                with patch("src.agent_tools.execute_proposal") as execute:
                    with self.assertRaises(AgentError):
                        self.draft()
                    execute.assert_not_called()
                self.assertEqual(self.runner.snapshot().state, AgentState.FAILED)

    def test_too_many_steps_and_invalid_json_fail_closed(self):
        for output in (plan(4), plan(0), "not JSON", '{"steps":[],"sql":"x"}',
                       '{"steps":[],"steps":[]}'):
            with self.subTest(output=output):
                self.setUp()
                self.provider.complete_json.side_effect = [output]
                with self.assertRaises(AgentError):
                    self.call("plan", "plan", confirmed=True)
                self.assertEqual(self.runner.snapshot().state, AgentState.FAILED)
                self.assertEqual(self.provider.complete_json.call_count, 1)

    def test_session_budget_shared_and_failure_stops(self):
        self.budget.used = self.budget.limit
        with self.assertRaises(AgentError):
            self.call("plan", "plan", confirmed=True)
        self.provider.complete_json.assert_not_called()
        self.assertEqual(self.runner.snapshot().state, AgentState.FAILED)

    def test_task_six_call_limit(self):
        # 到达六次后，内部适配器也不能再请求；正常三步流程仅需4至5次。
        self.runner._calls = [Mock()] * 6
        with self.assertRaises(AgentError):
            self.call("plan", "plan", confirmed=True)
        self.provider.complete_json.assert_not_called()
        self.assertEqual(self.budget.used, 0)

    def test_query_timeout_stops_without_more_calls(self):
        self.ready()
        draft = self.draft()
        with patch("src.agent_tools.execute_proposal", side_effect=SQLAnalysisError("查询执行超时。")):
            with self.assertRaises(AgentError):
                self.execute(draft)
        self.assertEqual(self.runner.snapshot().state, AgentState.FAILED)
        self.assertEqual(len(self.runner.snapshot().results), 0)
        with self.assertRaises(AgentError):
            self.draft(1)
        self.assertEqual(self.budget.used, 2)

    def test_provider_error_is_sanitized_no_retry(self):
        self.provider.complete_json.side_effect = LLMError("sensitive-key-and-body")
        with self.assertRaises(AgentError) as raised:
            self.call("plan", "plan", confirmed=True)
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertNotIn("sensitive", repr(self.runner.snapshot()))
        self.assertEqual(self.budget.used, 1)
        self.assertFalse(self.runner.snapshot().calls[0].success)

    def test_cancel_before_and_during_model_call(self):
        self.runner.cancel(dataset_id="dataset-A")
        with self.assertRaises(AgentError):
            self.call("plan", "plan", confirmed=True)
        self.provider.complete_json.assert_not_called()
        self.setUp()
        def cancel_during(_):
            self.runner.cancel(dataset_id="dataset-A")
            return plan()
        self.provider.complete_json.side_effect = cancel_during
        with self.assertRaises(AgentError):
            self.call("plan", "plan", confirmed=True)
        self.assertEqual(self.runner.snapshot().state, AgentState.CANCELLED)
        self.assertEqual(self.runner.snapshot().steps, ())

    def test_duplicate_commands_do_not_repeat_calls_or_execution(self):
        self.ready()
        self.call("plan", "plan", confirmed=True)
        draft = self.draft()
        self.draft()
        with patch("src.agent_tools.execute_proposal", return_value=QueryResult(pd.DataFrame({"n": [3]}), False)) as execute:
            self.execute(draft)
            self.execute(draft)
            self.assertEqual(execute.call_count, 1)
        self.assertEqual(self.budget.used, 2)
        with self.assertRaises(AgentError):
            self.call("generate_step", "plan", confirmed=True)

    def test_new_dataset_invalidates_old_task_and_results(self):
        self.ready()
        with self.assertRaises(AgentError):
            self.runner.generate_step(dataset_id="dataset-B", command_id="new", confirmed=True)
        self.assertEqual(self.runner.snapshot().state, AgentState.CANCELLED)
        with self.assertRaises(AgentError):
            self.draft()
        self.assertEqual(self.budget.used, 1)

    def test_schema_and_generation_requests_never_contain_rows(self):
        schema = inspect_schema(self.frame)
        self.assertEqual(schema["rows"], 3)
        self.assertEqual(schema["fields"][0]["缺失值数量"], 1)
        preview = self.runner.preview_plan("dataset-A")
        self.ready()
        step_preview = self.runner.preview_step(dataset_id="dataset-A")
        self.draft()
        self.assertEqual(preview, self.provider.complete_json.call_args_list[0].args[0])
        self.assertEqual(step_preview, self.provider.complete_json.call_args_list[1].args[0])
        for call in self.provider.complete_json.call_args_list:
            payload = json.loads(call.args[0][1]["content"])
            self.assertEqual(set(payload), {"table", "columns", "question"})
            self.assertNotIn("虚构甲", json.dumps(payload, ensure_ascii=False))

    def test_summary_separate_consent_and_exact_preview(self):
        self.complete_mock()
        packet = self.runner.preview_report(dataset_id="dataset-A")
        for kwargs in ({"preview_json": packet.json_text},
                       {"preview_json": "changed", "confirmed": True}):
            with self.assertRaises(AgentError):
                self.call("summarize", "denied", **kwargs)
        self.assertEqual(self.budget.used, 4)
        snapshot = self.call("summarize", "report", preview_json=packet.json_text, confirmed=True)
        self.assertEqual(self.provider.complete_json.call_args.args[0][1]["content"], packet.json_text)
        self.assertEqual(self.budget.used, 5)
        self.assertTrue(snapshot.report.facts)
        self.call("summarize", "report", preview_json=packet.json_text, confirmed=True)
        self.assertEqual(self.budget.used, 5)

    def test_null_partial_and_global_packet_limits(self):
        data = pd.DataFrame({"x": [None] + ["长" * 1000] * 25, "y": range(26)})
        packet = report_packet((QueryResult(data, True), QueryResult(data, False)))
        payload = json.loads(packet.json_text)
        self.assertTrue(packet.partial)
        self.assertLessEqual(len(payload["rows"]), 20)
        self.assertLessEqual(len(payload["columns"]), 20)
        self.assertLessEqual(len(packet.json_text.encode("utf-8")), 8192)
        self.assertIsNone(payload["rows"][0][3])
        self.assertEqual(payload["rows"][0][:3], [1, 1, "x"])
        self.assertTrue(all(row[0] == 1 for row in payload["rows"]))

    def test_report_keeps_large_integer_precision_with_null(self):
        data = pd.DataFrame({"n": pd.Series([9007199254740993, None], dtype=object)})
        packet = report_packet((QueryResult(data, False),))
        rows = json.loads(packet.json_text)["rows"]
        self.assertEqual(rows[0][3], 9007199254740993)
        self.assertIsNone(rows[1][3])

    def test_empty_results_local_facts_without_invented_report(self):
        self.ready()
        with patch("src.agent_tools.execute_proposal", return_value=QueryResult(pd.DataFrame({"n": []}), False)):
            for i in range(3):
                snapshot = self.execute(self.draft(i), i)
        self.assertEqual(snapshot.facts[0]["returned_rows"], 0)
        with self.assertRaises(AIAnalysisError):
            self.runner.preview_report(dataset_id="dataset-A")
        self.assertEqual(self.budget.used, 4)

    def test_actual_usage_and_unavailable_usage(self):
        self.provider.last_metrics = CallMetrics(0.1, {"prompt_tokens": 8})
        self.call("plan", "plan", confirmed=True)
        metrics = self.runner.snapshot().calls[0]
        self.assertEqual(metrics.usage, {"prompt_tokens": 8})
        self.assertGreaterEqual(metrics.elapsed_seconds, 0)
        self.setUp()
        self.call("plan", "plan", confirmed=True)
        self.assertIsNone(self.runner.snapshot().calls[0].usage)

    def test_failed_call_does_not_reuse_previous_usage(self):
        self.provider.last_metrics = CallMetrics(0.1, {"prompt_tokens": 8})
        self.ready()
        self.provider.complete_json.side_effect = RuntimeError("private body")
        with self.assertRaises(AgentError):
            self.draft()
        self.assertIsNone(self.runner.snapshot().calls[-1].usage)

    def test_failed_summary_stops_and_preserves_local_facts(self):
        self.complete_mock()
        self.provider.complete_json.side_effect = ["invalid JSON"]
        packet = self.runner.preview_report(dataset_id="dataset-A")
        with self.assertRaises(AgentError):
            self.call("summarize", "report", preview_json=packet.json_text, confirmed=True)
        snapshot = self.runner.snapshot()
        self.assertEqual(snapshot.state, AgentState.FAILED)
        self.assertIsNone(snapshot.report)
        self.assertEqual(len(snapshot.facts), 3)

    def test_report_retains_null_and_partial_warning(self):
        self.ready()
        result = QueryResult(pd.DataFrame({"n": [None]}), True)
        with patch("src.agent_tools.execute_proposal", return_value=result):
            for i in range(3):
                self.execute(self.draft(i), i)
        packet = self.runner.preview_report(dataset_id="dataset-A")
        snapshot = self.call("summarize", "report", preview_json=packet.json_text, confirmed=True)
        self.assertTrue(snapshot.report.partial)
        self.assertIn("null", snapshot.report.facts[0])
        self.assertTrue(any("部分结果" in note for note in snapshot.report.notes))

    def test_new_command_ids_cannot_reexecute_or_regenerate(self):
        self.ready()
        draft = self.draft()
        with self.assertRaises(AgentError):
            self.draft(99)
        with patch("src.agent_tools.execute_proposal", return_value=QueryResult(pd.DataFrame({"n": [3]}), False)) as execute:
            self.execute(draft)
            with self.assertRaises(AgentError):
                self.execute(draft, 99)
            self.assertEqual(execute.call_count, 1)
        self.assertEqual(self.budget.used, 2)

    def test_cancel_during_sql_discards_inflight_result(self):
        self.ready()
        draft = self.draft()
        def cancel_during(*args, **kwargs):
            self.runner.cancel(dataset_id="dataset-A")
            return QueryResult(pd.DataFrame({"n": [3]}), False)
        with patch("src.agent_tools.execute_proposal", side_effect=cancel_during):
            with self.assertRaises(AgentError):
                self.execute(draft)
        self.assertEqual(self.runner.snapshot().state, AgentState.CANCELLED)
        self.assertEqual(self.runner.snapshot().results, ())

    def test_snapshot_and_source_mutations_do_not_change_task(self):
        self.ready()
        self.frame.loc[0, "income"] = 999
        self.assertEqual(self.runner._frame.loc[0, "income"], 100)
        self.complete_mock_from_ready()
        snapshot = self.runner.snapshot()
        snapshot.results[0].data.iloc[0, 0] = 999
        self.assertEqual(self.runner.snapshot().results[0].data.iloc[0, 0], 3)

    def complete_mock_from_ready(self):
        with patch("src.agent_tools.execute_proposal", return_value=QueryResult(pd.DataFrame({"total": [3]}), False)):
            for i in range(3):
                self.execute(self.draft(i), i)

    def test_fixed_tools_reject_missing_authorization(self):
        self.assertEqual(set(TOOLS), {"inspect_schema", "run_safe_sql", "summarize_results"})
        with self.assertRaises(TypeError):
            TOOLS["other"] = object()
        with self.assertRaises(AIAnalysisError):
            run_safe_sql(self.frame, SQLProposal("SELECT * FROM uploaded_data", "预览"))
        packet = report_packet((QueryResult(pd.DataFrame({"n": [3]}), False),))
        with self.assertRaises(AIAnalysisError):
            summarize_results(packet, self.provider)
        self.provider.complete_json.assert_not_called()

    def test_invalid_state_transition_rejected(self):
        with self.assertRaises(AgentError):
            self.runner._transition(AgentState.COMPLETED)
        self.assertEqual(self.runner.snapshot().state, AgentState.CREATED)


if __name__ == "__main__":
    unittest.main()
