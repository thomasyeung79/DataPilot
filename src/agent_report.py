"""确定性本地事实摘要，以及跨步骤共享上限的外发结果包。"""

from dataclasses import dataclass, replace
from decimal import Decimal
import json
import math
from numbers import Integral, Real

import pandas as pd

from src.ai_analysis import (AIAnalysisError, ExplanationPacket, build_explanation_packet,
                             _valid_encoded_cell, _safe_cell, result_rows)
from src.sql_analysis import QueryResult


@dataclass(frozen=True)
class LocalSummary:
    facts: tuple[str, ...]
    notes: tuple[str, ...]
    partial: bool


_FIELD_LABELS = {"location": "地点", "total_income": "总收入", "total_hours": "总工时",
                 "weighted_avg_hourly": "加权时薪", "hourly_rate": "时薪"}


def _display_value(value, *, encoded=False):
    """格式化实际标量，不把 NULL、0、空字符串或非有限数混为一谈。"""
    if encoded and type(value) is dict and _valid_encoded_cell(value):
        if value["type"] == "decimal":
            return value["value"], False
        return "【安全替换标记：" + value["reason"] + "，不是 NULL 或0】", True
    if not pd.api.types.is_scalar(value):
        return "【异常非标量值，查看结果表】", True
    if isinstance(value, Decimal):
        return (str(value), False) if value.is_finite() else (f"【非有限数值 {value}，不是 NULL】", True)
    if isinstance(value, Real) and not isinstance(value, Integral) and not math.isfinite(value):
        return f"【非有限数值 {'NaN' if math.isnan(value) else str(value)}，不是 NULL】", True
    if value is None or pd.isna(value):
        return "NULL（null）", False
    if isinstance(value, (bool,)):
        return str(value), False
    if isinstance(value, Integral):
        return str(value), False
    if isinstance(value, Real):
        numeric = float(value)
        if not math.isfinite(numeric):
            return "【非有限数值，不作为有效统计事实】", True
        text = (format(numeric, ".2f").rstrip("0").rstrip(".")
                if numeric == 0 or abs(numeric) >= 0.01 else format(numeric, ".6g"))
        if numeric != float(text):
            text += "（显示约数，完整精度见结果表）"
        return text, False
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else "【非有限数值】", not value.is_finite()
    text = str(value)
    if len(text) > 200:
        return json.dumps(text[:200], ensure_ascii=False) + "…（已缩短）", True
    return json.dumps(text, ensure_ascii=False), False


def _row_fact(step, row, names, values, *, encoded=False):
    fields, partial = [], False
    for name, value in zip(names, values):
        label = f"{_FIELD_LABELS[name]}（{name}）" if name in _FIELD_LABELS else name
        text, shortened = _display_value(value, encoded=encoded)
        partial |= shortened
        fields.append(f"{label}={text}")
    return f"步骤{step}，结果第{row}行：" + "；".join(fields) + "。", partial


def local_summary(results, *, max_rows=10, max_columns=20) -> LocalSummary:
    """仅逐行描述真实结果，不跨步骤合并、不补算指标、不猜测单位。"""
    facts, notes, partial = [], [], False
    for step, result in enumerate(results, 1):
        if not isinstance(result, QueryResult) or not isinstance(result.data, pd.DataFrame):
            notes.append(f"步骤{step}：结果结构异常，未生成数据事实。")
            partial = True
            continue
        frame = result.data
        names = [str(column) for column in frame.columns]
        if len(names) != len(set(names)) or not names:
            notes.append(f"步骤{step}：字段为空或重名，无法唯一引用，请查看完整结果表。")
            partial = True
            continue
        if result.truncated:
            notes.append(f"步骤{step}：查询结果已截断，摘要仅基于已返回的部分结果。")
            partial = True
        if frame.empty:
            notes.append(f"步骤{step}：查询结果为空，不补造数据事实。")
        if len(frame) > max_rows or len(names) > max_columns:
            notes.append(f"步骤{step}：摘要仅列前{max_rows}行、前{max_columns}列；完整结果表保持不变。")
            partial = True
        for row, values in enumerate(result_rows(frame.iloc[:max_rows, :max_columns]), 1):
            line, shortened = _row_fact(step, row, names[:max_columns], values)
            facts.append(line)
            if shortened:
                notes.append(f"步骤{step}结果第{row}行：文本有缩短或存在异常值，请核对完整结果表。")
                partial = True
    notes.append("仅格式化 SQL 已返回的单元格，不额外计算或跨步骤补全；小数可能为显示约数。")
    notes.append("字段名称原样保留；结果未声明单位时，不推定币种或工时单位。NULL 不等于0或空字符串。")
    return LocalSummary(tuple(facts), tuple(notes), partial)


def _packet_summary(packet) -> LocalSummary:
    """将已经核验过的有限长表按原步骤/原结果行还原为可读事实。"""
    try:
        payload = json.loads(packet.json_text)
        if (payload["columns"] != ["步骤", "结果行", "字段", "值"]
                or not isinstance(payload["rows"], list)):
            raise ValueError()
        for record in payload["rows"]:
            if (not isinstance(record, list) or len(record) != 4
                    or type(record[0]) is not int or record[0] < 1
                    or type(record[1]) is not int or record[1] < 1
                    or not isinstance(record[2], str)):
                raise ValueError()
    except (TypeError, ValueError, KeyError, AttributeError):
        raise AIAnalysisError("结果出处预览结构无效，未生成数据事实。") from None
    groups = {}
    notes, facts, partial = list(packet.notes), [], packet.partial
    for step, row, name, value in payload["rows"]:
        groups.setdefault((step, row), []).append((name, value))
    for (step, row), cells in groups.items():
        names = [cell[0] for cell in cells]
        if len(names) != len(set(names)):
            notes.append(f"步骤{step}结果第{row}行：字段重名，未生成歧义事实。")
            partial = True
            continue
        line, shortened = _row_fact(step, row, names, [cell[1] for cell in cells], encoded=True)
        facts.append(line)
        partial |= shortened
    return LocalSummary(tuple(facts), tuple(notes), partial)


@dataclass(frozen=True)
class AgentReport:
    facts: tuple[str, ...]
    inferences: tuple[str, ...]
    suggestions: tuple[str, ...]
    preview_json: str
    partial: bool
    sent_cells: int
    notes: tuple[str, ...]
    scope_notes: tuple[str, ...] = ()


def grounded_report(packet, explanation):
    """报告保留精确出处预览和局部性说明，不添加自由生成的数据事实。"""
    summary = _packet_summary(packet)
    notes = ["数据事实按预览中的来源步骤和原结果行在本地格式化，不补充未发送字段。",
             "预览中的 null 按空值显示，不代表零或空字符串；原值以本地完整结果表为准。"]
    notes.extend(summary.notes)
    notes.append("单位未声明则不推定；小数可能为显示约数，完整精度以本地结果表为准。")
    if summary.partial:
        notes.append("本解释仅基于已发送的部分结果，不代表完整查询或完整数据集。")
        notes.append("预览可能省略字段、缩短文本或使用安全替换标记；标记不是 NULL，请核对本地表。")
    return AgentReport(summary.facts, tuple(explanation.inferences),
                       tuple(explanation.suggestions), packet.json_text, summary.partial,
                       packet.sent_rows, tuple(notes), packet.notes)


def readable_report_view(report):
    """旧任务缓存也仅根据原预览重新排版，不重新请求模型、不改写审计或原对象。"""
    packet = ExplanationPacket(report.preview_json, report.sent_cells, report.partial,
                               getattr(report, "scope_notes", ()))
    return grounded_report(packet, report)


def local_facts(results: tuple[QueryResult, ...]) -> tuple[dict, ...]:
    """只描述已实际返回的数据，不推断截断前总量或业务优劣。"""
    return tuple({"step": index + 1, "returned_rows": len(result.data),
                  "columns": tuple(str(c) for c in result.data.columns),
                  "partial": result.truncated,
                  "null_cells": sum(_safe_cell(value)[0] is None for row in result_rows(result.data) for value in row)}
                 for index, result in enumerate(results))


def report_packet(results: tuple[QueryResult, ...]):
    """将实际单元格转为带出处的长表，合计最多20个结果单元格（4列长表）/8KB。

    每行代表一个结果单元格，避免不同结果表拼接产生虚假的 NULL。
    字段名也按普通单元格限长。未发送的单元格或原查询截断均标记部分结果。
    """
    cells = []
    partial = any(result.truncated or result.data.size == 0 for result in results)
    total_cells = sum(result.data.size for result in results)
    for step, result in enumerate(results, 1):
        for row, values in enumerate(result_rows(result.data), 1):
            for column, value in zip(result.data.columns, values):
                if len(cells) == 20:
                    break
                cells.append([step, row, str(column), value])
            if len(cells) == 20:
                break
        if len(cells) == 20:
            break
    if not cells:
        raise AIAnalysisError("查询没有可发送的结果单元格；请查看本地空结果摘要。")
    # 混合整数与 NULL 时禁止 Pandas 自动转浮点，避免大整数丢失精度。
    frame = pd.DataFrame(cells, columns=["步骤", "结果行", "字段", "值"], dtype=object)
    packet = build_explanation_packet(QueryResult(frame, partial or total_cells > len(cells)))
    # 以最终发送包为准（8KB 裁剪也可能移除整个步骤），不只检查最初的候选长表。
    represented = {row[0] for row in json.loads(packet.json_text)["rows"]}
    notes = list(packet.notes)
    for step, result in enumerate(results, 1):
        if step not in represented:
            if len(result.data) == 0:
                notes.append(f"步骤{step}查询成功但返回0行，没有结果单元格；该步骤未包含在发送内容中。")
            else:
                notes.append(f"步骤{step}未包含在发送内容中（无结果单元格或发送上限）；请查看本地完整结果表。")
    return replace(packet, notes=tuple(notes))
