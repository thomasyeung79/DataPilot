"""V3.1 响应兼容、实际用量与预算控制测试；不连接外部服务。"""

from dataclasses import replace
import json
import unittest
from unittest.mock import Mock, patch

import requests
import pandas as pd

from src.ai_analysis import build_generation_payload, build_explanation_packet, encode_json, explain_result, generate_sql
from src.llm_provider import BudgetedProvider, CallMetrics, LLMConfig, LLMError, OpenAICompatibleProvider, RequestBudget
from src.sql_analysis import QueryResult


class ProviderV31Tests(unittest.TestCase):
    def setUp(self):
        self.config = LLMConfig("mock-v31-not-real-credential", "https://mock.invalid/v1", "mock-model")
        self.provider = OpenAICompatibleProvider(self.config)
        self.messages = [{"role": "system", "content": "返回JSON"}, {"role": "user", "content": "{}"}]
        factory = patch("src.llm_provider.requests.Session")
        self.session = factory.start().return_value.__enter__.return_value
        self.addCleanup(factory.stop)
        self.response = self.session.post.return_value.__enter__.return_value
        self.response.status_code = 200

    def envelope(self, message=None, usage=None, finish=True):
        choice = {"message": message or {"content": '{"sql":null,"explanation":"澄清"}'}}
        if finish:
            choice["finish_reason"] = "stop"
        body = {"choices": [choice]}
        if usage is not None:
            body["usage"] = usage
        self.response.iter_content.return_value = [json.dumps(body).encode("utf-8")]

    def test_real_usage_fields_preserved_without_inventing_total(self):
        self.envelope(usage={"prompt_tokens": 12, "completion_tokens": 7, "ignored": "sensitive", "total_tokens": "19"})
        self.provider.complete_json(self.messages)
        self.assertEqual(self.provider.last_metrics.usage, {"prompt_tokens": 12, "completion_tokens": 7})
        self.assertGreaterEqual(self.provider.last_metrics.elapsed_seconds, 0)
        self.envelope(usage={"input_tokens": 4, "output_tokens": 3})
        self.provider.complete_json(self.messages)
        self.assertEqual(self.provider.last_metrics.usage, {"input_tokens": 4, "output_tokens": 3})

    def test_missing_and_invalid_usage_are_not_estimated(self):
        for usage in [None, {}, {"total_tokens": True}, {"prompt_tokens": -1}]:
            self.envelope(usage=usage)
            self.provider.complete_json(self.messages)
            self.assertIsNone(self.provider.last_metrics.usage)

    def test_text_parts_and_omitted_finish_reason(self):
        self.envelope(message={"content": [{"type": "text", "text": '{"sql":null,'},
                                           {"type": "text", "text": '"explanation":"澄清"}'}]}, finish=False)
        self.assertEqual(json.loads(self.provider.complete_json(self.messages))["explanation"], "澄清")
        self.envelope(message={"content": [{"type": "image_url", "image_url": "do-not-open"}]})
        with self.assertRaises(LLMError):
            self.provider.complete_json(self.messages)
        self.envelope(message={"content": '{"sql":null,"explanation":"澄清"}', "refusal": "refused"})
        with self.assertRaises(LLMError):
            self.provider.complete_json(self.messages)

    def test_explicit_prompt_json_mode_and_output_cap(self):
        self.envelope()
        provider = OpenAICompatibleProvider(replace(self.config, json_mode="prompt", max_output_tokens=256))
        provider.complete_json(self.messages)
        payload = self.session.post.call_args.kwargs["json"]
        self.assertNotIn("response_format", payload)
        self.assertEqual(payload["max_completion_tokens"], 256)
        self.assertEqual(self.session.post.call_count, 1)

    def test_network_timeout_server_error_and_metrics(self):
        for error in [requests.ConnectionError("secret-never-display"), requests.Timeout("secret-never-display")]:
            self.session.post.side_effect = error
            with self.assertRaises(LLMError) as raised:
                self.provider.complete_json(self.messages)
            self.assertNotIn("secret-never-display", str(raised.exception))
            self.assertIsNone(self.provider.last_metrics.usage)
        self.session.post.side_effect = None
        self.response.status_code = 503
        with self.assertRaisesRegex(LLMError, "服务器"):
            self.provider.complete_json(self.messages)

    def test_server_errors_with_http_200_fail_closed(self):
        self.response.iter_content.return_value = [b'{"error":{"message":"private"}}']
        with self.assertRaisesRegex(LLMError, "错误响应") as raised:
            self.provider.complete_json(self.messages)
        self.assertNotIn("private", str(raised.exception))

    def test_budget_includes_failed_attempts(self):
        inner = Mock()
        inner.complete_json.side_effect = LLMError("模拟失败")
        budget = RequestBudget(limit=1)
        provider = BudgetedProvider(inner, budget)
        for _ in range(2):
            with self.assertRaises(LLMError):
                provider.complete_json(self.messages)
        inner.complete_json.assert_called_once()
        self.assertEqual(budget.used, 1)

    def test_actual_http_payload_equals_preview_and_separate_consent(self):
        frame = pd.DataFrame({"income": [999999]})
        self.envelope(message={"content": '{"sql":"SELECT SUM(income) FROM uploaded_data","explanation":"合计"}'})
        preview = encode_json(build_generation_payload(frame, "收入合计？"))
        generate_sql(frame, "收入合计？", self.provider, confirmed=True)
        sent = self.session.post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertEqual(sent, preview)
        self.assertNotIn("999999", sent)
        self.session.post.reset_mock()
        packet = build_explanation_packet(QueryResult(pd.DataFrame({"total": [50]}), False))
        self.envelope(message={"content": '{"facts":[{"kind":"cell","row":0,"column":"total"}],"inferences":[],"suggestions":[]}'})
        from src.ai_analysis import AIAnalysisError
        with self.assertRaises(AIAnalysisError):
            explain_result(packet, self.provider)
        self.session.post.assert_not_called()
        explain_result(packet, self.provider, confirmed=True)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["messages"][1]["content"], packet.json_text)


if __name__ == "__main__":
    unittest.main()
