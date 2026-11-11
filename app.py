"""DataPilot：本地文件预览、数据概况与安全只读 SQL 分析。"""

import hashlib
import streamlit as st

from src.data_loader import DataReadError, list_sheets, read_data, validate_file
from src.profiling import summarize_fields
from src.sql_analysis import SQLAnalysisError, example_queries, run_query
from src.ai_ui import render_ai_analysis
from src.agent_ui import invalidate_agent_dataset, render_agent_analysis


def main() -> None:
    st.set_page_config(page_title="DataPilot · 数据预览", page_icon="📊", layout="wide")
    st.title("📊 DataPilot")
    st.caption("DataPilot V4.1 · 本地分析、可选云端 AI 与受控多步骤 Agent")
    st.info("文件读取和 SQL 计算均在本机进行，不保存上传文件。AI 功能可选，只有主动确认后才向配置的模型服务发送明确列出的内容。")
    uploaded = st.file_uploader("上传 CSV 或 XLSX 文件（最大 20 MB）", type=["csv", "xlsx"])
    if uploaded is None:
        invalidate_agent_dataset(None)
        st.write("请选择数据文件。首行将作为字段名称；Excel 支持选择工作表。")
        return

    data = uploaded.getvalue()
    try:
        suffix = validate_file(data, uploaded.name)
        sheet_name = None
        encoding = "utf-8-sig"
        separator = ","
        if suffix == ".xlsx":
            sheet_name = st.selectbox("选择 Excel 工作表", list_sheets(data))
        else:
            left, right = st.columns(2)
            encoding = left.selectbox("CSV 文件编码", ["utf-8-sig", "gb18030", "utf-16"])
            separators = {"逗号（,）": ",", "分号（;）": ";", "制表符（Tab）": "\t", "竖线（|）": "|"}
            separator = separators[right.selectbox("CSV 分隔符", list(separators))]
        with st.spinner("正在读取本地数据……"):
            frame = read_data(data, uploaded.name, sheet_name, encoding, separator)
    except DataReadError as exc:
        invalidate_agent_dataset(None)
        st.error(str(exc))
        return

    st.success("文件读取成功。")
    rows, columns, missing = st.columns(3)
    rows.metric("总行数", f"{len(frame):,}")
    columns.metric("总列数", f"{len(frame.columns):,}")
    missing.metric("缺失值总数", f"{int(frame.isna().sum().sum()):,}")
    if frame.empty:
        st.warning("当前文件或工作表只有字段名称，没有数据行。")
    st.subheader("数据预览")
    preview_rows = st.selectbox("预览行数", [10, 20, 50, 100], index=1)
    st.dataframe(frame.head(preview_rows), hide_index=True)
    st.caption("预览仅显示前若干行；上方统计基于全部已读取数据，不包含表头。")
    st.subheader("字段与缺失值统计")
    st.dataframe(summarize_fields(frame), hide_index=True)
    st.caption("数据类型由 Pandas 自动推断；空单元格及 Pandas 默认缺失标记（如 NA、NULL）计为缺失值。")

    # 文件、工作表或读取选项变化后使用新编辑器，结果不跨数据源保留。
    dataset_key = hashlib.sha256(data).hexdigest() + str((sheet_name, encoding, separator))
    agent_dataset_key = dataset_key + hashlib.sha256(uploaded.name.encode("utf-8")).hexdigest()
    invalidate_agent_dataset(agent_dataset_key)
    render_sql_analysis(frame, dataset_key)
    render_ai_analysis(frame, dataset_key)
    render_agent_analysis(frame, agent_dataset_key)


def render_sql_analysis(frame, dataset_key: str) -> None:
    """仅在点击执行后运行 SQL，其他界面操作不会自动执行。"""
    st.subheader("SQL 数据分析")
    st.write("当前表名：`uploaded_data`。仅支持单条基础 SELECT，字段名称建议用双引号包围。")
    st.caption("每次查询限时 10 秒（包含进程启动）、最多返回 1000 行、100 列、4 MB；DuckDB 内存 128 MB，查询进程内存 1024 MB。")
    examples = example_queries(frame)
    choice = st.selectbox("选择示例查询（根据当前字段生成）", list(examples))
    editor_key = "sql_editor_" + dataset_key
    if editor_key not in st.session_state:
        st.session_state[editor_key] = examples["查看前 20 行"]
    if st.button("载入示例查询"):
        st.session_state[editor_key] = examples[choice]
    with st.form("sql_form_" + dataset_key):
        sql = st.text_area("输入 SQL 查询", key=editor_key, height=160)
        submitted = st.form_submit_button("执行 SQL 查询")
    if submitted:
        try:
            with st.spinner("正在本机执行只读查询……"):
                result = run_query(frame, sql)
            st.metric("返回行数", len(result.data))
            if result.truncated:
                st.warning("查询结果超过 1000 行，仅显示前 1000 行；请使用 WHERE 或聚合缩小结果。")
            st.dataframe(result.data, hide_index=True)
            if result.data.empty:
                st.info("查询执行成功，没有符合条件的数据。")
        except SQLAnalysisError as exc:
            st.error(str(exc))


if __name__ == "__main__":
    main()
