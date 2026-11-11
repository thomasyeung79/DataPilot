# DataPilot V4.1 安全与稳定性维护

## 范围与基线

基于 V4.0.1（207 passed、2 skipped、186 subtests passed）增量修复四个 Low 问题。
未安装依赖，未读取或修改真实 .env，未调用真实 API，未操作其他项目或 Git 状态。
历史文档保留；本说明覆盖历史文档关于非有限数规范化为空值的旧行为。

## 四项修复

1. 报告覆盖：按最终外发长表（含8KB裁剪）核对步骤。空步骤没有单元格可发送时，
   partial 为 true；本地 notes 明确步骤编号、返回0行及未包含在发送内容中。
   查询成功但为空不是失败，不补造0或NULL。所有步骤为空时仍不调用模型，查看本地摘要。
   notes 是本地覆盖说明，不额外外发；JSON 的 partial 同样准确声明有限范围。
   覆盖说明随报告缓存保留，原完整结果表、来源步骤及审计记录不变。
2. 单元格：先区分标量，list、ndarray、dict 等均不展开、不调用 repr、不发送原内容；
   使用 `{"type":"unavailable","reason":"non_scalar"}` 安全标记，partial 为 true。
   不支持的标量、过长 Decimal、NaN 与正负 Infinity 使用各自原因代码。预览和报告
   明示标记不是原值、NULL、0。单次 AI 页面与 Agent 页面均展示安全处理说明。
   Python 和常见 NumPy 数值保留数值语义；bool、0、空字符串、None 分开编码。
   JSON 不支持精确 Decimal，因此使用 `{"type":"decimal","value":"精确十进制文本"}`。
   不经 float 转换；解释入口严格校验标签、长度与有限性，极值用 Decimal 比较。
   Decimal 文本超过200字符则安全替换并标记部分结果，不截成另一个数值。
   超过64位的 NumPy 浮点使用相同精确文本协议，以 NumPy 自身文本表示为准。
   原始数据中的同形 dict 仍是非标量，不被当作可信数值标签。
   预览序列化或 UI 排版失败使用固定提示，不展示原值、异常正文或堆栈，不外发。
3. 取消：UI 全部取消路径统一捕获预期 AgentError，显示“操作未获确认”的安全提示。
   不伪称成功；旧任务移出界面时仍清除旧授权。未吞掉非预期取消异常。
   拒绝计划走状态机的计划拒绝入口，不直接跳到 cancel。操作不调用模型或执行SQL。
4. 过期拒绝：持锁确认 AWAITING_APPROVAL 且 approval=plan 后才允许拒绝并取消。
   已批准计划、SQL审批阶段、COMPLETED、FAILED、CANCELLED 收到过期拒绝均抛出
   受控 AgentError，不改终态、不消耗模型请求、不执行SQL。

## 缺失值表示的边界

Pandas 浮点列的 NaN 是缺失值哨兵，DuckDB fetchdf 也用它表示 SQL NULL。维持已有
SQL结果兼容性，这类缺失哨兵按NULL展示；object列的显式NaN则标为不可用的非有限值。
一旦原始SQL NaN和NULL被fetchdf合并成同一浮点哨兵，单凭DataFrame无法还原区别。
不声称可逆恢复这种已丢失的区别。Infinity 不作为NULL，NULL计数与预览采用同一策略。

## 安全不变量与上限

- 不修改 SQLGlot 白名单、DuckDB 沙箱、执行进程、超时、内存或返回行数限制。
- SQL、计划、数据集变化使旧审批失效；每步单独批准，不能自动执行下一步。
- 四层授权、幂等命令、任务及会话请求预算不变；模型不能直接执行工具或代码。
- Agent 报告跨步骤合计最多20个结果单元格，以“步骤、结果行、字段、值”4列长表
  表示，仍最多20行、8KB、单元格文本200字符。新标签的额外字节同样占8KB预算。
- 事实仅格式化真实本地结果或精确授权预览，不补算未发送的数据；推断与建议分开展示。

## 离线验收

```powershell
.\.venv\Scripts\python.exe scripts/run_offline_tests.py -q --disable-warnings
```

入口禁止真实配置默认加载和HTTP，关闭真实API测试开关。新增回归见
`tests/test_v41_safety.py` 和 `tests/test_agent_ui.py`，保留全部旧测试。
覆盖空步骤/全部为空/空与截断、数值类型和精度、非标量与标记、异常预览、覆盖说明缓存、
过期及重复取消、正常与过期计划拒绝、终态保持、无SQL与模型副作用。

最终完整离线运行：**235 passed、0 failed、2 skipped、215 subtests passed**，
耗时78.47秒。相对基线新增28项测试（23项报告/状态回归、5项Streamlit页面回归），
新增29个通过的subtests；2项真实API测试仍跳过。现有测试未删除、跳过或弱化。
`compileall` 对 src、app.py 及新增/修改测试的语法检查通过。没有发起真实API请求。

## 剩余风险及独立审查建议

本版本不是完全隔离环境，也不保证完全防止提示词注入。私有任务属性和session_state
仍由可信应用维护；具备任意同进程代码执行权限的代码不在审批指纹的隔离边界内。
真实云端没有在本轮重验，不能据Mock结果声明服务端行为已验收。旧缓存若没有保存
空步骤元数据，不能仅凭旧preview_json逆推出遗漏的步骤；新报告保留scope_notes。

下一轮重点复核：最终字节裁剪后的步骤覆盖、Decimal标签与非法标签拒绝、缺失值哨兵
约定、取消失败时的UI提示及状态、持锁拒绝的终态保护，以及原SQL审批不变量是否保持。
