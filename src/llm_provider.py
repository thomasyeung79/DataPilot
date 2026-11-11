"""可替换的 LLM 接口；只接收显式构造的消息，不接触上传文件。"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import time
from typing import Protocol
from urllib.parse import urlsplit

from dotenv import dotenv_values
import requests
from urllib3.exceptions import ReadTimeoutError

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


class LLMError(ValueError):
    """不包含服务端原始响应、凭据或请求内容的中文错误。"""

    def __init__(self, message: str, *, status_code: int | None = None, category: str | None = None):
        self.status_code = status_code
        self.category = category
        prefix = f"HTTP {status_code} · " if status_code is not None else ""
        prefix += f"{category}：" if category else ""
        super().__init__(prefix + message)


def _http_error(status: int) -> LLMError:
    """按状态码白名单映射，不读取/拼接服务端正文、URL、Header 或异常原文。"""
    errors = {
        400: ("INVALID_REQUEST", "模型服务请求失败：参数或请求格式无效，请检查实际模型 ID、JSON 模式和 Token 参数。"),
        401: ("AUTHENTICATION_FAILED", "模型认证失败，请检查 API Key 是否有效；密钥不会显示。"),
        403: ("PERMISSION_DENIED", "模型认证或访问被拒绝，请检查模型权限、项目和地区限制。"),
        404: ("NOT_FOUND", "模型服务请求失败：模型或接口不存在，请核对 DATAPILOT_MODEL 和 DATAPILOT_BASE_URL。"),
        408: ("REQUEST_TIMEOUT", "模型服务请求超时；未自动重试。"),
        429: ("RATE_LIMITED", "模型请求受限或额度不足，请检查服务商配额；未自动重试。"),
        500: ("SERVER_ERROR", "模型服务请求失败：服务器内部错误，请稍后手动重试。"),
    }
    default = ("SERVER_ERROR", "模型服务请求失败：服务器暂不可用；未自动重试。") if status >= 500 else (
        "UNEXPECTED_HTTP_STATUS", "模型服务请求失败：收到未预期的 HTTP 状态，请核对接口配置。")
    category, message = errors.get(status, default)
    return LLMError(message, status_code=status, category=category)


@dataclass(frozen=True)
class LLMConfig:
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    token_parameter: str = "max_completion_tokens"
    json_mode: str = "json_object"
    max_output_tokens: int = 1200

    @property
    def is_deepseek(self) -> bool:
        """按官方主机识别协议选项，不影响 Gemini 和其他兼容端点。"""
        return urlsplit(self.base_url).hostname == "api.deepseek.com"

    @property
    def request_token_parameter(self) -> str:
        # 官方 DeepSeek Chat Completions 文档使用 max_tokens，避免旧配置遗留不兼容参数。
        return "max_tokens" if self.is_deepseek else self.token_parameter

    @classmethod
    def from_environment(cls, env_file: Path | None = None):
        """只读项目 .env；系统环境变量优先，不把密钥写入 os.environ。"""
        env_file = env_file or Path(__file__).resolve().parents[1] / ".env"
        try:
            values = dotenv_values(env_file, interpolate=False) if env_file.is_file() else {}
            def setting(name, default=""):
                return str(os.environ.get(name, values.get(name) or default)).strip()
            key = setting("DATAPILOT_API_KEY")
            explicit_base = setting("DATAPILOT_BASE_URL")
            explicit_model = setting("DATAPILOT_MODEL")
            if not key:
                return None
            if not explicit_base and explicit_model and not explicit_model.startswith("deepseek-"):
                raise LLMError("显式配置非 DeepSeek 模型时，请同时填写匹配的 DATAPILOT_BASE_URL，避免凭据发往错误提供商。",
                               category="INVALID_PROVIDER_CONFIG")
            base = (explicit_base or DEFAULT_BASE_URL).rstrip("/")
            is_deepseek = urlsplit(base).hostname == "api.deepseek.com"
            model = explicit_model or (DEFAULT_MODEL if is_deepseek else "")
            if not all([key, base, model]):
                return None
            token_parameter = setting("DATAPILOT_TOKEN_PARAMETER", "max_tokens" if is_deepseek else "max_completion_tokens")
            json_mode = setting("DATAPILOT_JSON_MODE", "json_object")
            max_tokens = int(setting("DATAPILOT_MAX_OUTPUT_TOKENS", "1200"))
            parsed = urlsplit(base)
            if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.scheme not in {"http", "https"}
                    or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
                    or token_parameter not in {"max_completion_tokens", "max_tokens"}
                    or json_mode not in {"json_object", "prompt"} or not 128 <= max_tokens <= 2048
                    or key in base or key in model
                    or len(model) > 200 or any(char in key + base + model for char in "\r\n")):
                raise LLMError("模型配置无效：远程接口必须使用 HTTPS，Base URL 不可包含凭据、查询参数或片段。")
            # 官方 Gemini 接口需要 API 模型标识，不能使用中文说明或展示名称。
            # 不硬编码可用模型列表，也不自动替用户选择/替换模型。
            if (parsed.hostname == "generativelanguage.googleapis.com"
                    and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", model)):
                raise LLMError("DATAPILOT_MODEL 不是有效的 Gemini API 模型 ID 格式；请在本地 .env 填写有访问权限的实际模型 ID，不要填写中文说明或带空格的展示名称。",
                               category="INVALID_MODEL_CONFIG")
            return cls(key, base, model, token_parameter, json_mode, max_tokens)
        except LLMError:
            raise
        except Exception:
            raise LLMError("无法读取模型配置，请检查本地环境变量或 .env。") from None


class LLMProvider(Protocol):
    def complete_json(self, messages: list[dict[str, str]]) -> str:
        """返回 JSON 文本；业务层负责校验结构和内容。"""
        ...


@dataclass(frozen=True)
class CallMetrics:
    """仅保留耗时与服务端实际返回的已知 Token 字段，不估算缺失值或金额。"""

    elapsed_seconds: float
    usage: dict[str, int] | None


@dataclass
class RequestBudget:
    """本会话请求尝试数上限；失败请求也消耗一次，非服务商金额预算。"""

    limit: int = 10
    used: int = 0

    def claim(self):
        if self.used >= self.limit:
            raise LLMError("本会话已达到模型请求次数上限，请明确重置计数后再试。")
        self.used += 1


class BudgetedProvider:
    """为任意 Provider 添加显式请求预算，保持未来替换本地模型的接口。"""

    def __init__(self, provider: LLMProvider, budget: RequestBudget):
        self.provider = provider
        self.budget = budget

    @property
    def last_metrics(self):
        metrics = getattr(self.provider, "last_metrics", None)
        return metrics if isinstance(metrics, CallMetrics) else None

    def complete_json(self, messages):
        self.budget.claim()
        return self.provider.complete_json(messages)


class OpenAICompatibleProvider:
    """OpenAI 兼容 Chat Completions；无重试、无重定向、无工具调用。"""

    MAX_RESPONSE_BYTES = 64 * 1024
    MAX_CONTENT_CHARS = 12_000

    def __init__(self, config: LLMConfig):
        self._config = config
        self.last_metrics: CallMetrics | None = None

    def complete_json(self, messages: list[dict[str, str]]) -> str:
        self.last_metrics = None
        if self._config.api_key in json.dumps(messages, ensure_ascii=False):
            raise LLMError("请求内容疑似包含 API 凭据，已拒绝发送。")
        payload = {
            "model": self._config.model,
            "messages": messages,
            self._config.request_token_parameter: self._config.max_output_tokens,
            "stream": False,
        }
        if self._config.is_deepseek:
            # 原始 HTTP JSON 中 thinking 位于顶层；SDK 的 extra_body 会被 SDK 展开。
            # 只使用最终 content，不请求或回传 reasoning_content，不自动切换思考模式。
            payload["thinking"] = {"type": "disabled"}
        if self._config.json_mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        # 不记录请求、响应或异常文本；密钥只用于 Authorization 请求头。
        started = time.monotonic()
        usage = None
        try:
            with requests.Session() as session:
                session.trust_env = False  # 不自动采用代理、netrc 凭据或环境中的 CA 覆盖。
                with session.post(
                    self._config.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + self._config.api_key},
                    json=payload, timeout=(5, 20), allow_redirects=False, stream=True,
                ) as response:
                    status = response.status_code
                    if status != 200:
                        raise _http_error(status)
                    chunks = bytearray()
                    for chunk in response.iter_content(chunk_size=4096):
                        if time.monotonic() - started > 30:
                            raise LLMError("模型请求超时；未自动重试，请稍后手动重试。", category="REQUEST_TIMEOUT")
                        chunks.extend(chunk)
                        if len(chunks) > self.MAX_RESPONSE_BYTES:
                            raise LLMError("模型响应超过大小限制，已拒绝处理。")
                    envelope = json.loads(chunks)
                    raw_usage = envelope.get("usage")
                    if isinstance(raw_usage, dict):
                        usage = {key: value for key, value in raw_usage.items()
                                 if key in {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"}
                                 and type(value) is int and value >= 0} or None
                    if envelope.get("error"):
                        raise LLMError("模型服务返回错误响应，未使用其中内容。", status_code=200, category="PROVIDER_ERROR")
                    content = envelope["choices"][0]
                    # 部分兼容服务省略结束原因；仍必须通过 JSON、内容长度和业务校验。
                    if content.get("finish_reason") not in {None, "stop"}:
                        raise LLMError("模型输出未正常完成或被截断，请简化问题后重试。")
                    message = content["message"]
                    if message.get("tool_calls") or message.get("function_call") or message.get("refusal"):
                        raise LLMError("模型返回工具调用或拒绝回答标记，已拒绝处理。")
                    text = message["content"]
                    if isinstance(text, list):
                        parts = []
                        for part in text:
                            if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
                                raise LLMError("模型返回了不支持的非文本内容。")
                            parts.append(part["text"])
                        text = "".join(parts)
                    if isinstance(text, str) and not text.strip():
                        raise LLMError("模型返回空白内容，未生成有效 JSON；请检查提示和输出上限后手动重试。", category="EMPTY_RESPONSE")
                    if not isinstance(text, str) or not text or len(text) > self.MAX_CONTENT_CHARS:
                        raise LLMError("模型返回内容无效或过长。")
                    if self._config.api_key in text:
                        raise LLMError("模型返回内容包含凭据信息，已拒绝展示。")
                    # 防止服务端用 JSON Unicode 转义回显密钥后被业务层解码展示。
                    def contains_key(value):
                        if isinstance(value, str):
                            return self._config.api_key in value
                        if isinstance(value, dict):
                            return any(contains_key(key) or contains_key(item) for key, item in value.items())
                        if isinstance(value, list):
                            return any(contains_key(item) for item in value)
                        return False
                    try:
                        decoded_text = json.loads(text)
                    except (ValueError, TypeError):
                        raise LLMError("模型响应格式异常，模型内容不是有效 JSON。",
                                       category="MODEL_JSON_INVALID") from None
                    if contains_key(decoded_text):
                        raise LLMError("模型返回内容包含凭据信息，已拒绝展示。")
                    return text
        except LLMError:
            raise
        except requests.ConnectTimeout:
            raise LLMError("连接模型服务超时；未自动重试。", category="CONNECT_TIMEOUT") from None
        except requests.ReadTimeout:
            raise LLMError("读取模型响应超时；未自动重试。", category="READ_TIMEOUT") from None
        except requests.Timeout:
            raise LLMError("模型网络请求超时；未自动重试。", category="NETWORK_TIMEOUT") from None
        except requests.ConnectionError as exc:
            # requests 在 stream=True 读取响应时会将 urllib3 读取超时包装为 ConnectionError。
            if exc.args and isinstance(exc.args[0], ReadTimeoutError):
                raise LLMError("读取模型响应超时；未自动重试。", category="READ_TIMEOUT") from None
            raise LLMError("无法连接模型服务，请检查网络、TLS 和接口配置。", category="NETWORK_ERROR") from None
        except requests.RequestException:
            raise LLMError("无法完成模型网络请求，请检查网络与接口配置。", category="NETWORK_ERROR") from None
        except Exception:
            raise LLMError("模型响应格式异常，无法读取有效内容。", category="RESPONSE_FORMAT_INVALID") from None
        finally:
            self.last_metrics = CallMetrics(round(time.monotonic() - started, 3), usage)
