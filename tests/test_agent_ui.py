"""Agent Beta Streamlit 端到端验收：云端 Provider 全部 Mock，禁止 HTTP。"""

from contextlib import ExitStack
from io import BytesIO
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from src.agent_runner import AgentState, AgentError
from src.agent_ui import _ACTIVE_PROVIDER
from src.llm_provider import CallMetrics, LLMConfig, LLMError, RequestBudget
from src.sql_analysis import QueryResult, SQLAnalysisError, run_query


APP = str(Path(__file__).resolve().parents[1] / "app.py")
FAKE_KEY = "mock-beta-credential-not-real"
GOAL = "分析不同地点收入与工时并检查缺失值"
PLAN_CONSENT = "我确认发送上述目标和字段结构用于规划"
STEP_CONSENT = "我确认发送当前步骤的字段与分析问题"
SQL_CONSENT = "我已审核当前 SQL，批准本步骤在本机执行"
REPORT_CONSENT = "我单独确认将上述有限结果发送给模型生成报告"


def button(app, label):
    return next(item for item in app.button if item.label == label)


def checkbox(app, label):
    return next(item for item in app.checkbox if item.label == label)


def area(app, label):
    return next(item for item in app.text_area if item.label == label)


def sql_output(query):
    return json.dumps({"sql": query, "explanation": "按已批准步骤计算。"}, ensure_ascii=False)


def plan_output(count=3):
    return json.dumps({"steps": [{"title": title, "question": question} for title, question in
                                 [("统计记录数", "共有多少条记录"),
                                  ("汇总地点收入", "按 location 汇总 income"),
                                  ("检查缺失值", "统计 location 缺失的记录数")][:count]]}, ensure_ascii=False)


class Sample:
    name = "虚构演示.csv"
    content = b"location,income,hours\nA,100,5\nB,80,4\n,20,0"

    def getvalue(self):
        return self.content


class AgentUITests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.sample = Sample()
        self.upload = self.stack.enter_context(patch("streamlit.file_uploader", return_value=self.sample))
        self.config = self.stack.enter_context(patch("src.agent_ui.LLMConfig.from_environment",
                                                    return_value=LLMConfig(FAKE_KEY)))
        self.provider = Mock()
        self.provider.last_metrics = None
        self.outputs = [plan_output(), sql_output("SELECT COUNT(*) AS total FROM uploaded_data"),
                        sql_output("SELECT location, SUM(income) AS total FROM uploaded_data GROUP BY location ORDER BY total DESC"),
                        sql_output("SELECT COUNT(*) AS missing FROM uploaded_data WHERE location IS NULL"),
                        json.dumps({"facts": [{"kind": "cell", "row": 0, "column": "值"}],
                                    "inferences": ["insufficient_context"], "suggestions": ["review_missing_values"]})]
        self.provider.complete_json.side_effect = self.outputs
        self.factory = self.stack.enter_context(patch("src.agent_ui.OpenAICompatibleProvider", return_value=self.provider))
        self.single_provider = Mock(last_metrics=None)
        self.single_provider.complete_json.return_value = sql_output("SELECT COUNT(*) AS total FROM uploaded_data")
        self.stack.enter_context(patch("src.ai_ui.OpenAICompatibleProvider", return_value=self.single_provider))
        self.executor = self.stack.enter_context(patch("src.ai_analysis.run_query",
                                                      return_value=QueryResult(pd.DataFrame({"total": [3]}), False)))
        self.stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Beta 测试禁止联网")))

    def start(self):
        app = AppTest.from_file(APP, default_timeout=15).run()
        app.toggle[0].set_value(True).run()
        area(app, "Agent 多步骤分析目标").set_value(GOAL).run()
        self.assertFalse(app.exception)
        return app

    def plan(self, app):
        checkbox(app, PLAN_CONSENT).check().run()
        button(app, "生成 Agent 分析计划").click().run()
        self.assertFalse(app.exception)
        return app

    def approve(self, app):
        button(app, "批准 Agent 完整计划").click().run()
        self.assertFalse(app.exception)
        return app

    def draft(self, app):
        checkbox(app, STEP_CONSENT).check().run()
        button(app, "生成当前步骤 SQL").click().run()
        self.assertFalse(app.exception)
        return app

    def execute(self, app):
        checkbox(app, SQL_CONSENT).check().run()
        button(app, "执行当前步骤 SQL").click().run()
        self.assertFalse(app.exception)
        return app

    def complete(self, app):
        self.approve(self.plan(app))
        for _ in range(3):
            self.execute(self.draft(app))
        return app

    def state(self, app):
        return app.session_state["agent_task"].snapshot()

    def visible(self, app):
        return "\n".join(str(item.value) for group in
                         (app.text, app.caption, app.info, app.warning, app.error, app.success) for item in group)

    def test_v41_stale_session_dataset_discard_is_safe(self):
        app = self.plan(self.start())
        runner = app.session_state["agent_task"]
        calls = self.provider.complete_json.call_count
        app.session_state["agent_task_dataset"] = "stale-dataset"
        self.sample.content += b"\nC,10,1"
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(runner.snapshot().state, AgentState.CANCELLED)
        self.assertIn("取消或拒绝操作未获确认", self.visible(app))
        self.assertEqual(self.provider.complete_json.call_count, calls)
        self.executor.assert_not_called()

    def test_v41_cancel_expected_error_is_safe_and_not_success(self):
        app = self.plan(self.start())
        runner = app.session_state["agent_task"]
        calls = self.provider.complete_json.call_count
        with patch.object(runner, "cancel", side_effect=AgentError("sensitive-cancel-body")):
            button(app, "取消 Agent 任务").click().run()
        self.assertFalse(app.exception)
        self.assertIn("取消或拒绝操作未获确认", self.visible(app))
        self.assertNotIn("sensitive-cancel-body", self.visible(app))
        self.assertEqual(runner.snapshot().state, AgentState.AWAITING_APPROVAL)
        self.assertEqual(self.provider.complete_json.call_count, calls)
        self.executor.assert_not_called()

    def test_v41_reject_expected_error_is_safe(self):
        app = self.plan(self.start())
        runner = app.session_state["agent_task"]
        with patch.object(runner, "approve_plan", side_effect=AgentError("sensitive-reject-body")):
            button(app, "拒绝计划并取消任务").click().run()
        self.assertFalse(app.exception)
        self.assertIn("取消或拒绝操作未获确认", self.visible(app))
        self.assertNotIn("sensitive-reject-body", self.visible(app))
        self.assertEqual(runner.snapshot().state, AgentState.AWAITING_APPROVAL)
        self.executor.assert_not_called()

    def test_v41_report_preview_exception_has_safe_message(self):
        app = self.plan(self.start())
        runner = app.session_state["agent_task"]
        with patch.object(runner, "preview_report", side_effect=ValueError("sensitive-preview-body")):
            self.approve(app)
            for _ in range(3):
                self.execute(self.draft(app))
        self.assertFalse(app.exception)
        self.assertIn("报告预览无法安全生成", self.visible(app))
        self.assertNotIn("sensitive-preview-body", self.visible(app))
        self.assertEqual(self.provider.complete_json.call_count, 4)
        self.assertFalse(any(item.label == "生成 DeepSeek 分析报告" for item in app.button))

    def test_v41_empty_report_step_scope_visible(self):
        self.executor.side_effect = [QueryResult(pd.DataFrame({"n": [1]}), False),
                                    QueryResult(pd.DataFrame({"n": []}), False),
                                    QueryResult(pd.DataFrame({"n": []}), False)]
        app = self.complete(self.start())
        self.assertFalse(app.exception)
        self.assertIn("步骤2查询成功但返回0行", self.visible(app))
        self.assertIn("步骤3查询成功但返回0行", self.visible(app))
        self.assertIn("未包含在发送内容中", self.visible(app))
        self.assertEqual(self.provider.complete_json.call_count, 4)

    def test_three_steps_real_safe_executor_and_optional_report(self):
        with patch("src.ai_analysis.run_query", wraps=run_query) as executor:
            app = self.complete(self.start())
            self.assertEqual(executor.call_count, 3)
        snapshot = self.state(app)
        self.assertEqual(snapshot.state, AgentState.COMPLETED)
        self.assertEqual(snapshot.results[0].data.iloc[0, 0], 3)
        self.assertEqual(snapshot.results[1].data["total"].tolist(), [100, 80, 20])
        self.assertEqual(snapshot.results[2].data.iloc[0, 0], 1)
        self.assertTrue(any(item.value == "最终本地事实报告" for item in app.markdown))
        self.assertTrue(button(app, "生成 DeepSeek 分析报告").disabled)
        self.assertEqual(self.provider.complete_json.call_count, 4)
        checkbox(app, REPORT_CONSENT).check().run()
        button(app, "生成 DeepSeek 分析报告").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.provider.complete_json.call_count, 5)
        self.assertTrue(self.state(app).report.facts)
        for title in ("已校验的数据事实", "模型推断（尚未证实）", "建议（不是统计结论）"):
            self.assertTrue(any(item.value == title for item in app.markdown))

    def test_no_planning_without_external_consent(self):
        app = self.start()
        self.assertTrue(button(app, "生成 Agent 分析计划").disabled)
        app.run()
        self.provider.complete_json.assert_not_called()
        self.factory.assert_not_called()

    def test_plan_approval_does_not_generate_or_execute(self):
        app = self.plan(self.start())
        self.assertFalse(any(item.label == "生成当前步骤 SQL" for item in app.button))
        app.run()
        self.assertEqual(self.provider.complete_json.call_count, 1)
        self.executor.assert_not_called()
        self.approve(app)
        self.assertTrue(button(app, "生成当前步骤 SQL").disabled)
        self.executor.assert_not_called()

    def test_sql_without_approval_never_executes(self):
        app = self.draft(self.approve(self.plan(self.start())))
        self.assertTrue(button(app, "执行当前步骤 SQL").disabled)
        app.run()
        self.executor.assert_not_called()
        self.assertEqual(self.provider.complete_json.call_count, 2)

    def test_each_step_has_fresh_sql_approval(self):
        app = self.draft(self.approve(self.plan(self.start())))
        previous = checkbox(app, SQL_CONSENT).key
        self.execute(app)
        self.assertFalse(checkbox(app, STEP_CONSENT).value)
        self.draft(app)
        self.assertNotEqual(previous, checkbox(app, SQL_CONSENT).key)
        self.assertFalse(checkbox(app, SQL_CONSENT).value)
        self.assertTrue(button(app, "执行当前步骤 SQL").disabled)
        self.assertEqual(self.executor.call_count, 1)

    def test_report_no_authorization_no_data_send_and_exact_preview(self):
        app = self.complete(self.start())
        runner = app.session_state["agent_task"]
        packet = runner.preview_report(dataset_id=app.session_state["agent_task_dataset"])
        self.assertIn(packet.json_text, self.visible(app))
        app.run()
        self.assertEqual(self.provider.complete_json.call_count, 4)
        checkbox(app, REPORT_CONSENT).check().run()
        button(app, "生成 DeepSeek 分析报告").click().run()
        self.assertEqual(self.provider.complete_json.call_args.args[0][1]["content"], packet.json_text)

    def test_rerun_and_duplicated_click_keep_calls_and_execution(self):
        app = self.draft(self.approve(self.plan(self.start())))
        runner = app.session_state["agent_task"]
        task_id = self.state(app).task_id
        for _ in range(2):
            app.run()
        self.assertEqual(self.provider.complete_json.call_count, 2)
        self.assertEqual(self.state(app).task_id, task_id)
        draft = self.state(app)
        self.execute(app)
        # 模拟迟到的相同命令事件，稳定的命令标识使执行器仍只调用一次。
        runner.execute_step(dataset_id=app.session_state["agent_task_dataset"],
                            command_id="execute-" + draft.proposal_id,
                            proposal_id=draft.proposal_id, confirmed=True)
        app.run()
        self.assertEqual(self.executor.call_count, 1)
        self.assertEqual(self.provider.complete_json.call_count, 2)

    def test_dataset_change_cancels_even_when_agent_closed(self):
        app = self.draft(self.approve(self.plan(self.start())))
        old = app.session_state["agent_task"]
        app.toggle[0].set_value(False).run()
        self.sample.content = b"new_field\n42"
        app.run()
        self.assertEqual(old.snapshot().state, AgentState.CANCELLED)
        self.assertNotIn("agent_task", app.session_state)
        self.executor.assert_not_called()

    def test_upload_removed_or_invalidated_cancels_task(self):
        for content in (None, b""):
            with self.subTest(content=content):
                self.upload.return_value = self.sample
                self.sample.content = Sample.content
                self.provider.complete_json.side_effect = [plan_output()]
                app = self.plan(self.start())
                old = app.session_state["agent_task"]
                if content is None:
                    self.upload.return_value = None
                else:
                    self.sample.content = content
                app.run()
                self.assertFalse(app.exception)
                self.assertEqual(old.snapshot().state, AgentState.CANCELLED)
                self.assertNotIn("agent_task", app.session_state)

    def test_filename_only_change_invalidates_task(self):
        app = self.plan(self.start())
        old = app.session_state["agent_task"]
        self.sample.name = "另一份虚构文件.csv"
        app.run()
        self.assertEqual(old.snapshot().state, AgentState.CANCELLED)
        self.assertFalse(checkbox(app, PLAN_CONSENT).value)

    def test_goal_edit_and_restore_never_restores_old_approval(self):
        app = self.draft(self.approve(self.plan(self.start())))
        old = app.session_state["agent_task"]
        area(app, "Agent 多步骤分析目标").set_value("新目标").run()
        self.assertEqual(old.snapshot().state, AgentState.CANCELLED)
        area(app, "Agent 多步骤分析目标").set_value(GOAL).run()
        self.assertFalse(checkbox(app, PLAN_CONSENT).value)
        self.assertNotIn("agent_task", app.session_state)
        self.executor.assert_not_called()

    def test_cancel_persists_on_rerun_and_requires_new_task(self):
        app = self.draft(self.approve(self.plan(self.start())))
        button(app, "取消 Agent 任务").click().run()
        for _ in range(2):
            app.run()
        self.assertEqual(self.state(app).state, AgentState.CANCELLED)
        self.assertFalse(any(item.label == "执行当前步骤 SQL" for item in app.button))
        self.executor.assert_not_called()
        button(app, "新建 Agent 任务（重新授权）").click().run()
        self.assertFalse(checkbox(app, PLAN_CONSENT).value)
        self.assertEqual(self.provider.complete_json.call_count, 2)

    def test_reject_plan_cancels_without_sql(self):
        app = self.plan(self.start())
        button(app, "拒绝计划并取消任务").click().run()
        self.assertEqual(self.state(app).state, AgentState.CANCELLED)
        self.assertEqual(self.provider.complete_json.call_count, 1)
        self.executor.assert_not_called()

    def test_safe_http_error_classification(self):
        for status in (400, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                self.provider.complete_json.side_effect = LLMError(FAKE_KEY + " raw-body", status_code=status)
                app = self.plan(self.start())
                self.assertEqual(self.state(app).state, AgentState.FAILED)
                self.assertIn(f"HTTP {status}", self.visible(app))
                self.assertNotIn(FAKE_KEY, self.visible(app))
                self.assertNotIn("raw-body", self.visible(app))

    def test_timeout_category_and_no_retry(self):
        self.provider.complete_json.side_effect = LLMError("private timeout text", category="READ_TIMEOUT")
        app = self.plan(self.start())
        app.run()
        self.assertIn("READ_TIMEOUT", self.visible(app))
        self.assertIn("超时", self.visible(app))
        self.assertEqual(self.provider.complete_json.call_count, 1)

    def test_unsafe_sql_and_wrong_field_cannot_reach_executor(self):
        for query in ("DROP TABLE uploaded_data", "SELECT * FROM read_csv('private.csv')",
                      "SELECT absent FROM uploaded_data"):
            with self.subTest(query=query):
                self.provider.complete_json.side_effect = [plan_output(), sql_output(query)]
                app = self.draft(self.approve(self.plan(self.start())))
                self.assertEqual(self.state(app).state, AgentState.FAILED)
                self.executor.assert_not_called()
                self.assertFalse(any(item.label == "执行当前步骤 SQL" for item in app.button))

    def test_illegal_model_json_fails_without_execution(self):
        for output in ("not JSON", '{"steps": []}', '{"steps": [{"title":"x","question":"x","code":"x"}]}'):
            with self.subTest(output=output):
                self.provider.complete_json.side_effect = [output]
                app = self.plan(self.start())
                self.assertEqual(self.state(app).state, AgentState.FAILED)
                self.executor.assert_not_called()

    def test_exhausted_session_budget_blocks_provider(self):
        app = self.start()
        app.session_state["ai_api_budget"] = RequestBudget(limit=1, used=1)
        self.plan(app)
        self.assertEqual(self.state(app).state, AgentState.FAILED)
        self.assertIn("预算已耗尽", self.visible(app))
        self.provider.complete_json.assert_not_called()

    def test_token_usage_and_shared_single_analysis_budget(self):
        app = self.start()
        area(app, "分析问题（中文或英文）").set_value("共有多少条记录").run()
        checkbox(app, "我确认将上述字段与当前问题发送给模型服务").check().run()
        button(app, "生成 SQL").click().run()
        self.assertEqual(app.session_state["ai_api_budget"].used, 1)
        self.provider.last_metrics = CallMetrics(0.15, {"prompt_tokens": 12, "completion_tokens": 7})
        self.plan(app)
        snapshot = self.state(app)
        self.assertEqual(snapshot.session_requests, 2)
        self.assertEqual(snapshot.calls[0].usage, {"prompt_tokens": 12, "completion_tokens": 7})
        self.assertIn('"prompt_tokens":12', self.visible(app))
        self.assertIs(app.session_state["agent_task"]._budget, app.session_state["ai_api_budget"])

    def test_null_partial_report_and_missing_values_display(self):
        self.executor.return_value = QueryResult(pd.DataFrame({"total": [None]}), True)
        app = self.complete(self.start())
        self.assertIn("结果已截断", self.visible(app))
        self.assertIn("NULL 单元格 1 个", self.visible(app))
        self.assertIn("源数据缺失值合计：1", self.visible(app))
        self.assertIn("报告仅基于已发送的部分结果", self.visible(app))
        checkbox(app, REPORT_CONSENT).check().run()
        button(app, "生成 DeepSeek 分析报告").click().run()
        self.assertIn("null", self.visible(app))
        self.assertTrue(self.state(app).report.partial)

    def test_precise_metadata_preview_and_no_secret_in_task(self):
        app = self.start()
        preview = next(item.value for item in app.text if '"role":"system"' in item.value)
        self.plan(app)
        self.assertEqual(json.loads(preview), self.provider.complete_json.call_args.args[0])
        payload = json.loads(json.loads(preview)[1]["content"])
        self.assertEqual(set(payload), {"table", "columns", "question"})
        self.assertNotIn("虚构演示.csv", preview)
        runner = app.session_state["agent_task"]
        self.assertNotIn(FAKE_KEY, repr(vars(runner)))
        self.assertEqual(vars(runner._provider), {})
        self.assertIsNone(_ACTIVE_PROVIDER.get())

    def test_single_ai_workflow_is_independent_and_still_executes(self):
        app = self.plan(self.start())
        agent_id = self.state(app).task_id
        area(app, "分析问题（中文或英文）").set_value("记录数").run()
        checkbox(app, "我确认将上述字段与当前问题发送给模型服务").check().run()
        button(app, "生成 SQL").click().run()
        checkbox(app, "我已检查上述 SQL，确认在本机执行").check().run()
        button(app, "确认执行生成的 SQL").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.state(app).task_id, agent_id)
        self.assertEqual(self.state(app).approval, "plan")
        self.executor.assert_called_once()
        self.assertEqual(self.provider.complete_json.call_count, 1)
        self.assertEqual(self.single_provider.complete_json.call_count, 1)

    def test_sql_timeout_stops_and_does_not_offer_report(self):
        app = self.draft(self.approve(self.plan(self.start())))
        self.executor.side_effect = SQLAnalysisError("查询执行超时。")
        self.execute(app)
        self.assertEqual(self.state(app).state, AgentState.FAILED)
        self.assertFalse(any(item.label == "生成 DeepSeek 分析报告" for item in app.button))
        self.assertIn("SQL 查询超时", self.visible(app))
        self.assertEqual(self.provider.complete_json.call_count, 2)

    def test_model_configuration_change_invalidates_task(self):
        app = self.plan(self.start())
        old = app.session_state["agent_task"]
        self.config.return_value = LLMConfig(FAKE_KEY, model="deepseek-mock-changed")
        app.run()
        self.assertEqual(old.snapshot().state, AgentState.CANCELLED)
        self.assertFalse(checkbox(app, PLAN_CONSENT).value)

    def test_excel_sheet_change_invalidates_old_approval(self):
        buffer = BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            pd.DataFrame({"location": ["A"], "income": [100]}).to_excel(writer, sheet_name="甲表", index=False)
            pd.DataFrame({"location": ["B"], "income": [80]}).to_excel(writer, sheet_name="乙表", index=False)
        self.sample.name = "虚构演示.xlsx"
        self.sample.content = buffer.getvalue()
        app = self.draft(self.approve(self.plan(self.start())))
        old = app.session_state["agent_task"]
        next(item for item in app.selectbox if item.label == "选择 Excel 工作表").select("乙表").run()
        self.assertFalse(app.exception)
        self.assertEqual(old.snapshot().state, AgentState.CANCELLED)
        self.assertNotIn("agent_task", app.session_state)
        self.executor.assert_not_called()

    def test_missing_configuration_keeps_local_features(self):
        self.config.return_value = None
        app = AppTest.from_file(APP).run()
        app.toggle[0].set_value(True).run()
        self.assertFalse(app.exception)
        self.assertIn("Agent 模型未配置", self.visible(app))
        self.assertTrue(any(item.label == "执行 SQL 查询" for item in app.button))
        self.provider.complete_json.assert_not_called()
        self.factory.assert_not_called()

    def test_real_config_fields_open_both_pages_without_derived_property(self):
        # 配置为生产 dataclass 实例，不是 Mock 或临时伪配置类。
        config = LLMConfig(FAKE_KEY, token_parameter="max_tokens")
        self.assertIs(type(config), LLMConfig)
        self.config.return_value = config
        # 模拟缺少派生属性的已加载配置类；退出上下文恢复生产定义。
        with patch.object(LLMConfig, "request_token_parameter"):
            delattr(LLMConfig, "request_token_parameter")
            app = self.start()
            self.assertFalse(app.exception)
            self.assertEqual(area(app, "Agent 多步骤分析目标").value, GOAL)
            self.assertFalse(checkbox(app, PLAN_CONSENT).value)
            self.assertTrue(button(app, "生成 Agent 分析计划").disabled)
            self.assertTrue(any(item.label == "分析问题（中文或英文）" for item in app.text_area))
            app.run()
            self.assertFalse(app.exception)
        self.provider.complete_json.assert_not_called()
        self.single_provider.complete_json.assert_not_called()

    def test_declared_protocol_settings_invalidate_agent_fingerprint(self):
        for settings in ({"token_parameter": "max_tokens"}, {"json_mode": "prompt"},
                         {"max_output_tokens": 512}):
            with self.subTest(settings=settings):
                self.config.return_value = LLMConfig(FAKE_KEY)
                self.provider.complete_json.side_effect = [plan_output()]
                app = self.plan(self.start())
                old = app.session_state["agent_task"]
                self.config.return_value = LLMConfig(FAKE_KEY, **settings)
                app.run()
                self.assertFalse(app.exception)
                self.assertEqual(old.snapshot().state, AgentState.CANCELLED)
                self.assertFalse(checkbox(app, PLAN_CONSENT).value)

    def test_failed_report_retains_real_local_results(self):
        app = self.complete(self.start())
        self.provider.complete_json.side_effect = ["invalid JSON"]
        checkbox(app, REPORT_CONSENT).check().run()
        button(app, "生成 DeepSeek 分析报告").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.state(app).state, AgentState.FAILED)
        self.assertIsNone(self.state(app).report)
        self.assertEqual(len(self.state(app).results), 3)
        self.assertTrue(any(item.value == "最终本地事实报告" for item in app.markdown))
        self.assertEqual(self.provider.complete_json.call_count, 5)
        self.assertFalse(any(item.label == "生成 DeepSeek 分析报告" for item in app.button))

    def test_two_step_plan_completes_without_extra_sql(self):
        self.provider.complete_json.side_effect = [plan_output(2), self.outputs[1], self.outputs[2]]
        app = self.start()
        area(app, "Agent 多步骤分析目标").set_value("请用两个SQL步骤分析收入和工时").run()
        self.approve(self.plan(app))
        for _ in range(2):
            self.execute(self.draft(app))
        self.assertEqual(self.state(app).state, AgentState.COMPLETED)
        self.assertEqual(self.provider.complete_json.call_count, 3)
        self.assertEqual(self.executor.call_count, 2)
        self.assertTrue(button(app, "生成 DeepSeek 分析报告").disabled)

    def test_second_step_failure_diagnostics_visible_and_no_more_execution(self):
        for output, category, stage in (
            ("invalid private model body", "SQL_GENERATION_FORMAT", "SQL生成格式校验"),
            (sql_output("SELECT CASE WHEN SUM(hours)=0 THEN NULL ELSE SUM(income)/SUM(hours) END FROM uploaded_data"),
             "SQL_AST_REJECTED", "SQL安全校验"),
            (LLMError("private-content", category="MODEL_JSON_INVALID"), "SQL_GENERATION_FORMAT", "SQL生成格式校验"),
            (LLMError("private-content", status_code=500), "SERVER_ERROR", "SQL生成"),
        ):
            with self.subTest(category=category, stage=stage):
                self.executor.reset_mock()
                self.provider.complete_json.reset_mock()
                self.provider.complete_json.side_effect = [plan_output(2), self.outputs[1], output]
                app = self.start()
                area(app, "Agent 多步骤分析目标").set_value("只用两个 SQL 步骤").run()
                self.execute(self.draft(self.approve(self.plan(app))))
                self.draft(app)
                self.assertEqual(self.state(app).state, AgentState.FAILED)
                visible = self.visible(app)
                self.assertIn("失败步骤：2", visible)
                self.assertIn("失败阶段：" + stage, visible)
                self.assertIn("安全错误类别：" + category, visible)
                if category == "SQL_AST_REJECTED":
                    self.assertIn("安全 AST 节点：Case", visible)
                    self.assertIn("UNSUPPORTED_NODE", visible)
                self.assertIn("未执行（未提交安全执行器）", visible)
                self.assertIn("本操作模型请求：1 次", visible)
                self.assertNotIn("private", visible)
                self.assertEqual(self.executor.call_count, 1)
                self.assertEqual(self.state(app).session_requests, 3)
                app.run()
                self.assertEqual(self.provider.complete_json.call_count, 3)
                self.assertFalse(any(item.label == "执行当前步骤 SQL" for item in app.button))

    def test_extra_plan_step_rejection_visible(self):
        app = self.start()
        area(app, "Agent 多步骤分析目标").set_value("用两个SQL步骤完成分析").run()
        self.plan(app)
        self.assertEqual(self.state(app).failure_detail.category, "PLAN_STEP_COUNT_MISMATCH")
        self.assertIn("规划步骤校验", self.visible(app))
        self.assertIn("整份计划已拒绝", self.visible(app))
        self.assertFalse(any(item.label == "批准 Agent 完整计划" for item in app.button))
        self.executor.assert_not_called()

    def test_v401_readable_local_facts_without_another_model_call(self):
        totals = "SELECT location, SUM(income) AS total_income, SUM(hours) AS total_hours FROM uploaded_data GROUP BY location ORDER BY location"
        rates = '''SELECT t.location, t.income / d.hours AS weighted_avg_hourly
FROM (SELECT location, SUM(income) AS income FROM uploaded_data GROUP BY location) t
LEFT JOIN (SELECT location, SUM(hours) AS hours FROM uploaded_data GROUP BY location HAVING SUM(hours)<>0) d
ON t.location=d.location OR (t.location IS NULL AND d.location IS NULL) ORDER BY t.location'''
        self.provider.complete_json.side_effect = [plan_output(2), sql_output(totals), sql_output(rates)]
        self.executor.side_effect = [
            QueryResult(pd.DataFrame({"location": ["A", "B", "C", "D"], "total_income": [400, 400, 90, 0],
                                      "total_hours": [12, 10, 0, 0]}), False),
            QueryResult(pd.DataFrame({"location": ["A", "B", "C", "D"], "weighted_avg_hourly": [400/12, 40, None, None]}), False)]
        app = self.start()
        area(app, "Agent 多步骤分析目标").set_value("只用两个SQL步骤汇总并计算时薪").run()
        self.approve(self.plan(app))
        for _ in range(2):
            self.execute(self.draft(app))
        visible = self.visible(app)
        self.assertIn('地点（location）="A"', visible)
        self.assertIn("总收入（total_income）=400", visible)
        self.assertIn("总工时（total_hours）=12", visible)
        self.assertIn("weighted_avg_hourly）=33.33（显示约数", visible)
        self.assertIn("NULL（null）", visible)
        self.assertIn("步骤2，结果第1行", visible)
        self.assertNotIn("发送结果第1行，字段「值」", visible)
        self.assertEqual(self.provider.complete_json.call_count, 3)
        self.assertEqual(self.executor.call_count, 2)
        self.assertTrue(button(app, "生成 DeepSeek 分析报告").disabled)
        self.assertEqual(self.state(app).results[1].data.shape, (4, 2))
        app.run()
        self.assertEqual(self.provider.complete_json.call_count, 3)


if __name__ == "__main__":
    unittest.main()
