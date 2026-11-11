"""AI 页面端到端状态测试，模型响应全部 Mock。"""

from contextlib import ExitStack
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from src.llm_provider import CallMetrics, LLMConfig, LLMError, RequestBudget
from src.sql_analysis import QueryResult, SQLAnalysisError

APP = str(Path(__file__).resolve().parents[1] / "app.py")
FAKE_KEY = "mock-ui-secret-never-use"


class Sample:
    name = "私密文件名.csv"

    def getvalue(self):
        return "地点,收入\n私密原始样本,10\n乙,30".encode("utf-8")


def button(app, label):
    return next(item for item in app.button if item.label == label)


def checkbox(app, label):
    return next(item for item in app.checkbox if item.label == label)


class AIUITests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.sample = Sample()
        self.stack.enter_context(patch("streamlit.file_uploader", return_value=self.sample))
        self.config_reader = self.stack.enter_context(patch("src.ai_ui.LLMConfig.from_environment", return_value=LLMConfig(FAKE_KEY, "https://mock.invalid/v1", "mock-model")))
        self.provider = Mock()
        self.stack.enter_context(patch("src.ai_ui.OpenAICompatibleProvider", return_value=self.provider))
        self.runner = self.stack.enter_context(patch("src.ai_analysis.run_query", return_value=QueryResult(pd.DataFrame({"地点": ["乙"], "总收入": [30]}), False)))
        self.provider.complete_json.return_value = json.dumps({
            "sql": 'SELECT "地点", SUM("收入") AS "总收入" FROM uploaded_data GROUP BY "地点" ORDER BY "总收入" DESC LIMIT 1',
            "explanation": "按地点汇总收入并降序排列。",
        }, ensure_ascii=False)
        # 若业务代码不慎绕过 Mock provider 发起 HTTP，本测试会直接失败。
        self.stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("测试禁止联网")))

    def start(self):
        app = AppTest.from_file(APP).run()
        app.text_area[1].set_value("哪个地点总收入最高？").run()
        return app

    def generate(self, app):
        checkbox(app, "我确认将上述字段与当前问题发送给模型服务").check().run()
        button(app, "生成 SQL").click().run()
        return app

    def execute(self, app):
        checkbox(app, "我已检查上述 SQL，确认在本机执行").check().run()
        button(app, "确认执行生成的 SQL").click().run()
        return app

    def test_no_config_preserves_local_features(self):
        self.config_reader.return_value = None
        app = AppTest.from_file(APP).run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.dataframe), 2)
        self.assertTrue(any("未配置" in item.value for item in app.info))
        self.provider.complete_json.assert_not_called()

    def test_all_three_consents_and_rerun_persistence(self):
        app = self.start()
        self.assertTrue(button(app, "生成 SQL").disabled)
        self.provider.complete_json.assert_not_called()
        self.generate(app)
        self.assertFalse(app.exception)
        self.assertEqual(self.provider.complete_json.call_count, 1)
        self.runner.assert_not_called()
        self.assertTrue(button(app, "确认执行生成的 SQL").disabled)
        app.run()
        self.assertEqual(self.provider.complete_json.call_count, 1)
        self.execute(app)
        self.runner.assert_called_once()
        self.assertEqual(self.provider.complete_json.call_count, 1)
        self.assertTrue(button(app, "请求 AI 解释结果").disabled)
        app.run()
        self.runner.assert_called_once()
        self.assertEqual(self.provider.complete_json.call_count, 1)
        checkbox(app, "我单独确认将上述有限查询结果发送给模型解释").check().run()
        self.provider.complete_json.return_value = json.dumps({"facts": [{"kind": "cell", "row": 0, "column": "总收入"}], "inferences": ["insufficient_context"], "suggestions": []})
        button(app, "请求 AI 解释结果").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.provider.complete_json.call_count, 2)
        app.run()
        self.assertEqual(self.provider.complete_json.call_count, 2)
        self.runner.assert_called_once()
        state = app.session_state["ai_workflow"]
        self.assertNotIn(FAKE_KEY, repr(state))
        visible = [item.value for item in app.text] + [item.value for item in app.caption]
        self.assertNotIn(FAKE_KEY, repr(visible))

    def test_question_or_data_change_invalidates_state_and_consent(self):
        app = self.generate(self.start())
        self.execute(app)
        app.text_area[1].set_value("换个问题").run()
        self.assertFalse(any(item.label == "确认执行生成的 SQL" for item in app.button))
        self.assertFalse(checkbox(app, "我确认将上述字段与当前问题发送给模型服务").value)
        self.assertEqual(len(app.metric), 3)
        self.sample.getvalue = lambda: b"new_column\n1"
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_area[1].value, "")
        self.assertEqual(len(app.metric), 3)

    def test_execution_failure_never_explained(self):
        app = self.generate(self.start())
        self.runner.side_effect = SQLAnalysisError("测试：查询执行失败")
        self.execute(app)
        self.assertFalse(app.exception)
        self.assertTrue(any("执行失败" in item.value for item in app.error))
        self.assertFalse(any(item.label == "请求 AI 解释结果" for item in app.button))
        self.assertEqual(self.provider.complete_json.call_count, 1)
        self.assertEqual(len(app.metric), 3)

    def test_api_error_and_unsafe_sql_not_executed(self):
        app = self.start()
        self.provider.complete_json.side_effect = LLMError("模型认证失败")
        self.generate(app)
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.provider.complete_json.side_effect = None
        self.provider.complete_json.return_value = json.dumps({"sql": "SELECT * FROM read_csv('secret.csv')", "explanation": "错误输出"})
        button(app, "生成 SQL").click().run()
        self.assertFalse(app.exception)
        self.runner.assert_not_called()
        self.assertFalse(any(item.label == "确认执行生成的 SQL" for item in app.button))

    def test_new_execution_requires_fresh_result_disclosure_consent(self):
        app = self.execute(self.generate(self.start()))
        checkbox(app, "我单独确认将上述有限查询结果发送给模型解释").check().run()
        button(app, "确认执行生成的 SQL").click().run()
        self.assertFalse(checkbox(app, "我单独确认将上述有限查询结果发送给模型解释").value)
        self.assertEqual(self.runner.call_count, 2)
        self.assertEqual(self.provider.complete_json.call_count, 1)

    def test_metrics_and_budget_do_not_repeat_calls(self):
        self.provider.last_metrics = CallMetrics(0.123, None)
        app = self.generate(self.start())
        self.assertTrue(any("未返回有效 Token" in item.value for item in app.caption))
        self.assertEqual(app.session_state["ai_api_budget"].used, 1)
        app.run()
        self.assertEqual(app.session_state["ai_api_budget"].used, 1)
        self.provider.complete_json.assert_called_once()

    def test_session_budget_blocks_more_requests(self):
        app = self.start()
        app.session_state["ai_api_budget"] = RequestBudget(limit=1, used=1)
        app.run()
        self.generate(app)
        self.assertFalse(app.exception)
        self.assertTrue(any("上限" in item.value for item in app.error))
        self.provider.complete_json.assert_not_called()
        self.assertTrue(button(app, "重置本会话请求计数").disabled)


if __name__ == "__main__":
    unittest.main()
