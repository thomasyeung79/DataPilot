"""固定的本地工具接口；模型文本不能选择任意函数或改变执行限制。"""

from types import MappingProxyType

from src.ai_analysis import execute_proposal, explain_result
from src.profiling import summarize_fields


def inspect_schema(frame) -> dict:
    """完整概况只留在本地，不自动进入模型请求。"""
    return {"rows": len(frame), "columns": len(frame.columns),
            "fields": summarize_fields(frame).to_dict(orient="records")}


def run_safe_sql(frame, proposal, *, confirmed=False):
    """再次校验实际字段，复用原有独立进程、超时、行数及内存限制。"""
    return execute_proposal(frame, proposal, confirmed=confirmed)


def summarize_results(packet, provider, *, confirmed=False):
    """复用受限事实引用协议；未经独立授权不能调用模型。"""
    return explain_result(packet, provider, confirmed=confirmed)


TOOLS = MappingProxyType({"inspect_schema": inspect_schema,
                          "run_safe_sql": run_safe_sql,
                          "summarize_results": summarize_results})
