"""两步任务、分类审计及加权时薪回归；所有模型调用均为 Mock。"""

import json
import unittest
from unittest.mock import Mock, patch

from src.agent_planner import parse_plan, planning_messages, requested_step_count
from src.agent_runner import AgentError, AgentRunner, AgentState
from src.ai_analysis import AIAnalysisError
from src.evaluation import WEIGHTED_RATE_SQL, load_demo_frame
from src.llm_provider import LLMError, RequestBudget
from src.sql_analysis import QueryResult, SQLAnalysisError


GOAL = "请用两个 SQL 步骤分析：先汇总收入和工时，再计算加权时薪，零总工时返回 NULL。"


def plan(count=2):
    return json.dumps({"steps": [{"title": f"SQL分析{i + 1}", "question": question}
                                 for i, question in enumerate(["统计记录数", "计算地点加权时薪", "检查缺失值"][:count])]}, ensure_ascii=False)


def sql(query="SELECT COUNT(*) AS total FROM uploaded_data"):
    return json.dumps({"sql": query, "explanation": "按实际字段分析，结果待执行。"}, ensure_ascii=False)


class AgentDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.frame = load_demo_frame()
        self.provider = Mock(last_metrics=None)
        self.provider.complete_json.side_effect = [plan(), sql(), sql(WEIGHTED_RATE_SQL)]
        self.budget = RequestBudget()
        self.runner = AgentRunner(self.frame, "synthetic", GOAL, self.provider, self.budget)
        self.net = patch("requests.sessions.Session.request", side_effect=AssertionError("测试禁止HTTP"))
        self.net.start()
        self.addCleanup(self.net.stop)

    def call(self, method, command, **kwargs):
        return getattr(self.runner, method)(dataset_id="synthetic", command_id=command, **kwargs)

    def ready(self):
        self.call("plan", "plan", confirmed=True)
        self.call("approve_plan", "approve", confirmed=True)

    def first(self):
        self.ready()
        draft = self.call("generate_step", "gen1", confirmed=True)
        with patch("src.agent_tools.execute_proposal", return_value=QueryResult(self.frame.head(4), False)):
            self.call("execute_step", "exec1", proposal_id=draft.proposal_id, confirmed=True)
        return draft

    def test_two_steps_real_execution_and_zero_denominator(self):
        self.ready()
        for i in (1, 2):
            draft = self.call("generate_step", f"gen{i}", confirmed=True)
            snapshot = self.call("execute_step", f"exec{i}", proposal_id=draft.proposal_id, confirmed=True)
        self.assertEqual(snapshot.state, AgentState.COMPLETED)
        self.assertEqual(len(snapshot.results), 2)
        self.assertEqual(self.budget.used, 3)
        result = snapshot.results[1].data
        self.assertEqual(len(result), 4)
        indexed = result.set_index("location")
        self.assertEqual(indexed.loc["B", "hourly_rate"], 40)
        self.assertAlmostEqual(indexed.loc["A", "hourly_rate"], 33.33)
        self.assertTrue(indexed.loc[["C", "D"], "hourly_rate"].isna().all())
        self.assertEqual(indexed.loc["C", "total_income"], 90)

    def test_second_generation_errors_have_exact_categories(self):
        outputs = [
            ("not JSON", "SQL_GENERATION_FORMAT"),
            (json.dumps({"sql": 7, "explanation": "说明"}), "SQL_GENERATION_FORMAT"),
            (sql("SELECT CASE WHEN SUM(hours)=0 THEN NULL ELSE SUM(income)/SUM(hours) END FROM uploaded_data"), "SQL_AST_REJECTED"),
            (sql("SELECT SUM(income)/NULLIF(SUM(hours),0) FROM uploaded_data"), "SQL_AST_REJECTED"),
            (sql("SELECT missing_column FROM uploaded_data"), "SQL_FIELD_ERROR"),
            (sql("SELECT * FROM another_table"), "SQL_TABLE_ERROR"),
            (sql("SELECT * FROM"), "SQL_PARSE_ERROR"),
            (sql(None), "SQL_CLARIFICATION_REQUIRED"),
        ]
        for output, category in outputs:
            with self.subTest(category=category, output=output):
                self.setUp()
                self.provider.complete_json.side_effect = [plan(), sql(), output]
                old = self.first()
                with patch("src.agent_tools.execute_proposal") as executor:
                    with self.assertRaises(AgentError):
                        self.call("generate_step", "gen2", confirmed=True)
                    snapshot = self.runner.snapshot()
                    detail = snapshot.failure_detail
                    self.assertEqual(snapshot.state, AgentState.FAILED)
                    self.assertEqual(detail.step, 2)
                    self.assertEqual(detail.category, category)
                    self.assertFalse(detail.sql_submitted)
                    self.assertEqual(detail.model_requests_consumed, 1)
                    self.assertEqual(len(snapshot.results), 1)
                    event = next(e for e in reversed(snapshot.audit) if e.kind == "command_failed")
                    self.assertEqual((event.step, event.category, event.model_requests_consumed), (2, category, 1))
                    with self.assertRaises(AgentError):
                        self.call("execute_step", "stale", proposal_id=old.proposal_id, confirmed=True)
                    with self.assertRaises(AgentError):
                        self.call("generate_step", "gen3", confirmed=True)
                    executor.assert_not_called()
                    self.assertEqual(self.budget.used, 3)
                    self.assertEqual(self.runner.snapshot().failure_detail, detail)

    def test_extra_step_rejected_without_truncating_or_execution(self):
        self.provider.complete_json.side_effect = [plan(3)]
        with self.assertRaises(AgentError):
            self.call("plan", "plan", confirmed=True)
        snapshot = self.runner.snapshot()
        self.assertEqual(snapshot.failure_detail.category, "PLAN_STEP_COUNT_MISMATCH")
        self.assertIsNone(snapshot.failure_detail.step)
        self.assertEqual(snapshot.steps, ())
        self.assertEqual(self.budget.used, 1)
        self.assertIn("恰好 2", planning_messages(self.frame, GOAL)[0]["content"])

    def test_planning_json_and_step_validation_distinct(self):
        for text, category in (("bad", "PLANNING_JSON_INVALID"), ('{"steps": []}', "PLANNING_STEPS_INVALID")):
            with self.subTest(category=category):
                with self.assertRaises(AIAnalysisError) as raised:
                    parse_plan(text, GOAL)
                self.assertEqual(raised.exception.category, category)

    def test_report_is_not_a_sql_step(self):
        text = json.dumps({"steps": [{"title": "最终报告", "question": "整合结果生成最终报告"}]})
        with self.assertRaises(AIAnalysisError) as raised:
            parse_plan(text)
        self.assertEqual(raised.exception.category, "PLAN_NON_SQL_STEP")

    def test_explicit_count_detection_excludes_ordinals_and_data_counts(self):
        for goal in (GOAL, "分两步分析收入", "Use two SQL steps", "用2个步骤分析", "仅两步SQL", "只用2步分析"):
            self.assertEqual(requested_step_count(goal), 2)
        for goal in ("第一步汇总，第二步检查", "统计两个月的收入", "分析第二个步骤"):
            self.assertIsNone(requested_step_count(goal))
        with self.assertRaises(AIAnalysisError):
            requested_step_count("执行四个SQL步骤")

    def test_failed_second_model_request_counted_and_sanitized(self):
        self.provider.complete_json.side_effect = [plan(), sql(), LLMError("private-response-and-key", status_code=429)]
        self.first()
        with self.assertRaises(AgentError):
            self.call("generate_step", "gen2", confirmed=True)
        snapshot = self.runner.snapshot()
        self.assertEqual(snapshot.failure_detail.category, "RATE_LIMITED")
        self.assertEqual(snapshot.failure_detail.model_requests_consumed, 1)
        self.assertEqual(self.budget.used, 3)
        self.assertNotIn("private-response", repr(snapshot))

    def test_second_budget_exhaustion_does_not_send(self):
        self.budget.limit = 2
        self.first()
        with self.assertRaises(AgentError):
            self.call("generate_step", "gen2", confirmed=True)
        detail = self.runner.snapshot().failure_detail
        self.assertEqual(detail.category, "SESSION_REQUEST_LIMIT")
        self.assertEqual(detail.stage, "模型请求预算校验")
        self.assertEqual(detail.model_requests_consumed, 0)
        self.assertEqual(self.provider.complete_json.call_count, 2)

    def test_execution_failure_distinct_from_generation(self):
        self.ready()
        draft = self.call("generate_step", "gen1", confirmed=True)
        with patch("src.agent_tools.execute_proposal", side_effect=SQLAnalysisError("查询执行超时。")):
            with self.assertRaises(AgentError):
                self.call("execute_step", "exec1", proposal_id=draft.proposal_id, confirmed=True)
        detail = self.runner.snapshot().failure_detail
        self.assertEqual(detail.category, "SQL_EXECUTION_TIMEOUT")
        self.assertTrue(detail.sql_submitted)
        self.assertEqual(detail.model_requests_consumed, 0)

    def test_internal_state_transition_failure_is_classified(self):
        with self.assertRaises(AgentError):
            self.runner._command("test", "bad-transition", "synthetic", [],
                                 lambda: self.runner._transition(AgentState.COMPLETED))
        snapshot = self.runner.snapshot()
        self.assertEqual(snapshot.state, AgentState.FAILED)
        self.assertEqual(snapshot.failure_detail.category, "AGENT_STATE_TRANSITION")
        self.assertEqual(snapshot.failure_detail.model_requests_consumed, 0)

    def test_cancel_after_first_prevents_next_request(self):
        self.first()
        self.runner.cancel(dataset_id="synthetic")
        with self.assertRaises(AgentError):
            self.call("generate_step", "gen2", confirmed=True)
        self.assertEqual(self.provider.complete_json.call_count, 2)
        self.assertEqual(self.runner.snapshot().state, AgentState.CANCELLED)


if __name__ == "__main__":
    unittest.main()
