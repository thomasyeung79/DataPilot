# DataPilot V4.0 Alpha：受控单数据集分析服务

本文件记录 Alpha 阶段交付；后续页面接线及当前使用方式见 [V4.0 Beta](AGENT_BETA.md)。

本轮只交付服务层和 Mock 验收。没有新增 Streamlit 区域，没有创建或调用真实模型
Provider，没有改动 V3.1 上传、SQL 安全策略、自然语言分析页面或授权流程。
Alpha 不是已经接通云端和页面的 Beta 产品。

## 模块与依赖

```text
AgentRunner
  ├─ agent_planner：元数据请求及严格 JSON 计划解析
  ├─ agent_tools.inspect_schema → profiling（仅本地）
  ├─ ai_analysis.generate_sql → 注入的 Provider
  ├─ agent_tools.run_safe_sql → execute_proposal
  │    → SQLGlot 字段/AST 校验 → 原有 DuckDB 独立进程
  └─ agent_report：本地事实摘要、全任务结果预览包
       → agent_tools.summarize_results → 原有受限事实引用解释器
```

没有动态函数发现、模型工具调用、代码执行或新 Agent 框架。固定只读工具表包含
`inspect_schema`、`run_safe_sql`、`summarize_results`。工具不接受模型提供的资源配置。
SQL 执行继续使用现有默认超时、内存、最大返回行数和禁用外部访问配置。

`AgentRunner(frame, dataset_id, goal, provider, session_budget)` 冻结当前 DataFrame 的
本地深拷贝，注入普通 `LLMProvider` 协议对象及共享 `RequestBudget`。不要再套
`BudgetedProvider`，构造时会拒绝这种重复计数用法。原有会话预算由调用方传入，
Beta 必须复用现有 `ai_api_budget` 对象，不得给 Agent 单独开一个会话额度。

## 状态与审批

```text
CREATED
  └─ 外发确认 + plan → PLANNED → AWAITING_APPROVAL（计划）
       └─ approve_plan → PLANNED
            └─ 外发确认 + generate_step → AWAITING_APPROVAL（单条 SQL）
                 └─ execute_step + 当前 proposal_id + 单独确认
                      → RUNNING → STEP_COMPLETED
                           ├─ 下一步 SQL 生成及单独审批
                           └─ 最后一步 → COMPLETED（本地事实摘要）
```

解析、模型调用或 SQL 执行失败进入 `FAILED`，没有自动重试。用户取消进入
`CANCELLED`。数据集标识变化也进入 `CANCELLED`，包括已经完成的旧任务；不允许
再发送旧结果。缺少授权、SQL 审批标识过期或调用顺序错误只拒绝当前操作，不执行
请求或 SQL。取消和失败均不能恢复执行，需要显式新建任务。

`AWAITING_APPROVAL` 的 `approval` 明确区分计划与 SQL。批准计划不等于批准任何
SQL。每条 SQL 的审批标识绑定任务、步骤和规范化 SQL；上一条审批不能复用。
模型只生成严格结构化计划或 SQL；模型输出不能调用任何工具。

`preview_plan()`、`preview_step()` 可取得完整模型 messages（含系统提示和精确
用户 JSON）。这两个阶段只发送字段名、类型、目标和已批准的当前步骤问题。
`inspect_current_schema()` 的行列数及缺失统计只留本地，不自动进入上述请求。
生成返回 `sql: null` 视为需要业务澄清，停止任务，不擅自代填口径或继续分析。
即使不发送数据行，字段名称和分析目标也可能含敏感信息；未来页面必须明确提示
外发内容、传输风险及云端费用，不能把批准分析计划当成报告外发授权。

## 限额、幂等与审计

- 一个任务一至三个步骤，最多六次模型请求尝试。正常三步需要一次规划、三次
  SQL 生成、可选一次报告，共四至五次；没有为凑满六次增加请求。
- 模型失败请求也计入任务和共享会话预算；在预算已满时不调用 Provider。
- 每次调用记录本地耗时、成功标志和实际返回的 Token 字段。缺少用量是 `None`，
  不估算 Token、总量或费用，也不把上一次成功请求的用量归给失败请求。
- 每个变更命令必须有调用方生成、在重复提交时保持稳定的 `command_id`。
  相同命令只返回当前快照，不重复副作用；同一标识更换内容则拒绝。
  即使换用新的标识，状态检查仍阻止重复生成和重复执行。
- 本地审计事件含 UTC 时间、顺序、状态、步骤、操作和内容摘要散列，不记录
  API Key、请求正文或第三方异常正文。SQL 历史、数据结果和目标留在任务本地
  快照，不能把整个快照直接写到公开日志。
- 快照深拷贝隔离，页面不能修改快照中的 DataFrame 来修改任务结果。
- 幂等记录和审计只在内存中，不提供跨进程重启、持久化或防篡改审计保证。

## 本地摘要与报告外发

所有 SQL 完成后，`snapshot().facts` 确定性记录每步实际返回行数、字段、NULL
单元格数和截断标志，不把返回行数当完整数据集总数，不自动评价业务优劣。

可选报告需要先调用 `preview_report()`，再以该包的**原样** `json_text` 调用
`summarize(preview_json=..., confirmed=True, ...)`。未确认或内容与当前预览不同
不会调用模型。解释也计入共享预算；失败停止任务，保留本地查询事实但不产生报告。

跨步骤结果按“步骤、结果行、字段、值”的长表发送：每行是一个实际单元格，
避免不同结果表拼接时人为制造 NULL。所有步骤合计最多 **20 行、4 列（低于20列
上限）、8KB UTF-8**，单元格沿用200字符限制。超过限额会标记部分结果。
按步骤、结果行、字段顺序取前20个单元格，可能不包含后续步骤；用户必须能看清
覆盖范围。空结果只给本地摘要，不调用模型。NULL 不等于零。

外发仅有预览包，不附带原文件、原表、额外样本、问题或 SQL。复用 V3.1 的
事实引用校验：事实由程序从实际单元格或可验证数值极值渲染，推断和建议为受限
中文模板。报告保留精确预览、NULL 说明和部分结果提醒。它不是任意自由文本
长报告，也不保证分析目标的业务口径正确或不同单位指标可以比较。

## Alpha 验收

在项目根目录使用 PowerShell：

```powershell
Remove-Item Env:DATAPILOT_LIVE_API -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_agent.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Agent 测试不创建云端 Provider。正常三步集成测试走真实本地 SQL 独立进程，
其余覆盖审批、恶意/无效 SQL、非法 JSON、步骤和请求上限、会话共享、超时、
取消、重复命令、更换数据集、预览一致性、NULL、部分结果及报告校验失败。
Streamlit 重复提交在 Alpha 仅验证服务层命令重放；真正页面交互验收属于 Beta。

本轮最终验收：全量运行 **141 项，139 项通过，2 项真实 API 测试跳过**，耗时
53.766秒。其中新增 Agent 测试29项全部通过；原有112项保持110通过、2跳过。
未调用真实云端 API。另包含 NULL 与大整数混合时的精度回归测试。

## 安全边界与 Beta 接入条件

这不是完全隔离的执行环境，不保证完全防止提示词注入；授权来自可信应用调用
方，Python 私有属性不是对恶意同进程代码的安全屏障。仍依赖原 SQL 白名单、
字段校验、独立进程和 DuckDB 安全配置作为执行边界。

取消标志可在在途操作期间设置，返回结果会被丢弃，不再继续后续步骤；已经发出
的 API 请求可能仍计费，已经启动的 SQL 继续由原执行器超时/进程回收管理。
Alpha 使用串行服务命令，没有新增后台任务管理。数据源深拷贝和快照会增加内存
占用；并不提供所有 Python 内存占用的硬限制。

`dataset_id` 必须由可信页面根据上传文件内容和工作表/读取选项生成，每个命令都
传当前值。Alpha 对不匹配标识立即作废旧任务；实际上传事件与任务销毁尚未接线。
仅在内存中更改来源 DataFrame 不会自动改变标识，也不会修改任务已冻结的数据。

进入 Beta 需要用户确认后，才新增独立 `agent_ui.py` 区域、复用 DeepSeek Provider
和原会话预算、实现精确外发预览及所有审批控件，并用 Streamlit 端到端 Mock
验收刷新、上传换表和重复点击。Beta 不默认授权真实 API 验收；真实请求仍需
用户另行明确同意。保留原 AI 自然语言分析区域，不开发图表、导出或多 Agent。
