"""读取用户文件；只在内存中处理数据，不访问网络或保存上传文件。"""

import csv
from io import BytesIO, StringIO
from pathlib import Path

import pandas as pd

MAX_FILE_BYTES = 20 * 1024 * 1024


class DataReadError(ValueError):
    """可直接显示给用户的文件读取错误。"""


def validate_file(data: bytes, filename: str) -> str:
    """校验文件大小和后缀，返回规范化后缀。"""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".xlsx"}:
        raise DataReadError("仅支持 CSV 和 XLSX 文件，请检查文件格式。")
    if not data:
        raise DataReadError("文件为空，请上传包含数据的文件。")
    if len(data) > MAX_FILE_BYTES:
        raise DataReadError("文件超过 20 MB，请拆分后上传。")
    return suffix


def list_sheets(data: bytes) -> list[str]:
    """读取工作表名称，并及时关闭工作簿。"""
    validate_file(data, "工作簿.xlsx")
    try:
        with pd.ExcelFile(BytesIO(data), engine="openpyxl") as workbook:
            names = workbook.sheet_names
        if not names:
            raise DataReadError("Excel 文件中没有可读取的工作表。")
        return names
    except DataReadError:
        raise
    except Exception as exc:
        raise DataReadError("无法读取 Excel 工作簿，请确认文件未损坏、未加密且为 XLSX 格式。") from exc


def read_data(
    data: bytes,
    filename: str,
    sheet_name: str | None = None,
    encoding: str = "utf-8-sig",
    separator: str = ",",
) -> pd.DataFrame:
    """首行作为字段名读取；不跳过格式错误的数据行。"""
    suffix = validate_file(data, filename)
    try:
        if suffix == ".csv":
            text = data.decode(encoding)
            if not text.strip():
                raise DataReadError("文件没有可读取的内容。")
            # 先校验字段数量，避免 Pandas 将多余字段当作索引或截断。
            records = csv.reader(StringIO(text), delimiter=separator, strict=True)
            width = None
            for record in records:
                if not record or (len(record) == 1 and not record[0].strip()):
                    continue
                if width is None:
                    width = len(record)
                elif len(record) != width:
                    raise DataReadError(f"CSV 第 {records.line_num} 行字段数量与表头不一致，请检查分隔符和引号。")
            frame = pd.read_csv(StringIO(text), sep=separator, on_bad_lines="error", index_col=False)
        else:
            frame = pd.read_excel(
                BytesIO(data), sheet_name=sheet_name if sheet_name is not None else 0,
                engine="openpyxl",
            )
    except DataReadError:
        raise
    except UnicodeDecodeError as exc:
        raise DataReadError("CSV 编码不匹配，请切换编码后重试。") from exc
    except pd.errors.EmptyDataError as exc:
        raise DataReadError("文件或工作表为空，没有可读取的字段。") from exc
    except (pd.errors.ParserError, csv.Error) as exc:
        raise DataReadError("CSV 格式错误，请检查分隔符、引号及每行字段数量。") from exc
    except Exception as exc:
        raise DataReadError("读取失败，请检查文件是否损坏、加密，或工作表是否存在。") from exc
    if len(frame.columns) == 0:
        raise DataReadError("文件或工作表为空，没有可读取的字段。")
    return frame
