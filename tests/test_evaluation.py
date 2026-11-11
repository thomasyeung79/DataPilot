"""20 个模拟问题的离线结果评估，不代表真实模型准确率。"""

import json
import unittest
from unittest.mock import Mock

import pandas as pd

from src.ai_analysis import AIAnalysisError, SQLProposal, generate_sql
from src.evaluation import CASES, WEIGHTED_RATE_SQL, evaluate_proposal, load_demo_frame, result_matches
from src.sql_analysis import SQLAnalysisError, run_query, validate_sql


class EvaluationTests(unittest.TestCase):
    def test_weighted_rate_is_not_row_average(self):
        case = next(case for case in CASES if case.name == "weighted_rate")
        bad = SQLProposal("SELECT location, SUM(income), SUM(hours), ROUND(AVG(income/hours),2) FROM uploaded_data WHERE hours>0 GROUP BY location ORDER BY 4 DESC, location", "错误的逐行均值")
        self.assertFalse(evaluate_proposal(load_demo_frame(), case, bad, confirmed=True))

    def test_nullif_remains_forbidden(self):
        with self.assertRaises(SQLAnalysisError):
            validate_sql("SELECT ROUND(SUM(income)/NULLIF(SUM(hours),0),2) FROM uploaded_data")

    def test_null_location_and_zero_hours_are_preserved(self):
        frame = pd.DataFrame({"location": [None, None, "Z"], "income": [60, 40, 90], "hours": [2, 3, 0]})
        result = run_query(frame, WEIGHTED_RATE_SQL)
        self.assertTrue(result_matches(result.data, ((None, 100, 5, 20.0), ("Z", 90, 0, None))))

    def test_evaluation_requires_execution_consent(self):
        case = CASES[0]
        with self.assertRaises(AIAnalysisError):
            evaluate_proposal(load_demo_frame(), case, SQLProposal(case.reference_sql, "测试"))


def _make_case_test(case):
    def test(self):
        frame = load_demo_frame()
        if case.name == "clarify_month":
            frame = frame.drop(columns=["month"])
        provider = Mock()
        provider.complete_json.return_value = json.dumps({"sql": case.reference_sql,
            "explanation": "需澄清业务口径或补充月份字段。" if case.reference_sql is None else "使用已明确口径进行模拟评估。"})
        proposal = generate_sql(frame, case.question, provider, confirmed=True)
        self.assertTrue(evaluate_proposal(frame, case, proposal, confirmed=True), case.name)
    return test


for _case in CASES:
    setattr(EvaluationTests, "test_question_" + _case.name, _make_case_test(_case))


if __name__ == "__main__":
    unittest.main()
