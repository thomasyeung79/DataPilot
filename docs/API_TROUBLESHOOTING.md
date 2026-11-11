# 云端 API 连接失败排查

## 当前默认：DeepSeek

配置模板和代码默认使用官方 Base URL `https://api.deepseek.com` 与模型 `deepseek-flash`；不删除 Gemini 或通用 Provider。没有密钥时不启用 AI，已有 `.env` 显式配置继续优先，不会自动改写或跨提供商复用原密钥。

切换实际服务时，在本地编辑 `.env`，同时填入 **DeepSeek 的密钥**和以下非敏感配置：

```dotenv
DATAPILOT_BASE_URL=https://api.deepseek.com
DATAPILOT_MODEL=deepseek-flash
DATAPILOT_TOKEN_PARAMETER=max_tokens
DATAPILOT_JSON_MODE=json_object
DATAPILOT_MAX_OUTPUT_TOKENS=1200
```

重启应用，检查接收方主机名为 `api.deepseek.com`。不要继续使用 Gemini 密钥，也不要在 Base URL 后面重复填写 `/chat/completions`。系统环境变量优先于 `.env`，若仍显示原提供商，应在本地检查对应环境变量覆盖情况，不要打印密钥。

按 [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) 和 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)，本项目发出 `POST /chat/completions`，并明确发送 `thinking={"type":"disabled"}`；该字段在原始 HTTP JSON 顶层，不应嵌套在 SDK 的 `extra_body` 中。官方主机固定使用 `max_tokens`，不发送 `max_completion_tokens` 或 `reasoning_effort`。

[JSON Output 文档](https://api-docs.deepseek.com/guides/json_mode/)支持 `response_format={"type":"json_object"}`，要求提示中出现 JSON 并提供结构示例。本项目两种提示均满足此要求。JSON 模式仍可能返回全空白 content 或因输出上限被截断；分别显示 `EMPTY_RESPONSE` 或不完整输出错误，不自动重试、不将 reasoning_content 作为结果，也不绕过后续格式/字段/SQL 安全校验。

验收顺序：上传虚构演示 CSV → 输入“共有多少条记录？” → 核对元数据预览并主动确认生成 → 核对 SQL 并主动确认执行 → 预期为 8。可选解释仍需单独确认，限制维持 20 行、20 列、8 KB。每会话请求预算与实际 Token 用量统计不变，HTTP 错误沿用下方安全分类。本次仅做 Mock 测试，未调用真实 DeepSeek API。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_deepseek_provider.py -v
```

本次 DeepSeek 12 项 Mock 测试全部通过；全量共 112 项，110 项通过、2 项真实 API 测试显式跳过。依赖与语法检查通过，尚不代表真实 DeepSeek 模型验收通过。

## 历史记录：Gemini 连接失败排查

本次仅修改 Provider 的配置诊断与错误分类，未改变 SQL 白名单、隐私确认流程、数据发送内容或请求重试策略，没有调用真实模型 API，也未修改用户 `.env` 或选择替代模型。

## 本机只读检查结果

- 项目 `.env` 存在，API Key、Base URL、模型字段均从该文件加载，无对应系统环境变量覆盖。没有输出密钥或 `.env` 原文。
- Base URL 的主机与兼容路径正确，拼接后为官方 `/v1beta/openai/chat/completions`，没有重复后缀。
- 首次检查时模型字段包含非 ASCII 内容，不是可直接使用的正常 Gemini API 模型 ID。新代码会在网络调用前报告 `INVALID_MODEL_CONFIG`，提示在本地填写实际模型 ID，不回显原值，也不会自动挑选模型。
- 排查期间 `.env` 已有外部更新；最后只读复核时，模型字段已通过格式检查，官方主机和路径仍正确。此次代码修改未写入 `.env`，已保留现有配置；格式通过不等于模型存在、密钥有效或接口实际验收通过。
- 原提示把 400、404 等状态合并为同一泛化错误，无法据此回溯那次实际返回码。模型配置是明确发现的问题，但在没有原始状态且不重发请求的前提下，不能证明它是服务端失败的唯一原因。

## 接口与参数核对

官方兼容文档：[Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)。其端点与当前配置一致，且列有结构化 JSON 输出示例。官方示例仓库也展示了通过 `response_format` 生成结构化结果：[Google Gemini Cookbook](https://github.com/google-gemini/cookbook/blob/main/quickstarts/Get_started_OpenAI_Compatibility.ipynb)。

当时 Gemini 配置默认发送 `response_format={"type":"json_object"}` 和 `max_completion_tokens=1200`。当时核对的公开兼容页面没有完整列出这两个具体选项的模型级支持矩阵，不能仅凭泛化错误认定它们不受支持。Google 开发者论坛有该兼容端点使用 `max_completion_tokens` 的讨论与官方回复，但不能替代当前所选模型的实际验收：[Gemini Max Tokens](https://discuss.ai.google.dev/t/gemini-max-tokens/98124)。

本次保留原参数，不盲目关闭 JSON 模式、不自动改为 max_tokens，不重复探测服务。用户修正实际模型 ID 后，如主动确认的请求明确返回 400，再对照模型及服务商文档检查参数；既有配置仍允许显式选择 `DATAPILOT_TOKEN_PARAMETER=max_tokens` 或 `DATAPILOT_JSON_MODE=prompt`，但这不是本次自动实施的修复。

## 安全错误类别

| 状态/异常 | 页面类别 | 提示方向 |
| --- | --- | --- |
| HTTP 400 | INVALID_REQUEST | 模型 ID、JSON 模式、Token 参数或格式 |
| HTTP 401 | AUTHENTICATION_FAILED | 密钥认证 |
| HTTP 403 | PERMISSION_DENIED | 模型权限、项目或地区限制 |
| HTTP 404 | NOT_FOUND | 模型或接口路径 |
| HTTP 429 | RATE_LIMITED | 限流或额度 |
| HTTP 500 / 其他 5xx | SERVER_ERROR | 服务端错误 |
| 连接超时 | CONNECT_TIMEOUT | 网络连接超时 |
| 响应读取超时 | READ_TIMEOUT | 包括 requests 包装成 ConnectionError 的流式读取超时 |
| 其他超时/网络异常 | NETWORK_TIMEOUT / NETWORK_ERROR | 无有效 HTTP 响应时不编造状态码 |

服务端错误采用“状态码白名单 → 固定中文提示”的映射，完全不回显错误正文、Header、URL、请求内容或异常原文，而不是只替换当前密钥字符串。即使服务商在错误正文中回显敏感数据，也不会送入页面或日志。HTTP 200 内的错误对象只展示 `HTTP 200 · PROVIDER_ERROR`。

## 验证方式

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_api_diagnostics.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_llm_provider.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_provider_v31.py -v
```

全部使用 Mock HTTP 和虚构配置。真实模型可用性、密钥有效性、项目配额和实际模型参数支持仍需用户在正确配置后，通过原有页面授权流程自行验收。本次未开启真实冒烟测试，不执行任何 GitHub 发布操作。

最终全量回归：100 项中 98 项通过，2 项真实 API 测试显式跳过；另已通过语法与依赖检查。SQL 安全和原有确认流程测试保持通过。
