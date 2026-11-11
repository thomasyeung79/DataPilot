"""可选真实云端冒烟：默认跳过；开启后最多两次请求，不使用真实个人数据。"""

import os
import unittest

from src.ai_analysis import AIAnalysisError, execute_proposal, generate_sql
from src.evaluation import CASES, evaluate_proposal, load_demo_frame
from src.llm_provider import BudgetedProvider, LLMConfig, OpenAICompatibleProvider, RequestBudget

ENABLE_PHRASE = "I_CONFIRM_SYNTHETIC_METADATA_SEND_AND_LOCAL_SQL_EXECUTION"


@unittest.skipUnless(os.environ.get("DATAPILOT_LIVE_API") == ENABLE_PHRASE,
                     "真实 API 默认跳过；需明确授权外发模拟元数据与本地 SQL 执行")
class LiveAPISmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = LLMConfig.from_environment()
        if config is None:
            raise RuntimeError("已开启真实测试但模型配置不完整；未发出请求。")
        cls.provider = BudgetedProvider(OpenAICompatibleProvider(config), RequestBudget(limit=2))

    def check_live_question(self, case, question):
        frame = load_demo_frame()
        # 即使测试被开启，业务层仍必须拒绝缺少显式授权的调用。
        before = self.provider.budget.used
        with self.assertRaises(AIAnalysisError):
            generate_sql(frame, question, self.provider)
        self.assertEqual(self.provider.budget.used, before)
        try:
            proposal = generate_sql(frame, question, self.provider, confirmed=True)
        finally:
            metrics = self.provider.last_metrics
            if metrics:
                usage = metrics.usage if metrics.usage is not None else "服务商未返回；不估算"
                print(f"真实冒烟 {case.name}：耗时 {metrics.elapsed_seconds:.3f}秒；Token={usage}")
        with self.assertRaises(AIAnalysisError):
            execute_proposal(frame, proposal)
        self.assertTrue(evaluate_proposal(frame, case, proposal, confirmed=True), "模型结果未达到模拟题预期；不会自动重试")

    def test_chinese_count(self):
        self.check_live_question(CASES[0], CASES[0].question)

    def test_english_weighted_rate(self):
        case = next(case for case in CASES if case.name == "weighted_rate")
        self.check_live_question(case, "For each location return location, total_income, total_hours, hourly_rate. "
            "Use SUM(income)/SUM(hours), round to 2 decimals, retain income on zero-hour rows, "
            "retain locations with zero total hours with NULL rates. Sort rates descending NULLS LAST, then location ascending. "
            "NULLIF and CASE are forbidden; use allowed aggregate subqueries and LEFT JOIN.")


if __name__ == "__main__":
    unittest.main()
