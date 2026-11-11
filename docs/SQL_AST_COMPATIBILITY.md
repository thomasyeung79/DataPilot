# 两步骤 SQL 兼容性与安全 AST 审计（2026-11-10）

## 能确认及不能确认的原因

用户提供的新一次真实验收记录可确认：两步计划已生成，第一步查询成功；第二步
模型请求返回后，在 SQL 安全校验阶段以 `SQL_AST_REJECTED` 拦截，尚未提交执行器，
共三次模型请求。这足以定位到第二步 AST 校验，不能确定具体被拒绝的节点。

项目内未发现可恢复的落盘审计文件。现有审计只存在 Streamlit 任务内存中；
`sql_history` 在执行成功后才追加。校验失败的候选 SQL 不会进入该历史，也没有
默认保存模型响应。因此本次没有找到原候选 SQL，不能认定用了 CASE 或 NULLIF。
新诊断只能帮助后续任务，不能恢复旧节点信息。

## 本地实测支持范围

使用项目当前 SQLGlot、DuckDB 和模拟数据验证，没有新增白名单节点。

| 结构 | 当前结果 | 拒绝时的首个节点类型 |
| --- | --- | --- |
| 除法、SUM/AVG/COUNT、GROUP BY/HAVING/ORDER BY | AST及字段校验允许 | — |
| 基础聚合子查询、同数据集 LEFT JOIN、OR/IS NULL | AST及字段校验允许 | — |
| CASE WHEN | 拒绝 | Case |
| NULLIF | 拒绝 | Nullif |
| CAST | 拒绝 | Cast |
| CTE/WITH | 拒绝 | With |
| 窗口聚合 | 拒绝 | Window |
| 聚合 FILTER | 拒绝 | Filter |

节点是 SQLGlot 的类名，大小写与 SQL 关键字不同。这里只列出被测结构，不能据此
宣称任意组合都受支持。仍须单条 SELECT、只读白名单及实际字段校验，来源只能是
uploaded_data；禁止外部文件、网络、其他数据库及修改语句。

裸除法通过 AST 不代表零分母会自动转 NULL。满足零工时业务口径的生成 SQL 必须
使用下方已验证模板，不能用 WHERE hours > 0 或分组后 HAVING SUM(hours)>0 丢掉
零工时地点，也不能将 NULL 擅自替换为零。

## 优先生成的 SQL 模板

规划器提示明确两步各自查询 uploaded_data，不能引用第一步临时结果表，也不为
最终报告增加 SQL 步骤。SQL 生成提示增加下面的完整合法结构示例；只能在实际
字段匹配、问题口径明确时使用，不发送任何数据行，不默认 ROUND。

第一步：

```sql
SELECT location, SUM(income) AS total_income, SUM(hours) AS total_hours
FROM uploaded_data GROUP BY location ORDER BY location
```

第二步：

```sql
SELECT t."location", t."total_income" / d."total_hours" AS "weighted_avg_hourly"
FROM (SELECT "location", SUM("income") AS "total_income"
      FROM uploaded_data GROUP BY "location") t
LEFT JOIN (SELECT "location", SUM("hours") AS "total_hours"
           FROM uploaded_data GROUP BY "location" HAVING SUM("hours") <> 0) d
ON t."location" = d."location" OR (t."location" IS NULL AND d."location" IS NULL)
ORDER BY t."location"
```

左侧保留所有地点及全部收入，右侧只保留非零总工时的分母。零工时、全部工时
NULL 或工时正负抵消的组，连接后分母为 NULL，因而时薪为 NULL。OR 条件保留
空地点的匹配。没有调用 CASE、NULLIF、CAST 或新增函数。

通过现有独立进程及逐步审批，demo_income.csv 的实测结果为：

| location | total_income | total_hours | weighted_avg_hourly |
| --- | ---: | ---: | ---: |
| A | 400 | 12 | 33.333333333333336 |
| B | 400 | 10 | 40 |
| C | 90 | 0 | NULL |
| D | 0 | 0 | NULL |

## 新的安全诊断

失败卡片和 command_failed 审计新增 `ast_node`、`ast_reason`。只接收 SQLGlot
内置节点类名及固定原因代码，逐层由 SQL 校验器、AI 错误、Agent 诊断传递。
例如 `Case / UNSUPPORTED_NODE`，或 `Table / TABLE_SOURCE`。只记录首个拒绝点，
不代表整条 SQL 只有这一个问题。无法定位时留空，不从模型正文推测。

不记录 AST 的 SQL 文本、字段/表名、字面值、完整模型响应或凭据。仍保留阶段、
步骤、类别、执行器提交状态、请求次数和实际 Token 用量。成功查询历史仅在任务
内存中供页面展示，不作为新的默认 SQL 日志。SQLGlot 的错误上下文长度设置为0，
防止“不支持命令”的解析警告回显 SQL 片段；这不改变解析或拒绝规则。

## 离线测试及再次真实验收

使用新的离线 pytest 入口可阻断默认 .env 加载和 requests HTTP，且强制跳过
真实 API 测试；配置单测仍可读取指定的虚构临时配置或空白示例文件：

```powershell
.\.venv\Scripts\python.exe scripts/run_offline_tests.py -q
```

`test_sql_prompt_compatibility.py` 覆盖语法矩阵、完整模板、精确两步结果、逐步审批、
零分母、空地点、全空工时、正负工时抵消、节点元数据脱敏及解析警告不回显 SQL。
现有全部测试也保留在运行范围内。Mock 验收不能保证真实模型必然遵循模板。

本轮离线全量 pytest：**191项通过、2项真实 API 测试跳过**（合计193项），
另有180个通过的 subtest，耗时92.54秒。新增兼容性测试8项全部通过，现有
Agent 页面32项也全部通过。真实 .env 的默认加载和 requests HTTP 在测试进程中
均被阻断；没有为了验收而增加重试、自动执行或 SQL 权限。

用户可以在明确授权后重启 Streamlit、新建两步骤任务，用虚构 demo_income.csv
再次真实验收。每个生成请求和每条 SQL 仍须各自授权；第二步若再次拒绝，查看
新的 AST 类名及固定原因代码，不自动重试、不执行未批准 SQL，也不公开原始响应。
本轮没有调用真实 DeepSeek、读取或修改真实 .env、操作其他项目或执行 Git。
