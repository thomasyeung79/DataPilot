"""Streamlit 三步确认流程；仅按钮事件触发外部请求和本地查询。"""

import hashlib
from uuid import uuid4
from urllib.parse import urlsplit

import streamlit as st

from src.ai_analysis import (
    AIAnalysisError, build_explanation_packet, build_generation_payload, encode_json,
    execute_proposal, explain_result, generate_sql,
)
from src.llm_provider import BudgetedProvider, CallMetrics, LLMConfig, LLMError, OpenAICompatibleProvider, RequestBudget
from src.sql_analysis import SQLAnalysisError


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _show_metrics(metrics):
    if isinstance(metrics, CallMetrics):
        st.caption(f"本次请求耗时：{metrics.elapsed_seconds:.3f} 秒。")
        if metrics.usage is None:
            st.caption("服务商未返回有效 Token 用量；未估算 Token 或费用。")
        else:
            st.text("服务商实际返回的 Token 用量：" + encode_json(metrics.usage))


def _show_budget(placeholder, budget):
    placeholder.caption(f"本会话已尝试 {budget.used}/{budget.limit} 次模型请求（失败也计数）；刷新会话可重置，此计数不是金额预算。")


def render_ai_analysis(frame, dataset_key: str) -> None:
    st.subheader("AI 自然语言分析")
    try:
        config = LLMConfig.from_environment()
    except LLMError as exc:
        st.warning(str(exc))
        return
    if config is None:
        st.info("模型未配置：默认使用 DeepSeek，请在本地 .env 填写 DeepSeek 的 DATAPILOT_API_KEY；其他提供商需同时配置 DATAPILOT_BASE_URL 和 DATAPILOT_MODEL。上传和手动 SQL 功能仍可使用。")
        return

    # 不在 session_state 保存配置对象或密钥，不展示 Base URL 路径及凭据。
    host = urlsplit(config.base_url).hostname
    st.caption(f"模型已配置；请求接收方：{host}。仅在主动确认并点击按钮后调用，服务商可能收费。")
    # 与 Agent 一致，页面身份只依赖声明配置字段，不依赖 Provider 的派生属性。
    context = _digest(encode_json([dataset_key, config.base_url, config.model, config.token_parameter,
                                  config.json_mode, config.max_output_tokens]))
    if "ai_api_budget" not in st.session_state:
        st.session_state["ai_api_budget"] = RequestBudget()
    budget = st.session_state["ai_api_budget"]
    budget_status = st.empty()
    _show_budget(budget_status, budget)
    if budget.used >= budget.limit:
        reset = st.checkbox("我确认重置请求次数，继续调用可能产生额外费用")
        if st.button("重置本会话请求计数", disabled=not reset):
            budget.used = 0
            _show_budget(budget_status, budget)
    if st.session_state.get("ai_context") != context:
        st.session_state["ai_context"] = context
        st.session_state["ai_workflow"] = {}
    question = st.text_area("分析问题（中文或英文）", max_chars=2000, key="ai_question_" + context)
    version = _digest(context + question)
    workflow = st.session_state["ai_workflow"]
    if workflow.get("version") != version:
        workflow = {"version": version}
        st.session_state["ai_workflow"] = workflow

    st.warning("生成 SQL 将向所配置的模型服务发送字段名称、数据类型和当前问题，不发送数据行、样本或原始文件。字段名称和问题本身也可能包含敏感信息，请先检查。")
    payload = None
    if question.strip():
        try:
            payload = build_generation_payload(frame, question)
            with st.expander("查看将发送的字段与问题"):
                st.text(encode_json(payload))
        except AIAnalysisError as exc:
            st.error(str(exc))
    send_confirmed = st.checkbox("我确认将上述字段与当前问题发送给模型服务", key="ai_send_" + version)
    provider = BudgetedProvider(OpenAICompatibleProvider(config), budget)
    if st.button("生成 SQL", disabled=payload is None or not send_confirmed, key="ai_generate_" + version):
        # 先清除旧草案、执行结果和解释，即使本次失败也不显示过时结果。
        workflow.clear()
        workflow["version"] = version
        workflow["proposal_id"] = uuid4().hex
        try:
            with st.spinner("正在请求模型生成 SQL……"):
                workflow["proposal"] = generate_sql(frame, question, provider, confirmed=send_confirmed)
        except (AIAnalysisError, LLMError, SQLAnalysisError) as exc:
            workflow["error"] = str(exc)
        finally:
            workflow["generation_metrics"] = provider.last_metrics
            _show_budget(budget_status, budget)
    _show_metrics(workflow.get("generation_metrics"))
    if workflow.get("error"):
        st.error(workflow["error"])
    proposal = workflow.get("proposal")
    if proposal is None:
        return
    st.caption("生成说明（模型草拟，尚未计算，不作为数据事实）：")
    st.text(proposal.explanation)
    if proposal.sql is None:
        st.info("请根据上述说明澄清问题或补充字段，本次没有可执行的 SQL。")
        return
    st.code(proposal.sql, language="sql")
    proposal_key = _digest(version + proposal.sql + workflow["proposal_id"])
    execute_confirmed = st.checkbox("我已检查上述 SQL，确认在本机执行", key="ai_execute_consent_" + proposal_key)
    if st.button("确认执行生成的 SQL", disabled=not execute_confirmed, key="ai_execute_" + proposal_key):
        for key in ["result", "packet", "explanation", "execution_error", "explanation_error"]:
            workflow.pop(key, None)
        workflow["result_id"] = uuid4().hex
        try:
            with st.spinner("正在通过安全引擎执行 SQL……"):
                workflow["result"] = execute_proposal(frame, proposal, confirmed=execute_confirmed)
        except (AIAnalysisError, SQLAnalysisError) as exc:
            workflow["execution_error"] = str(exc)
    if workflow.get("execution_error"):
        st.error(workflow["execution_error"])
    result = workflow.get("result")
    if result is None:
        return
    st.metric("AI 查询返回行数", len(result.data))
    st.dataframe(result.data, hide_index=True)
    if result.truncated:
        st.warning("安全引擎截断了结果，仅保留前 1000 行，结果解释无法代表未返回的数据。")
    if result.data.empty:
        st.info("查询成功但没有符合条件的数据，不调用模型解释。")
        return

    st.write("可选：AI 结果解释")
    try:
        packet = build_explanation_packet(result)
    except AIAnalysisError as exc:
        st.warning(str(exc))
        return
    st.warning(f"解释将另行发送下方预览的查询结果：{packet.sent_rows} 行，最多 20 列、8 KB，单元格最多 200 字符。不发送原始文件、原问题或 SQL。请确认其中不含敏感内容。")
    if packet.partial:
        st.warning("仅发送部分查询结果（可能省略行、列或缩短单元格）；解释只基于下方实际发送的部分内容。")
    for note in packet.notes:
        st.warning(note)
    with st.expander("查看解释请求将发送的全部结果内容"):
        st.text(packet.json_text)
    packet_key = _digest(proposal_key + packet.json_text + workflow["result_id"])
    explanation_confirmed = st.checkbox("我单独确认将上述有限查询结果发送给模型解释", key="ai_explain_consent_" + packet_key)
    if st.button("请求 AI 解释结果", disabled=not explanation_confirmed, key="ai_explain_" + packet_key):
        workflow.pop("explanation", None)
        workflow.pop("explanation_error", None)
        try:
            with st.spinner("正在请求并核验结果解释……"):
                workflow["explanation"] = explain_result(packet, provider, confirmed=explanation_confirmed)
        except (AIAnalysisError, LLMError) as exc:
            workflow["explanation_error"] = str(exc)
        finally:
            workflow["explanation_metrics"] = provider.last_metrics
            _show_budget(budget_status, budget)
    _show_metrics(workflow.get("explanation_metrics"))
    if workflow.get("explanation_error"):
        st.error(workflow["explanation_error"])
    explanation = workflow.get("explanation")
    if explanation:
        for title, lines in [
            ("数据事实（引用发送结果并经本地校验）", explanation.facts),
            ("模型推断边界（尚未证实）", explanation.inferences),
            ("建议（不作为统计结论）", explanation.suggestions),
        ]:
            st.write(title)
            for line in lines:
                # 使用纯文本，防止模型或字段值中的 Markdown 链接/图片引发外部加载。
                st.text(line)
