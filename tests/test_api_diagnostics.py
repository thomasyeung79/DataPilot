"""API 失败诊断回归：不读取真实 .env、不联网、不输出凭据。"""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests
from urllib3.exceptions import ReadTimeoutError

from src.llm_provider import LLMConfig, LLMError, OpenAICompatibleProvider

FAKE_KEY = "mock-diagnostic-secret-never-real"


class APIDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.provider = OpenAICompatibleProvider(LLMConfig(FAKE_KEY, "https://mock.invalid/v1", "mock-model"))
        self.messages = [{"role": "system", "content": "返回 JSON"}, {"role": "user", "content": "private-question"}]
        factory = patch("src.llm_provider.requests.Session")
        self.session = factory.start().return_value.__enter__.return_value
        self.addCleanup(factory.stop)
        self.response = self.session.post.return_value.__enter__.return_value

    def test_http_status_and_category_without_reading_error_body(self):
        expected = {400: "INVALID_REQUEST", 401: "AUTHENTICATION_FAILED", 403: "PERMISSION_DENIED",
                    404: "NOT_FOUND", 429: "RATE_LIMITED", 500: "SERVER_ERROR", 503: "SERVER_ERROR"}
        for status, category in expected.items():
            with self.subTest(status=status):
                self.response.status_code = status
                self.response.iter_content.reset_mock()
                with self.assertRaises(LLMError) as raised:
                    self.provider.complete_json(self.messages)
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(raised.exception.category, category)
                self.assertIn(f"HTTP {status}", str(raised.exception))
                self.response.iter_content.assert_not_called()

    def test_error_body_headers_and_key_are_not_logged_or_displayed(self):
        self.response.status_code = 400
        secrets = [FAKE_KEY, "private-question", "private-data-row", "https://private.invalid/path?secret=x"]
        self.response.text = " ".join(secrets)
        output = StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger()
        logger.addHandler(handler)
        try:
            with redirect_stdout(output), redirect_stderr(output):
                with self.assertRaises(LLMError) as raised:
                    self.provider.complete_json(self.messages)
            for secret in secrets:
                self.assertNotIn(secret, str(raised.exception))
                self.assertNotIn(secret, output.getvalue())
            self.response.json.assert_not_called()
            self.response.iter_content.assert_not_called()
        finally:
            logger.removeHandler(handler)

    def test_connection_read_and_wrapped_read_timeouts(self):
        cases = [
            (requests.ConnectTimeout(FAKE_KEY), "CONNECT_TIMEOUT"),
            (requests.ReadTimeout(FAKE_KEY), "READ_TIMEOUT"),
            (requests.Timeout(FAKE_KEY), "NETWORK_TIMEOUT"),
            (requests.ConnectionError(ReadTimeoutError(None, "/private", FAKE_KEY)), "READ_TIMEOUT"),
            (requests.ConnectionError(FAKE_KEY), "NETWORK_ERROR"),
        ]
        for error, category in cases:
            with self.subTest(category=category):
                self.session.post.side_effect = error
                with self.assertRaises(LLMError) as raised:
                    self.provider.complete_json(self.messages)
                self.assertEqual(raised.exception.category, category)
                self.assertIsNone(raised.exception.status_code)
                self.assertNotIn(FAKE_KEY, str(raised.exception))
                self.assertNotIn("HTTP 500", str(raised.exception))

    def test_streamed_read_timeout_is_classified(self):
        self.response.status_code = 200
        self.response.iter_content.side_effect = requests.ConnectionError(ReadTimeoutError(None, "/private", FAKE_KEY))
        with self.assertRaisesRegex(LLMError, "READ_TIMEOUT") as raised:
            self.provider.complete_json(self.messages)
        self.assertNotIn(FAKE_KEY, str(raised.exception))

    def test_official_gemini_model_format_fails_before_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            for model in ["填写模型名称", "Gemini Display Name", "gemini-模拟-model"]:
                path.write_text(f"DATAPILOT_API_KEY={FAKE_KEY}\nDATAPILOT_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/\nDATAPILOT_MODEL={model}\n", encoding="utf-8")
                with self.subTest(model=model), patch.dict(os.environ, {}, clear=True):
                    with self.assertRaisesRegex(LLMError, "INVALID_MODEL_CONFIG") as raised:
                        LLMConfig.from_environment(path)
                    self.assertNotIn(model, str(raised.exception))
                    self.assertNotIn(FAKE_KEY, str(raised.exception))
            self.session.post.assert_not_called()

    def test_gemini_endpoint_parameters_preserved_without_retry(self):
        self.response.status_code = 200
        self.response.iter_content.return_value = [b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(f"DATAPILOT_API_KEY={FAKE_KEY}\nDATAPILOT_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/\nDATAPILOT_MODEL=gemini-test-only\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                config = LLMConfig.from_environment(path)
        OpenAICompatibleProvider(config).complete_json(self.messages)
        args, kwargs = self.session.post.call_args
        self.assertEqual(args[0], "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
        self.assertEqual(kwargs["json"]["model"], "gemini-test-only")
        self.assertEqual(kwargs["json"]["response_format"], {"type": "json_object"})
        self.assertEqual(kwargs["json"]["max_completion_tokens"], 1200)
        self.assertNotIn("max_tokens", kwargs["json"])
        self.assertEqual(self.session.post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
