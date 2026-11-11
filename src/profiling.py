"""生成字段级数据概况。"""

import pandas as pd


def summarize_fields(frame: pd.DataFrame) -> pd.DataFrame:
    """按列位置统计，兼容字段名重复的 DataFrame。"""
    missing = frame.isna().sum().to_numpy()
    return pd.DataFrame({
        "字段名称": [str(column) for column in frame.columns],
        "数据类型": [str(dtype) for dtype in frame.dtypes],
        "缺失值数量": missing,
        "缺失率（%）": (missing / len(frame) * 100).round(2) if len(frame) else 0.0,
    })
