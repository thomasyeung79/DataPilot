"""固定模拟数据的 SQL 结果评估，不会自行调用模型或外部服务。"""

from dataclasses import dataclass
import math
from numbers import Real
from pathlib import Path

import pandas as pd

from src.ai_analysis import SQLProposal, execute_proposal
from src.data_loader import read_data


def load_demo_frame() -> pd.DataFrame:
    path = Path(__file__).resolve().parents[1] / "examples" / "demo_income.csv"
    return read_data(path.read_bytes(), path.name)


# NULLIF 未在第二阶段开放。右侧不含零工时组，LEFT JOIN 使其分母自然为 NULL。
WEIGHTED_RATE_SQL = '''SELECT t.location, t.total_income, t.total_hours,
    ROUND(t.total_income / d.total_hours, 2) AS hourly_rate
FROM (SELECT location, SUM(income) AS total_income, SUM(hours) AS total_hours
      FROM uploaded_data GROUP BY location) t
LEFT JOIN (SELECT location, SUM(hours) AS total_hours FROM uploaded_data
           GROUP BY location HAVING SUM(hours) <> 0) d
ON t.location = d.location OR (t.location IS NULL AND d.location IS NULL)
ORDER BY hourly_rate DESC, t.location'''


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    question: str
    reference_sql: str | None
    expected: tuple[tuple, ...]


CASES = (
    EvaluationCase("count", "共有多少条记录？", "SELECT COUNT(*) FROM uploaded_data", ((8,),)),
    EvaluationCase("sum_income", "收入总和是多少？", "SELECT SUM(income) FROM uploaded_data", ((890,),)),
    EvaluationCase("avg_income", "所有记录的收入算术均值是多少？", "SELECT AVG(income) FROM uploaded_data", ((111.25,),)),
    EvaluationCase("sum_hours", "总工时是多少？", "SELECT SUM(hours) FROM uploaded_data", ((22,),)),
    EvaluationCase("where_income", "收入至少100的记录有几条？", "SELECT COUNT(*) FROM uploaded_data WHERE income >= 100", ((4,),)),
    EvaluationCase("where_location", "地点A收入合计是多少？", "SELECT SUM(income) FROM uploaded_data WHERE location = 'A'", ((400,),)),
    EvaluationCase("zero_hours", "零工时记录有多少条？", "SELECT COUNT(*) FROM uploaded_data WHERE hours = 0", ((3,),)),
    EvaluationCase("group_count", "各地点有几条记录，按地点升序？", "SELECT location, COUNT(*) FROM uploaded_data GROUP BY location ORDER BY location", (("A", 3), ("B", 3), ("C", 1), ("D", 1))),
    EvaluationCase("group_income", "各地点总收入，按地点升序？", "SELECT location, SUM(income) FROM uploaded_data GROUP BY location ORDER BY location", (("A", 400), ("B", 400), ("C", 90), ("D", 0))),
    EvaluationCase("group_hours", "各地点总工时，按地点升序？", "SELECT location, SUM(hours) FROM uploaded_data GROUP BY location ORDER BY location", (("A", 12), ("B", 10), ("C", 0), ("D", 0))),
    EvaluationCase("month_income", "按现成month字段统计收入，月份升序。", "SELECT month, SUM(income) FROM uploaded_data GROUP BY month ORDER BY month", (("2026-01", 390), ("2026-02", 500))),
    EvaluationCase("month_hours", "按月份合计工时，月份升序。", "SELECT month, SUM(hours) FROM uploaded_data GROUP BY month ORDER BY month", (("2026-01", 6), ("2026-02", 16))),
    EvaluationCase("top_income", "按收入合计降序列出前两个地点；收入相同时地点升序。", "SELECT location, SUM(income) AS total FROM uploaded_data GROUP BY location ORDER BY total DESC, location LIMIT 2", (("A", 400), ("B", 400))),
    EvaluationCase("filtered_avg", "正工时记录的收入均值？", "SELECT AVG(income) FROM uploaded_data WHERE hours > 0", ((160,),)),
    EvaluationCase("where_in", "地点B和C收入总和？", "SELECT SUM(income) FROM uploaded_data WHERE location IN ('B', 'C')", ((490,),)),
    EvaluationCase("month_count", "Count records per month ordered by month ascending.", "SELECT month, COUNT(*) FROM uploaded_data GROUP BY month ORDER BY month", (("2026-01", 3), ("2026-02", 5))),
    EvaluationCase("weighted_rate", "计算各地点加权平均时薪，定义为所有记录收入合计/所有记录工时合计，保留零工时收入；零总工时地点返回NULL，四舍五入2位。依次返回location、total_income、total_hours、hourly_rate，按时薪降序、地点升序，NULL排最后。", WEIGHTED_RATE_SQL, (("B", 400, 10, 40.0), ("A", 400, 12, 33.33), ("C", 90, 0, None), ("D", 0, 0, None))),
    EvaluationCase("overall_rate", "所有记录收入合计除以总工时，保留零工时收入，结果保留2位小数。", "SELECT ROUND(SUM(income) / SUM(hours), 2) FROM uploaded_data", ((40.45,),)),
    EvaluationCase("clarify_rate", "平均时薪是多少？", None, ()),
    EvaluationCase("clarify_month", "日期字段格式不明且没有月份字段时，按日期提取月份。", None, ()),
)


def result_matches(actual: pd.DataFrame, expected: tuple[tuple, ...]) -> bool:
    """比对实际有序结果的形状、空值、文本与数值；允许不同输出别名。"""
    if len(actual) != len(expected) or (expected and actual.shape[1] != len(expected[0])):
        return False
    for actual_row, expected_row in zip(actual.itertuples(index=False, name=None), expected):
        for value, wanted in zip(actual_row, expected_row):
            if wanted is None:
                if not pd.isna(value):
                    return False
            elif isinstance(wanted, Real):
                if not isinstance(value, Real) or not math.isfinite(float(value)) or not math.isclose(float(value), float(wanted), rel_tol=1e-8, abs_tol=1e-6):
                    return False
            elif value != wanted:
                return False
    return True


def evaluate_proposal(frame: pd.DataFrame, case: EvaluationCase, proposal: SQLProposal, *, confirmed=False) -> bool:
    """只评估已获执行确认的草案；不以 SQL 字符串相同作为正确性标准。"""
    if case.reference_sql is None:
        return proposal.sql is None and bool(proposal.explanation.strip())
    result = execute_proposal(frame, proposal, confirmed=confirmed)
    return not result.truncated and result_matches(result.data, case.expected)
