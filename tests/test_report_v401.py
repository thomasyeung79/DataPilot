"""V4.0.1 真实本地计算、审批绑定及确定性报告质量回归，无真实模型请求。"""

from decimal import Decimal
import json
import math
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from src.agent_planner import AnalysisStep
from src.agent_report import AgentReport, grounded_report, local_summary, readable_report_view, report_packet
from src.agent_runner import AgentError, AgentRunner, AgentState
from src.ai_analysis import AIAnalysisError, ExplanationPacket, GroundedExplanation, SQLProposal
from src.evaluation import load_demo_frame
from src.llm_provider import RequestBudget
from src.sql_analysis import QueryResult


TOTALS = "SELECT location, SUM(income) AS total_income, SUM(hours) AS total_hours FROM uploaded_data GROUP BY location ORDER BY location"
RATES = '''SELECT t.location, t.total_income/d.total_hours AS weighted_avg_hourly
FROM (SELECT location, SUM(income) AS total_income FROM uploaded_data GROUP BY location) t
LEFT JOIN (SELECT location, SUM(hours) AS total_hours FROM uploaded_data
GROUP BY location HAVING SUM(hours) <> 0) d
ON t.location=d.location OR (t.location IS NULL AND d.location IS NULL)
ORDER BY t.location'''


def proposal(sql):
    return json.dumps({"sql": sql, "explanation": "按实际字段分析。"}, ensure_ascii=False)


class ReportV401Tests(unittest.TestCase):
    def setUp(self):
        self.frame = load_demo_frame()
        self.provider = Mock(last_metrics=None)
        self.provider.complete_json.side_effect = [json.dumps({"steps": [
            {"title": "地点汇总", "question": "按地点汇总收入及工时"},
            {"title": "加权时薪", "question": "计算加权时薪，零工时NULL"}]}), proposal(TOTALS), proposal(RATES)]
        self.runner = AgentRunner(self.frame, "demo", "只用两个SQL步骤分析收入工时及时薪", self.provider, RequestBudget())
        self.http = patch("requests.sessions.Session.request", side_effect=AssertionError("禁止HTTP"))
        self.http.start()
        self.addCleanup(self.http.stop)

    def ready(self):
        self.runner.plan(dataset_id="demo", command_id="plan", confirmed=True)
        self.runner.approve_plan(dataset_id="demo", command_id="approve", confirmed=True)

    def draft(self, i=1):
        return self.runner.generate_step(dataset_id="demo", command_id=f"gen{i}", confirmed=True)

    def execute(self, snapshot, i=1, confirmed=True):
        return self.runner.execute_step(dataset_id="demo", command_id=f"exec{i}",
                                        proposal_id=snapshot.proposal_id, confirmed=confirmed)

    def samples(self):
        return (QueryResult(pd.DataFrame({"location": ["A", "B", "C", "D"],
                                          "total_income": [400, 400, 90, 0], "total_hours": [12, 10, 0, 0]}), False),
                QueryResult(pd.DataFrame({"location": ["A", "B", "C", "D"],
                                          "weighted_avg_hourly": [400/12, 40, None, None]}), False))

    def test_two_steps_real_values_and_local_readable_report(self):
        self.ready()
        first = self.execute(self.draft())
        self.assertEqual(first.state, AgentState.STEP_COMPLETED)
        self.assertEqual(self.provider.complete_json.call_count, 2)
        second_draft = self.draft(2)
        with self.assertRaises(AgentError):
            self.execute(second_draft, 2, confirmed=False)
        snapshot = self.execute(second_draft, 2)
        self.assertEqual(snapshot.state, AgentState.COMPLETED)
        self.assertEqual(list(snapshot.results[0].data.itertuples(index=False, name=None)),
                         [("A", 400, 12), ("B", 400, 10), ("C", 90, 0), ("D", 0, 0)])
        rates = snapshot.results[1].data
        self.assertTrue(math.isclose(rates.iloc[0, 1], 400/12, rel_tol=1e-12))
        self.assertEqual(rates.iloc[1, 1], 40)
        self.assertTrue(rates.iloc[2:, 1].isna().all())
        summary = local_summary(snapshot.results)
        self.assertIn('地点（location）="A"', summary.facts[0])
        self.assertIn("总收入（total_income）=400", summary.facts[0])
        self.assertIn("总工时（total_hours）=12", summary.facts[0])
        self.assertIn("步骤2，结果第1行", summary.facts[4])
        self.assertIn("weighted_avg_hourly）=33.33（显示约数", summary.facts[4])
        self.assertFalse(summary.partial)
        self.assertEqual(self.provider.complete_json.call_count, 3)

    def test_sql_replaced_after_preview_invalidates_old_approval(self):
        self.ready()
        draft = self.draft()
        self.runner._proposal = SQLProposal("SELECT COUNT(*) FROM uploaded_data", "changed")
        with patch("src.agent_tools.run_safe_sql") as executor:
            with self.assertRaises(AgentError):
                self.execute(draft)
            executor.assert_not_called()
        snapshot = self.runner.snapshot()
        self.assertEqual(snapshot.state, AgentState.FAILED)
        self.assertEqual(snapshot.failure_detail.category, "APPROVAL_INVALIDATED")
        self.assertFalse(snapshot.failure_detail.sql_submitted)
        self.assertEqual(snapshot.failure_detail.model_requests_consumed, 0)

    def test_plan_changed_after_approval_invalidates_pending_sql(self):
        self.ready()
        draft = self.draft()
        self.runner._steps[1] = AnalysisStep("其他计划", "按其他口径分析")
        with patch("src.agent_tools.run_safe_sql") as executor:
            with self.assertRaises(AgentError):
                self.execute(draft)
            executor.assert_not_called()
        self.assertEqual(self.runner.snapshot().failure_detail.category, "APPROVAL_INVALIDATED")

    def test_plan_changed_before_sql_generation_prevents_model_call(self):
        self.ready()
        self.runner._steps.pop()
        with self.assertRaises(AgentError):
            self.draft()
        self.assertEqual(self.provider.complete_json.call_count, 1)
        self.assertEqual(self.runner.snapshot().failure_detail.category, "APPROVAL_INVALIDATED")

    def test_dataset_change_invalidates_sql_approval(self):
        self.ready()
        draft = self.draft()
        with patch("src.agent_tools.run_safe_sql") as executor:
            with self.assertRaises(AgentError):
                self.runner.execute_step(dataset_id="other", command_id="exec", proposal_id=draft.proposal_id, confirmed=True)
            executor.assert_not_called()
        self.assertEqual(self.runner.snapshot().state, AgentState.CANCELLED)

    def test_first_approval_does_not_approve_second_sql(self):
        self.ready()
        first = self.draft()
        self.execute(first)
        second = self.draft(2)
        self.assertNotEqual(first.proposal_id, second.proposal_id)
        with patch("src.agent_tools.run_safe_sql") as executor:
            with self.assertRaises(AgentError):
                self.runner.execute_step(dataset_id="demo", command_id="stale", proposal_id=first.proposal_id, confirmed=True)
            executor.assert_not_called()

    def test_null_zero_and_empty_string_remain_distinct(self):
        result = QueryResult(pd.DataFrame({"location": [None, ""], "income": [None, 0]}, dtype=object), False)
        summary = local_summary((result,))
        self.assertIn("NULL（null）", summary.facts[0])
        self.assertIn('location）=""', summary.facts[1])
        self.assertIn("income=0", summary.facts[1])

    def test_original_units_and_large_integer_precision_preserved(self):
        result = QueryResult(pd.DataFrame({"收入（USD）": [2**60+1], "工时（h）": [Decimal("1.234567890123456789")]}, dtype=object), False)
        summary = local_summary((result,))
        self.assertIn("收入（USD）=1152921504606846977", summary.facts[0])
        self.assertIn("工时（h）=1.234567890123456789", summary.facts[0])
        self.assertNotIn("元", summary.facts[0])

    def test_truncation_summary_limits_and_full_data_untouched(self):
        result = self.samples()[0]
        result.truncated = True
        summary = local_summary((result,), max_rows=1, max_columns=2)
        self.assertTrue(summary.partial)
        self.assertEqual(len(summary.facts), 1)
        self.assertTrue(any("截断" in note for note in summary.notes))
        self.assertTrue(any("前1行" in note for note in summary.notes))
        self.assertEqual(result.data.shape, (4, 3))

    def test_empty_duplicate_and_invalid_results_fail_closed(self):
        empty = QueryResult(pd.DataFrame({"n": []}), False)
        duplicate = QueryResult(pd.DataFrame([[1, 2]], columns=["n", "n"]), False)
        for result in (None, QueryResult(None, False), duplicate):
            with self.subTest(result=type(result).__name__):
                summary = local_summary((result,))
                self.assertEqual(summary.facts, ())
                self.assertTrue(summary.partial)
        summary = local_summary((empty,))
        self.assertFalse(summary.facts)
        self.assertTrue(any("为空" in note for note in summary.notes))

    def test_abnormal_and_long_cells_explicitly_marked(self):
        result = QueryResult(pd.DataFrame({"value": [float("inf"), [1, 2], "长" * 201]}, dtype=object), False)
        summary = local_summary((result,))
        self.assertTrue(summary.partial)
        self.assertIn("非有限数值", summary.facts[0])
        self.assertIn("异常非标量", summary.facts[1])
        self.assertIn("已缩短", summary.facts[2])

    def test_cloud_report_facts_use_actual_provenance_not_generic_value_label(self):
        packet = report_packet(self.samples())
        explanation = GroundedExplanation(["发送结果第1行，字段「值」的值为 A"], ["推断边界"], ["建议"])
        report = grounded_report(packet, explanation)
        self.assertEqual(len(report.facts), 8)
        self.assertIn("步骤1，结果第1行", report.facts[0])
        self.assertIn("total_income）=400", report.facts[0])
        self.assertNotIn("字段「值」", "".join(report.facts))
        self.assertIn("NULL（null）", report.facts[-1])
        self.assertFalse(report.partial)
        self.assertEqual(report.preview_json, packet.json_text)
        self.assertEqual(report.inferences, ("推断边界",))

    def test_partial_cloud_packet_does_not_fill_unsent_fields(self):
        result = QueryResult(pd.DataFrame({"location": ["A"], **{f"field{i}": [i] for i in range(22)}}), True)
        packet = report_packet((result,))
        report = grounded_report(packet, GroundedExplanation([], [], []))
        self.assertTrue(report.partial)
        self.assertNotIn("field21=", "".join(report.facts))
        self.assertTrue(any("部分结果" in note for note in report.notes))

    def test_invalid_provenance_packet_does_not_invent_facts(self):
        for text in ("private-invalid", '{"columns":[],"rows":[]}',
                     '{"columns":["步骤","结果行","字段","值"],"rows":[["fake",1,"n",2]]}'):
            with self.subTest(text=text[:10]):
                with self.assertRaises(AIAnalysisError) as raised:
                    grounded_report(ExplanationPacket(text, 1, False), GroundedExplanation([], [], []))
                self.assertNotIn("private", str(raised.exception))

    def test_cached_legacy_report_reformatted_without_model_or_mutation(self):
        packet = report_packet(self.samples())
        old = AgentReport(("发送结果第1行，字段「值」的值为 A",), ("推断边界",), (),
                          packet.json_text, False, packet.sent_rows, ())
        view = readable_report_view(old)
        self.assertIn("total_income）=400", view.facts[0])
        self.assertNotIn("字段「值」", "".join(view.facts))
        self.assertIn("字段「值」", old.facts[0])
        self.assertEqual(view.preview_json, old.preview_json)
        self.provider.complete_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
