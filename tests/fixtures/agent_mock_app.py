"""仅本地浏览器验收夹具；模拟上传与模型，阻断所有 requests HTTP 请求。

启动：python -m streamlit run tests/fixtures/agent_mock_app.py --server.address 127.0.0.1
不得用此夹具进行真实 API 验收；实际产品入口仍是 app.py。
"""

import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st

from src.llm_provider import CallMetrics, LLMConfig
from app import main


class SyntheticUpload:
    name = "纯虚构演示.csv"

    def getvalue(self):
        return b"location,income,hours\nA,100,5\nB,80,4\n,20,0"


class LocalMockProvider:
    def __init__(self):
        self.last_metrics = None

    def complete_json(self, messages):
        self.last_metrics = CallMetrics(0.001, {"prompt_tokens": 10, "completion_tokens": 5})
        prompt = messages[0]["content"]
        if "规划器" in prompt:
            return json.dumps({"steps": [
                {"title": "记录数量", "question": "统计总记录数"},
                {"title": "地点收入", "question": "按 location 汇总 income"},
                {"title": "数据质量", "question": "统计 location 缺失记录数"}]}, ensure_ascii=False)
        if "facts" in prompt:
            return json.dumps({"facts": [{"kind": "cell", "row": 0, "column": "值"}],
                               "inferences": ["insufficient_context"],
                               "suggestions": ["review_missing_values"]})
        question = json.loads(messages[1]["content"])["question"]
        question = question.split("已批准的当前步骤：", 1)[-1]
        query = "SELECT COUNT(*) AS total FROM uploaded_data"
        if "地点收入" in question:
            query = "SELECT location, SUM(income) AS total FROM uploaded_data GROUP BY location ORDER BY total DESC"
        elif "数据质量" in question:
            query = "SELECT COUNT(*) AS missing FROM uploaded_data WHERE location IS NULL"
        return json.dumps({"sql": query, "explanation": "本地 Mock 草案，不调用云端。"}, ensure_ascii=False)


if __name__ == "__main__":
    provider = LocalMockProvider()
    with patch("streamlit.file_uploader", return_value=SyntheticUpload()), \
            patch("src.agent_ui.LLMConfig.from_environment", return_value=LLMConfig("mock-browser-not-a-real-key")), \
            patch("src.agent_ui.OpenAICompatibleProvider", return_value=provider), \
            patch("src.ai_ui.OpenAICompatibleProvider", return_value=provider), \
            patch("requests.sessions.Session.request", side_effect=AssertionError("Mock 夹具禁止 HTTP")):
        main()
        st.info("本页为纯虚构数据的本地 Mock 验收夹具；requests HTTP 已阻断，Token 数字为模拟返回值。")
