"""HTTP 层和环境配置测试：所有请求均 Mock，不连接外部服务。"""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from src.llm_provider import LLMConfig, LLMError, OpenAICompatibleProvider

FAKE_KEY = "mock-secret-never-use-as-real-key"


class ConfigurationTests(unittest.TestCase):
    def test_missing_config_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            with patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(LLMConfig.from_environment(path))
            path.write_text(f"DATAPILOT_API_KEY={FAKE_KEY}\nDATAPILOT_BASE_URL=https://mock.invalid/v1\nDATAPILOT_MODEL=file-model\n", encoding="utf-8")
            with patch.dict(os.environ, {"DATAPILOT_MODEL": "env-model"}, clear=True):
                config = LLMConfig.from_environment(path)
                self.assertEqual(config.model, "env-model")
                self.assertNotIn(FAKE_KEY, repr(config))
                self.assertNotIn("DATAPILOT_API_KEY", os.environ)

    def test_unsafe_configuration(self):
        base = {"DATAPILOT_API_KEY": FAKE_KEY, "DATAPILOT_MODEL": "mock-model"}
        for url in ["http://remote.invalid/v1", "https://user:pass@remote.invalid/v1", "https://remote.invalid/v1?key=x", "file:///etc/passwd"]:
            with self.subTest(url=url), patch.dict(os.environ, {**base, "DATAPILOT_BASE_URL": url}, clear=True):
                with self.assertRaises(LLMError):
                    LLMConfig.from_environment(Path("missing-env-for-tests"))


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = OpenAICompatibleProvider(LLMConfig(FAKE_KEY, "https://mock.invalid/v1", "mock-model"))
        self.messages = [{"role": "system", "content": "请返回 JSON"}, {"role": "user", "content": "{}"}]
        self.patcher = patch("src.llm_provider.requests.Session")
        factory = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.session = factory.return_value.__enter__.return_value
        self.response = self.session.post.return_value.__enter__.return_value
        self.response.status_code = 200
        self.set_response('{"sql":"SELECT 1","explanation":"测试"}')

    def set_response(self, text, finish="stop", **extra):
        body = {"choices": [{"finish_reason": finish, "message": {"content": text, **extra}}]}
        self.response.iter_content.return_value = [json.dumps(body).encode("utf-8")]

    def test_request_contract_and_no_tools(self):
        self.provider.complete_json(self.messages)
        args, kwargs = self.session.post.call_args
        self.assertEqual(args[0], "https://mock.invalid/v1/chat/completions")
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["json"]["max_completion_tokens"], 1200)
        self.assertEqual(kwargs["json"]["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", kwargs["json"])
        self.assertNotIn(FAKE_KEY, json.dumps(kwargs["json"]))
        self.assertEqual(self.session.post.call_count, 1)

    def test_timeout_and_authentication_redact_errors(self):
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.session.post.side_effect = requests.Timeout(FAKE_KEY)
            with self.assertRaisesRegex(LLMError, "超时") as raised:
                self.provider.complete_json(self.messages)
            self.assertNotIn(FAKE_KEY, str(raised.exception))
            self.session.post.side_effect = None
            for status, expected in [(401, "认证"), (403, "认证"), (429, "受限"), (500, "失败"), (302, "失败")]:
                self.response.status_code = status
                with self.subTest(status=status), self.assertRaisesRegex(LLMError, expected) as raised:
                    self.provider.complete_json(self.messages)
                self.assertNotIn(FAKE_KEY, str(raised.exception))
        self.assertNotIn(FAKE_KEY, output.getvalue())

    def test_invalid_truncated_or_tool_output(self):
        for text, finish, extra in [("ok", "length", {}), ("ok", "stop", {"tool_calls": [{"name": "bad"}]}),
                                    (FAKE_KEY, "stop", {}), ("x" * 12001, "stop", {})]:
            self.set_response(text, finish, **extra)
            with self.subTest(finish=finish), self.assertRaises(LLMError):
                self.provider.complete_json(self.messages)
        self.response.iter_content.return_value = [b"not JSON"]
        with self.assertRaises(LLMError):
            self.provider.complete_json(self.messages)

    def test_size_limit_and_accidental_key_in_prompt(self):
        self.response.iter_content.return_value = [b"x" * (64 * 1024 + 1)]
        with self.assertRaisesRegex(LLMError, "大小"):
            self.provider.complete_json(self.messages)
        self.session.post.reset_mock()
        with self.assertRaisesRegex(LLMError, "凭据"):
            self.provider.complete_json([{"role": "user", "content": FAKE_KEY}])
        self.session.post.assert_not_called()

    def test_unicode_escaped_key_never_reaches_ui(self):
        escaped = "".join("\\u" + format(ord(character), "04x") for character in FAKE_KEY)
        self.set_response('{"sql":null,"explanation":"' + escaped + '"}')
        with self.assertRaisesRegex(LLMError, "凭据") as raised:
            self.provider.complete_json(self.messages)
        self.assertNotIn(FAKE_KEY, str(raised.exception))

    def test_legacy_compatible_token_option(self):
        provider = OpenAICompatibleProvider(LLMConfig(FAKE_KEY, "https://mock.invalid/v1", "mock-model", "max_tokens"))
        provider.complete_json(self.messages)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["max_tokens"], 1200)


if __name__ == "__main__":
    unittest.main()
