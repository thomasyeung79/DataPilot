"""Agent Beta 页面：仅显式按钮触发命令，所有外部调用复用现有 Provider。"""

from contextvars import ContextVar
import hashlib
from urllib.parse import urlsplit
from uuid import uuid4

import streamlit as st

from src.agent_planner import planning_messages
from src.agent_report import local_summary, readable_report_view
from src.agent_runner import AgentError, AgentRunner, AgentState
from src.agent_tools import inspect_schema
from src.ai_analysis import AIAnalysisError, encode_json
from src.llm_provider import LLMConfig, LLMError, OpenAICompatibleProvider, RequestBudget


_ACTIVE_PROVIDER = ContextVar("agent_render_provider", default=None)
_TERMINAL = {AgentState.COMPLETED, AgentState.CANCELLED, AgentState.FAILED}


class _TransientProvider:
    """任务仅保存无凭据代理；真实 Provider 只存在于当前按钮调用的上下文。"""

    def complete_json(self, messages):
        provider = _ACTIVE_PROVIDER.get()
        if provider is None:
            raise LLMError("当前没有授权的模型请求上下文。")
        return provider.complete_json(messages)

    @property
    def last_metrics(self):
        provider = _ACTIVE_PROVIDER.get()
        return getattr(provider, "last_metrics", None)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cancel_task(runner, dataset, *, reject_plan=False):
    """仅捕获预期状态错误；不展示异常正文，不谎称取消成功。"""
    try:
        if reject_plan:
            runner.approve_plan(dataset_id=dataset, command_id="reject-plan", confirmed=False)
        else:
            runner.cancel(dataset_id=dataset)
    except AgentError:
        st.session_state["agent_cancel_warning"] = (
            "取消或拒绝操作未获确认：任务或数据集标识可能已过期。"
            "请核对当前任务状态；本操作没有执行 SQL 或请求模型。")
        return False
    return True


def _show_cancel_warning():
    warning = st.session_state.pop("agent_cancel_warning", None)
    if warning:
        st.warning(warning)


def _discard_task(message):
    runner = st.session_state.pop("agent_task", None)
    dataset = st.session_state.pop("agent_task_dataset", None)
    if runner is not None:
        _cancel_task(runner, dataset)
    st.session_state["agent_revision"] = uuid4().hex
    st.session_state["agent_notice"] = message
    # 旧的外发和 SQL 复选框不能在目标改回原值时复活。
    for key in list(st.session_state):
        if key.startswith("agent_consent_"):
            del st.session_state[key]


def invalidate_agent_dataset(dataset_key):
    """上传移除、读取失败及新数据源时均调用；不依赖 Agent 区域是否展开。"""
    if st.session_state.get("agent_dataset") != dataset_key:
        _discard_task("数据集已变化，旧 Agent 任务及审批已失效。")
        st.session_state["agent_dataset"] = dataset_key
        st.session_state.pop("agent_binding", None)
        _show_cancel_warning()


def _invoke(runner, method, config, *, model=False, **kwargs):
    """按钮调用范围内绑定 Provider，finally 清理，不存入 session_state。"""
    token = None
    try:
        if model:
            token = _ACTIVE_PROVIDER.set(OpenAICompatibleProvider(config))
        with st.spinner("Agent 正在处理，请勿重复提交……"):
            getattr(runner, method)(**kwargs)
    except (AgentError, AIAnalysisError, LLMError):
        # 服务层已保留脱敏失败原因；不展示原始 Provider 异常或模型正文。
        pass
    finally:
        if token is not None:
            _ACTIVE_PROVIDER.reset(token)
    st.rerun()


def _show_requests(snapshot, budget):
    st.caption(f"任务模型请求：{len(snapshot.calls)}/6；共享会话请求：{budget.used}/{budget.limit}（失败也计数，不是金额预算）。")
    for index, call in enumerate(snapshot.calls, 1):
        st.caption(f"Agent 请求 {index}：耗时 {call.elapsed_seconds:.3f} 秒；调用{'成功' if call.success else '失败'}。")
        if call.usage is None:
            st.caption("服务商未返回有效 Token 用量；未估算 Token 或费用。")
        else:
            st.text("服务商实际 Token 用量：" + encode_json(call.usage))


def _show_results(snapshot):
    for index, result in enumerate(snapshot.results):
        st.write(f"已完成步骤 {index + 1}")
        st.text(snapshot.steps[index].title)
        st.code(snapshot.sql_history[index], language="sql")
        st.caption(f"实际返回 {len(result.data)} 行。")
        st.dataframe(result.data, hide_index=True)
        if result.truncated:
            st.warning(f"步骤 {index + 1} 结果已截断，只展示安全执行器返回的部分结果。")
        if result.data.empty:
            st.info(f"步骤 {index + 1} 查询成功，结果为空。")


def _show_local_report(snapshot, schema):
    st.write("最终本地事实报告")
    st.text("分析目标：" + snapshot.goal)
    st.caption(f"已完成 {len(snapshot.results)}/{len(snapshot.steps)} 个步骤。")
    for fact in snapshot.facts:
        st.text(f"步骤 {fact['step']}：实际返回 {fact['returned_rows']} 行；"
                f"结果中 NULL 单元格 {fact['null_cells']} 个；"
                f"{'部分结果' if fact['partial'] else '未被执行器截断'}。")
    summary = local_summary(snapshot.results)
    st.write("确定性数据事实摘要（本地生成）")
    for line in summary.facts:
        st.text(line)
    for note in summary.notes:
        if summary.partial:
            st.warning(note)
        else:
            st.caption(note)
    st.text(f"源数据缺失值合计：{sum(item['缺失值数量'] for item in schema['fields'])}。"
            "NULL 表示缺失或查询产生的空值，不等于零；聚合结果不代表源数据没有缺失。")
    st.info("数据事实来自上方已完成查询的结果表。返回行数不一定是源数据总数；"
            "分析口径由问题、计划和 SQL 决定，未验证因果关系或业务优劣。截断结果不能代表完整数据。")


def render_agent_analysis(frame, dataset_key):
    st.subheader("AI Agent 多步骤分析")
    invalidate_agent_dataset(dataset_key)
    _show_cancel_warning()
    if not st.toggle("打开 AI Agent 多步骤分析", key="agent_enabled"):
        return
    st.caption("最多三个 SQL 步骤，逐条审核；与单次 AI 分析独立使用，共享会话模型请求预算。")
    try:
        config = LLMConfig.from_environment()
    except LLMError as exc:
        _discard_task("模型配置无效，旧任务已失效。")
        _show_cancel_warning()
        st.warning(str(exc))
        return
    if config is None:
        _discard_task("模型未配置，旧任务已失效。")
        _show_cancel_warning()
        st.info("Agent 模型未配置，请在本地 .env 配置 DeepSeek；上传、预览和手动 SQL 仍可使用。")
        return
    # 指纹使用配置声明字段；服务商实际 Token 参数的转换由 Provider 负责。
    binding = _digest(encode_json([dataset_key, config.base_url, config.model,
                                  config.token_parameter, config.json_mode,
                                  config.max_output_tokens]))
    if st.session_state.get("agent_binding") != binding:
        _discard_task("数据源或模型配置已更新，请重新授权 Agent 任务。")
        st.session_state["agent_binding"] = binding
    st.caption(f"模型已配置；请求接收方：{urlsplit(config.base_url).hostname}。")
    budget = st.session_state.setdefault("ai_api_budget", RequestBudget())
    st.caption(f"共享会话模型请求预算：{budget.used}/{budget.limit}；任务最多6次请求，无自动重试。")
    schema = inspect_schema(frame)
    with st.expander("Agent 数据集字段信息（仅本地）"):
        st.caption(f"{schema['rows']} 行，{schema['columns']} 列；以下缺失统计不进入规划或 SQL 请求。")
        st.dataframe(schema["fields"], hide_index=True)
    goal = st.text_area("Agent 多步骤分析目标", max_chars=1200, key="agent_goal_" + binding)
    version = _digest(binding + goal)
    if st.session_state.get("agent_goal_version") != version:
        _discard_task("分析目标已变化，旧计划及 SQL 审批已失效。")
        st.session_state["agent_goal_version"] = version
    _show_cancel_warning()
    if st.session_state.get("agent_notice"):
        st.caption(st.session_state["agent_notice"])
    runner = st.session_state.get("agent_task")
    flow = st.session_state["agent_revision"]

    if runner is None:
        st.warning("规划将外发字段名称、类型及分析目标，不发送数据行、样本或文件。"
                   "字段和目标也可能含敏感信息；云端请求可能收费并存在数据传输风险。")
        messages = None
        if goal.strip():
            try:
                messages = planning_messages(frame, goal)
                with st.expander("规划请求的精确外发内容"):
                    st.text(encode_json(messages))
            except AIAnalysisError as exc:
                st.error(str(exc))
        consent = st.checkbox("我确认发送上述目标和字段结构用于规划", key="agent_consent_plan_" + flow)
        if st.button("生成 Agent 分析计划", disabled=not consent or messages is None,
                     key="agent_plan_" + flow):
            runner = AgentRunner(frame, dataset_key, goal, _TransientProvider(), budget)
            st.session_state["agent_task"] = runner
            st.session_state["agent_task_dataset"] = dataset_key
            st.session_state["agent_notice"] = ""
            _invoke(runner, "plan", config, model=True, dataset_id=dataset_key,
                    command_id="plan", confirmed=consent)
        return

    snapshot = runner.snapshot()
    st.text("Agent 状态：" + snapshot.state.value)
    _show_requests(snapshot, budget)
    if snapshot.steps:
        st.write("完整分析计划（模型草拟，尚不是数据事实）")
        for index, step in enumerate(snapshot.steps, 1):
            st.text(f"步骤 {index}：{step.title}\n分析问题：{step.question}")
    if snapshot.failure:
        st.error(snapshot.failure)
    detail = getattr(snapshot, "failure_detail", None)
    if detail:
        step_label = str(detail.step) if detail.step is not None else "不适用（规划、审批或报告阶段）"
        st.text(f"失败步骤：{step_label}\n失败阶段：{detail.stage}\n安全错误类别：{detail.category}")
        st.text("本步骤 SQL：" + ("已提交安全执行器，是否实际运行或完成未确认；不视为成功。"
                                    if detail.sql_submitted else "未执行（未提交安全执行器）。"))
        st.text(f"本操作模型请求：{detail.model_requests_consumed} 次；"
                + ("已消耗请求预算。" if detail.model_requests_consumed else "未消耗新的模型请求。"))
        if getattr(detail, "ast_node", None) or getattr(detail, "ast_reason", None):
            st.text(f"安全 AST 节点：{getattr(detail, 'ast_node', None) or '未定位'}；"
                    f"拒绝原因代码：{getattr(detail, 'ast_reason', None) or '未记录'}。")
        if detail.category == "PLAN_STEP_COUNT_MISMATCH":
            st.info("模型计划与目标中的明确步骤数不一致，整份计划已拒绝，没有截断或执行额外步骤。请新建任务重新规划。")
    if snapshot.proposal and snapshot.state in _TERMINAL:
        st.write("任务停止时的 SQL（仅供本地审计）")
        st.code(snapshot.proposal.sql, language="sql")
        st.caption("任务已经终止，此处不提供执行按钮。")
    with st.expander("Agent 本地审计日志"):
        st.dataframe([{"序号": event.sequence, "UTC时间": event.timestamp,
                       "事件": event.kind, "状态": event.state.value,
                       "步骤": str(event.step) if event.step is not None else "不适用",
                       "阶段": getattr(event, "stage", None),
                       "安全错误类别": getattr(event, "category", None),
                       "已提交SQL执行器": getattr(event, "sql_submitted", None),
                       "本操作模型请求数": getattr(event, "model_requests_consumed", None),
                       "AST节点类型": getattr(event, "ast_node", None),
                       "AST拒绝原因": getattr(event, "ast_reason", None),
                       "内容散列": event.fingerprint}
                      for event in snapshot.audit], hide_index=True)
    if snapshot.state not in _TERMINAL:
        if st.button("取消 Agent 任务", key="agent_cancel_" + snapshot.task_id):
            _cancel_task(runner, dataset_key)
            st.rerun()
    else:
        if st.button("新建 Agent 任务（重新授权）", key="agent_new_" + snapshot.task_id):
            _discard_task("请重新检查目标并授权新任务。")
            st.rerun()
    if snapshot.state != AgentState.COMPLETED:
        if snapshot.steps and len(snapshot.results) == len(snapshot.steps):
            _show_local_report(snapshot, schema)
        _show_results(snapshot)
    if snapshot.approval == "plan":
        if st.button("批准 Agent 完整计划", key="agent_approve_plan_" + snapshot.task_id):
            _invoke(runner, "approve_plan", config, dataset_id=dataset_key,
                    command_id="approve-plan", confirmed=True)
        if st.button("拒绝计划并取消任务", key="agent_reject_plan_" + snapshot.task_id):
            _cancel_task(runner, dataset_key, reject_plan=True)
            st.rerun()
    elif snapshot.state in {AgentState.PLANNED, AgentState.STEP_COMPLETED}:
        step = len(snapshot.results)
        st.write(f"当前步骤 {step + 1}：准备生成 SQL")
        with st.expander("当前 SQL 生成请求的精确外发内容"):
            st.text(encode_json(runner.preview_step(dataset_id=dataset_key)))
        consent = st.checkbox("我确认发送当前步骤的字段与分析问题",
                              key=f"agent_consent_generate_{snapshot.task_id}_{step}")
        if st.button("生成当前步骤 SQL", disabled=not consent,
                     key=f"agent_generate_{snapshot.task_id}_{step}"):
            _invoke(runner, "generate_step", config, model=True, dataset_id=dataset_key,
                    command_id=f"generate-{step}", confirmed=consent)
    elif snapshot.approval == "sql":
        st.write(f"当前步骤 {len(snapshot.results) + 1}：等待 SQL 审批")
        st.code(snapshot.proposal.sql, language="sql")
        st.text("生成说明（未经计算）：" + snapshot.proposal.explanation)
        st.success("SQL 已通过现有 AST 白名单及字段校验；执行时将再次校验，并使用独立进程和原资源限制。")
        consent = st.checkbox("我已审核当前 SQL，批准本步骤在本机执行",
                              key="agent_consent_sql_" + snapshot.proposal_id)
        if st.button("执行当前步骤 SQL", disabled=not consent,
                     key="agent_execute_" + snapshot.proposal_id):
            _invoke(runner, "execute_step", config, dataset_id=dataset_key,
                    command_id="execute-" + snapshot.proposal_id,
                    proposal_id=snapshot.proposal_id, confirmed=consent)
    elif snapshot.state == AgentState.COMPLETED:
        _show_local_report(snapshot, schema)
        st.write("关键数据事实：各步骤实际返回的查询结果")
        _show_results(snapshot)
        if snapshot.report:
            try:
                report_view = readable_report_view(snapshot.report)
            except Exception:
                st.warning("报告预览无法安全生成，请查看本地完整结果表；未发送数据或调用模型。")
                return
            for title, lines in (("已校验的数据事实", report_view.facts),
                                 ("模型推断（尚未证实）", report_view.inferences),
                                 ("建议（不是统计结论）", report_view.suggestions),
                                 ("报告范围与 NULL 说明", report_view.notes)):
                st.write(title)
                for line in lines:
                    st.text(line)
            return
        try:
            packet = runner.preview_report(dataset_id=dataset_key)
        except AIAnalysisError as exc:
            st.info(str(exc))
            return
        except Exception:
            st.warning("报告预览无法安全生成，请查看本地完整结果表；未发送数据或调用模型。")
            return
        st.write("可选：DeepSeek 自然语言报告")
        st.warning("报告将另行发送下方精确预览；所有步骤共用最多20个结果单元格（4列长表）、8KB上限。"
                   "每行代表一个带出处的查询结果单元格，可能不覆盖所有步骤；单元格最多200字符。"
                   "不发送原文件、额外样本、问题或 SQL。调用可能收费，请检查敏感信息。")
        if packet.partial:
            st.warning("报告仅基于已发送的部分结果，不代表完整查询或全部步骤。")
        for note in packet.notes:
            st.warning(note)
        with st.expander("报告请求的精确外发结果"):
            st.text(packet.json_text)
        consent = st.checkbox("我单独确认将上述有限结果发送给模型生成报告",
                              key="agent_consent_report_" + snapshot.task_id)
        if st.button("生成 DeepSeek 分析报告", disabled=not consent,
                     key="agent_report_" + snapshot.task_id):
            _invoke(runner, "summarize", config, model=True, dataset_id=dataset_key,
                    command_id="report", preview_json=packet.json_text, confirmed=consent)
