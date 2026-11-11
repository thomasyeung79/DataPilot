"""当前 AST 能力、无数据模板、精确两步结果和脱敏节点审计回归。"""

import json
import math
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from src.agent_planner import planning_messages
from src.agent_runner import AgentError, AgentRunner, AgentState
from src.ai_analysis import AIAnalysisError, GENERATION_PROMPT, generate_sql, validate_proposal_sql
from src.evaluation import load_demo_frame
from src.llm_provider import RequestBudget
from src.sql_analysis import SQLAnalysisError, safe_ast_metadata, validate_sql, run_query


TOTALS = '''SELECT location, SUM(income) AS total_income, SUM(hours) AS total_hours
FROM uploaded_data GROUP BY location ORDER BY location'''

WEIGHTED = '''SELECT t."location", t."total_income" / d."total_hours" AS "weighted_avg_hourly"
FROM (SELECT "location", SUM("income") AS "total_income"
      FROM uploaded_data GROUP BY "location") t
LEFT JOIN (SELECT "location", SUM("hours") AS "total_hours"
           FROM uploaded_data GROUP BY "location" HAVING SUM("hours") <> 0) d
ON t."location" = d."location" OR (t."location" IS NULL AND d."location" IS NULL)
ORDER BY t."location"'''

GOAL = "只用两个SQL步骤：先按地点汇总收入和工时，再计算加权时薪，零总工时返回NULL。"


def sql_response(sql):
    return json.dumps({"sql": sql, "explanation": "按批准的实际字段计算。"}, ensure_ascii=False)


def plan_response():
    return json.dumps({"steps": [{"title": "收入工时", "question": "按地点汇总收入及工时"},
                                  {"title": "加权时薪", "question": "计算加权平均时薪，零总工时NULL"}]}, ensure_ascii=False)


class SQLPromptCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.frame = load_demo_frame()
        self.http = patch("requests.sessions.Session.request", side_effect=AssertionError("禁止真实API"))
        self.http.start()
        self.addCleanup(self.http.stop)

    def test_supported_structures(self):
        queries = [TOTALS, WEIGHTED, "SELECT SUM(income)/SUM(hours) FROM uploaded_data",
                   "SELECT SUM(income), AVG(hours), COUNT(*) FROM uploaded_data WHERE income >= 0",
                   "SELECT t.total FROM (SELECT SUM(income) AS total FROM uploaded_data) t",
                   "SELECT a.location FROM uploaded_data a LEFT JOIN uploaded_data b ON a.location=b.location",
                   "SELECT location FROM uploaded_data GROUP BY location HAVING SUM(hours) <> 0 ORDER BY location"]
        for query in queries:
            with self.subTest(query=query):
                self.assertTrue(validate_proposal_sql(query, self.frame))

    def test_rejected_structures_have_only_safe_node_names(self):
        queries = [
            ("SELECT CASE WHEN SUM(hours)=0 THEN NULL ELSE SUM(income)/SUM(hours) END FROM uploaded_data", "Case"),
            ("SELECT SUM(income)/NULLIF(SUM(hours),0) FROM uploaded_data", "Nullif"),
            ("SELECT CAST(income AS DOUBLE) FROM uploaded_data", "Cast"),
            ("WITH x AS (SELECT * FROM uploaded_data) SELECT * FROM x", "With"),
            ("SELECT SUM(income) OVER () FROM uploaded_data", "Window"),
            ("SELECT SUM(income) FILTER (WHERE hours>0) FROM uploaded_data", "Filter"),
        ]
        for query, expected in queries:
            with self.subTest(node=expected):
                with self.assertRaises(SQLAnalysisError) as raised:
                    validate_sql(query)
                self.assertEqual(raised.exception.ast_node, expected)
                self.assertEqual(raised.exception.ast_reason, "UNSUPPORTED_NODE")
                self.assertEqual(raised.exception.category, "SQL_AST_REJECTED")

    def test_prompt_contains_exact_validated_template_and_no_input_rows(self):
        self.assertIn(WEIGHTED, GENERATION_PROMPT)
        provider = Mock(last_metrics=None)
        provider.complete_json.return_value = sql_response(WEIGHTED)
        proposal = generate_sql(self.frame, "按地点计算加权时薪", provider, confirmed=True)
        self.assertTrue(proposal.sql)
        sent = json.loads(provider.complete_json.call_args.args[0][1]["content"])
        self.assertEqual(set(sent), {"table", "columns", "question"})
        self.assertNotIn("rows", sent)
        self.assertNotIn("400", json.dumps(sent))
        self.assertIn("不引用第一步临时表", planning_messages(self.frame, GOAL)[0]["content"])

    def test_exact_two_steps_and_per_step_approval(self):
        provider = Mock(last_metrics=None)
        provider.complete_json.side_effect = [plan_response(), sql_response(TOTALS), sql_response(WEIGHTED)]
        runner = AgentRunner(self.frame, "demo", GOAL, provider, RequestBudget())
        runner.plan(dataset_id="demo", command_id="plan", confirmed=True)
        runner.approve_plan(dataset_id="demo", command_id="approve", confirmed=True)
        for i in range(2):
            draft = runner.generate_step(dataset_id="demo", command_id=f"gen{i}", confirmed=True)
            with self.assertRaises(AgentError):
                runner.execute_step(dataset_id="demo", command_id=f"exec{i}", proposal_id=draft.proposal_id)
            snapshot = runner.execute_step(dataset_id="demo", command_id=f"exec{i}",
                                           proposal_id=draft.proposal_id, confirmed=True)
        self.assertEqual(snapshot.state, AgentState.COMPLETED)
        self.assertEqual(provider.complete_json.call_count, 3)
        self.assertEqual(list(snapshot.results[0].data.itertuples(index=False, name=None)),
                         [("A", 400, 12), ("B", 400, 10), ("C", 90, 0), ("D", 0, 0)])
        result = snapshot.results[1].data
        self.assertEqual(list(result.columns), ["location", "weighted_avg_hourly"])
        self.assertEqual(result.location.tolist(), ["A", "B", "C", "D"])
        self.assertTrue(math.isclose(result.iloc[0, 1], 400/12, rel_tol=1e-12))
        self.assertEqual(result.iloc[1, 1], 40)
        self.assertTrue(result.iloc[2:, 1].isna().all())

    def test_null_location_null_hours_and_cancelling_hours_preserved(self):
        frame = pd.DataFrame({"location": [None, None, "Z", "Z", "N"],
                              "income": [60, 40, 50, 50, 20], "hours": [2, 3, 2, -2, None]})
        result = run_query(frame, validate_proposal_sql(WEIGHTED, frame)).data
        self.assertEqual(len(result), 3)
        self.assertEqual(result.loc[result.location.isna(), "weighted_avg_hourly"].iloc[0], 20)
        self.assertTrue(result.loc[result.location.isin(["Z", "N"]), "weighted_avg_hourly"].isna().all())

    def test_rejected_node_survives_agent_audit_without_candidate_sql(self):
        private_query = "SELECT CASE WHEN hours=0 THEN 'private-marker' ELSE 'other' END FROM uploaded_data"
        provider = Mock(last_metrics=None)
        provider.complete_json.side_effect = [plan_response(), sql_response(TOTALS), sql_response(private_query)]
        runner = AgentRunner(self.frame, "demo", GOAL, provider, RequestBudget())
        runner.plan(dataset_id="demo", command_id="plan", confirmed=True)
        runner.approve_plan(dataset_id="demo", command_id="approve", confirmed=True)
        draft = runner.generate_step(dataset_id="demo", command_id="gen1", confirmed=True)
        runner.execute_step(dataset_id="demo", command_id="exec1", proposal_id=draft.proposal_id, confirmed=True)
        with self.assertRaises(AgentError):
            runner.generate_step(dataset_id="demo", command_id="gen2", confirmed=True)
        snapshot = runner.snapshot()
        self.assertEqual(snapshot.failure_detail.ast_node, "Case")
        self.assertEqual(snapshot.failure_detail.ast_reason, "UNSUPPORTED_NODE")
        event = next(e for e in reversed(snapshot.audit) if e.kind == "command_failed")
        self.assertEqual((event.step, event.stage, event.category), (2, "SQL安全校验", "SQL_AST_REJECTED"))
        self.assertEqual((event.ast_node, event.ast_reason), ("Case", "UNSUPPORTED_NODE"))
        self.assertFalse(event.sql_submitted)
        self.assertEqual(event.model_requests_consumed, 1)
        self.assertNotIn("private-marker", repr(snapshot))
        self.assertEqual(len(snapshot.sql_history), 1)

    def test_unknown_diagnostic_text_is_dropped(self):
        self.assertEqual(safe_ast_metadata("SELECT private-marker", "private-marker"), (None, None))
        error = AIAnalysisError("安全提示", ast_node="private-marker", ast_reason="private-marker")
        self.assertIsNone(error.ast_node)
        self.assertIsNone(error.ast_reason)

    def test_parser_warning_does_not_echo_sql(self):
        with self.assertLogs("sqlglot", level="WARNING") as captured:
            with self.assertRaises(SQLAnalysisError):
                validate_sql("LOAD 'private-marker.sql'")
        self.assertNotIn("private-marker", repr(captured.output))


if __name__ == "__main__":
    unittest.main()
