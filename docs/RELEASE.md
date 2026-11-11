# DataPilot V4.1 发布准备

当前只整理独立仓库的首次公开发布材料，本轮未创建公开仓库、未 commit、未 push。MIT 仅覆盖本项目原创代码，依赖仍遵循各自许可证。

## 仓库信息建议

- 名称：`DataPilot`
- 描述：`基于 Streamlit、DuckDB 和 OpenAI 兼容 API 的隐私可控数据分析工具：自然语言生成 SQL，用户确认后安全执行。`
- Topics：`python`、`streamlit`、`duckdb`、`sqlglot`、`llm`、`text-to-sql`、`data-analysis`、`privacy`。
- 提交说明建议：`准备 DataPilot V4.1 首次公开发布`。

## 发布范围

DataPilot 为独立 Git 仓库，只发布本项目。发布前用 `git rev-parse --show-toplevel` 确认仓库根目录为 DataPilot，以 Git 列出的已跟踪及未忽略文件作为候选范围；不得加入真实配置、用户数据、日志、缓存或无关文件。本轮不初始化仓库、不改写历史、不配置远程，也不暂存、提交或推送。

## 只读检查

```powershell
.\.venv\Scripts\python.exe scripts/audit_release.py
git check-ignore .env .venv/pyvenv.cfg __pycache__/app.cpython-312.pyc .pytest_cache/probe
git status --short
```

检查脚本按内容识别文件格式，扫描候选文本及所有可达历史文本，只报告路径、对象摘要和风险类别，不打印匹配内容。文本即使使用图片后缀仍会扫描；UTF-8、带BOM的UTF-16/32文本继续执行原有检测规则。已识别的PNG等二进制文件列入 `skipped_binary` 清单，不将压缩字节当文本，也不据此认证安全；需人工检查可见内容及元数据。未知二进制、无法解码文本及超大文件列入 `incomplete`，返回非零状态。发现疑似凭据或个人信息时停止发布，必要时撤销密钥并处理历史，不能仅删除当前文件。

V4.1 既有离线验收为235 passed、2 skipped、215 subtests passed。独立安全复核结论为0 Critical / High / Medium，仍有1项Low：字段名显示可能混淆安全标记。本轮发布整理不修复或隐去该Low，不改变SQL安全策略与人工审批。最新发布扫描和本轮测试应单独记录，不能把启发式扫描无匹配等同于生产环境安全认证。

启发式检查不等于绝对安全证明，无法覆盖所有自定义密钥格式、隐写、不可达对象、reflog、LFS 或服务端历史。发布前应额外检查候选文件清单和元数据，确认没有真实数据。`.gitignore` 不会自动清除已经跟踪或提交的文件。

示例 CSV `examples/demo_income.csv` 为纯虚构数据，地点 A/B/C/D 不代表真实地点；只有这一份 CSV 作为明确例外允许提交。其余 CSV/XLSX/日志、缓存、虚拟环境、密钥文件和数据库默认忽略。

## 本轮发布整理验证（2026-11-11）

- 原有敏感文本检测规则保持不变；新增6项扫描器测试，覆盖PNG误报、JPEG内容识别、
  文本密钥检测、BOM文本、未知编码阻断及工作区/历史跳过记录。
- 定向测试：8 passed、5 subtests passed；完整离线测试：241 passed、0 failed、
  2 skipped、220 subtests passed。真实API测试仍跳过，不替代既有235项验收记录。
- Git候选文件52个：50个文本完成扫描，findings及incomplete均为空；2张截图按内容
  识别为JPEG，列入二进制跳过记录。人工复核未见敏感内容；只有普通JFIF信息，无EXIF。
- 文档中的私人绝对路径、过期仓库说明和其他项目内部名称已清理；25项忽略/例外
  规则检查通过，新增数据库旁路文件规则，保留演示CSV和空白配置模板例外。
- 仓库根目录为DataPilot，当前无可达提交。本轮准备检查通过，不代表已经公开发布、
  消除既有Low或完成生产安全认证；首次提交后仍须复查提交元数据及实际发布清单。
- 未修改Agent核心、真实.env或其他项目，未调用真实API，未暂存、提交或推送。

## 用户确认后才执行的操作

创建公开仓库、添加 remote、commit、push、打 tag 或发布 Release。当前自动化与真实模型验收结果应分开写明，未跑真实接口时不能写成云端验收通过。
