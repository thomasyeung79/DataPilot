"""DeepSeek 官方协议 Mock 验证：不读取真实配置、不调用真实 API。"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from src.ai_analysis import AIAnalysisError, build_generation_payload, encode_json, generate_sql
from src.llm_provider import BudgetedProvider, DEFAULT_BASE_URL, DEFAULT_MODEL, LLMConfig, LLMError, OpenAICompatibleProvider, RequestBudget

FAKE_KEY = "mock-deepseek-key-not-a-real-credential"


class DeepSeekProviderTests(unittest.TestCase):
    def setUp(self):
        factory = patch("src.llm_provider.requests.Session")
        self.session = factory.start().return_value.__enter__.return_value
        self.addCleanup(factory.stop)
        self.response = self.session.post.return_value.__enter__.return_value
        self.response.status_code = 200
        self.messages = [{"role": "system", "content": "请返回JSON"}, {"role": "user", "content": "{}"}]
        self.response_body('{"sql":"SELECT COUNT(*) FROM uploaded_data","explanation":"统计记录数"}')

    def response_body(self, content, finish="stop", **message_extra):
        body = {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content, **message_extra}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42, "prompt_cache_hit_tokens": 0}}
        self.response.iter_content.return_value = [json.dumps(body, ensure_ascii=False).encode("utf-8")]

    def test_default_configuration_from_key_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("DATAPILOT_API_KEY=" + FAKE_KEY + "\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                config = LLMConfig.from_environment(path)
        self.assertEqual(config.base_url, "https://api.deepseek.com")
        self.assertEqual(config.model, "deepseek-flash")
        self.assertEqual(config.token_parameter, "max_tokens")
        self.assertNotIn(FAKE_KEY, repr(config))
        self.session.post.assert_not_called()

    def test_template_is_deepseek_with_no_key(self):
        path = Path(__file__).resolve().parents[1] / ".env.example"
        from dotenv import dotenv_values
        values = dotenv_values(path)
        self.assertFalse(values["DATAPILOT_API_KEY"])
        self.assertEqual(values["DATAPILOT_BASE_URL"], DEFAULT_BASE_URL)
        self.assertEqual(values["DATAPILOT_MODEL"], DEFAULT_MODEL)
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(LLMConfig.from_environment(path))

    def test_path_json_tokens_and_explicit_non_thinking(self):
        for base in [DEFAULT_BASE_URL, DEFAULT_BASE_URL + "/", DEFAULT_BASE_URL + "/v1/"]:
            with self.subTest(base=base):
                config = LLMConfig(FAKE_KEY, base, DEFAULT_MODEL)
                provider = OpenAICompatibleProvider(config)
                provider.complete_json(self.messages)
                args, kwargs = self.session.post.call_args
                self.assertEqual(args[0], base.rstrip("/") + "/chat/completions")
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + FAKE_KEY)
                payload = kwargs["json"]
                self.assertEqual(payload["model"], "deepseek-flash")
                self.assertEqual(payload["max_tokens"], 1200)
                self.assertEqual(payload["thinking"], {"type": "disabled"})
                self.assertEqual(payload["response_format"], {"type": "json_object"})
                self.assertFalse(payload["stream"])
                for excluded in ["max_completion_tokens", "reasoning_effort", "extra_body", "tools"]:
                    self.assertNotIn(excluded, payload)
                self.assertNotIn(FAKE_KEY, json.dumps(payload))

    def test_legacy_token_setting_does_not_break_deepseek(self):
        config = LLMConfig(FAKE_KEY, token_parameter="max_completion_tokens", max_output_tokens=256)
        OpenAICompatibleProvider(config).complete_json(self.messages)
        payload = self.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["max_tokens"], 256)
        self.assertNotIn("max_completion_tokens", payload)
        self.assertEqual(self.session.post.call_count, 1)

    def test_invalid_model_json_distinct_from_envelope_error(self):
        self.response_body("private invalid model content")
        with self.assertRaises(LLMError) as raised:
            OpenAICompatibleProvider(LLMConfig(FAKE_KEY)).complete_json(self.messages)
        self.assertEqual(raised.exception.category, "MODEL_JSON_INVALID")
        self.assertNotIn("private", str(raised.exception))
        self.response.iter_content.return_value = [b"invalid-envelope"]
        with self.assertRaises(LLMError) as raised:
            OpenAICompatibleProvider(LLMConfig(FAKE_KEY)).complete_json(self.messages)
        self.assertEqual(raised.exception.category, "RESPONSE_FORMAT_INVALID")

    def test_final_content_and_actual_usage_without_reasoning(self):
        self.response_body('{"sql":null,"explanation":"请澄清"}', reasoning_content="do-not-display-or-send-back")
        provider = OpenAICompatibleProvider(LLMConfig(FAKE_KEY))
        text = provider.complete_json(self.messages)
        self.assertNotIn("do-not-display", text)
        self.assertEqual(provider.last_metrics.usage, {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42})
        self.assertNotIn("reasoning_content", json.dumps(self.session.post.call_args.kwargs["json"]))

    def test_blank_json_content_rejected_without_retry(self):
        self.response_body(" \n\t ")
        provider = OpenAICompatibleProvider(LLMConfig(FAKE_KEY))
        with self.assertRaisesRegex(LLMError, "EMPTY_RESPONSE"):
            provider.complete_json(self.messages)
        self.assertEqual(self.session.post.call_count, 1)

    def test_truncated_json_and_reasoning_only_are_not_used(self):
        provider = OpenAICompatibleProvider(LLMConfig(FAKE_KEY))
        for content, finish in [('{"sql":', "length"), (None, "stop")]:
            self.response_body(content, finish, reasoning_content='{"sql":"SELECT 1"}')
            with self.subTest(finish=finish), self.assertRaises(LLMError):
                provider.complete_json(self.messages)

    def test_generation_preview_no_samples_and_no_request_without_consent(self):
        frame = pd.DataFrame({"income": [987654321]})
        provider = OpenAICompatibleProvider(LLMConfig(FAKE_KEY))
        with self.assertRaises(AIAnalysisError):
            generate_sql(frame, "共有多少条记录？", provider)
        self.session.post.assert_not_called()
        with patch("src.ai_analysis.run_query") as runner:
            proposal = generate_sql(frame, "共有多少条记录？", provider, confirmed=True)
            runner.assert_not_called()
        self.assertIn("COUNT(*)", proposal.sql)
        payload = self.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["messages"][1]["content"], encode_json(build_generation_payload(frame, "共有多少条记录？")))
        self.assertNotIn("987654321", json.dumps(payload))
        self.assertIn("JSON", payload["messages"][0]["content"])

    def test_budget_and_error_redaction_preserved(self):
        self.response.status_code = 401
        self.response.text = FAKE_KEY
        budget = RequestBudget(limit=1)
        provider = BudgetedProvider(OpenAICompatibleProvider(LLMConfig(FAKE_KEY)), budget)
        with self.assertRaisesRegex(LLMError, "HTTP 401") as raised:
            provider.complete_json(self.messages)
        self.assertNotIn(FAKE_KEY, str(raised.exception))
        with self.assertRaisesRegex(LLMError, "上限"):
            provider.complete_json(self.messages)
        self.assertEqual(self.session.post.call_count, 1)

    def test_explicit_gemini_configuration_retains_its_options(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(f"DATAPILOT_API_KEY={FAKE_KEY}\nDATAPILOT_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/\nDATAPILOT_MODEL=gemini-test-only\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                config = LLMConfig.from_environment(path)
        OpenAICompatibleProvider(config).complete_json(self.messages)
        payload = self.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "gemini-test-only")
        self.assertIn("max_completion_tokens", payload)
        self.assertNotIn("thinking", payload)

    def test_lookalike_host_is_not_deepseek(self):
        config = LLMConfig(FAKE_KEY, "https://api.deepseek.com.evil.invalid", "custom-model")
        OpenAICompatibleProvider(config).complete_json(self.messages)
        payload = self.session.post.call_args.kwargs["json"]
        self.assertNotIn("thinking", payload)
        self.assertIn("max_completion_tokens", payload)

    def test_partial_non_deepseek_config_does_not_use_default_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(f"DATAPILOT_API_KEY={FAKE_KEY}\nDATAPILOT_MODEL=gemini-test-only\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(LLMError, "INVALID_PROVIDER_CONFIG"):
                LLMConfig.from_environment(path)
        self.session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
