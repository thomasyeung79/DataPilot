"""使用内存样例验证读取与统计，不包含用户数据。"""

from io import BytesIO
import unittest

import pandas as pd

from src.data_loader import DataReadError, MAX_FILE_BYTES, list_sheets, read_data
from src.profiling import summarize_fields


class DataTests(unittest.TestCase):
    def test_csv_and_profile(self):
        frame = read_data("姓名,金额\n小王,12\n小李,\n".encode("utf-8-sig"), "样例.CSV")
        self.assertEqual(frame.shape, (2, 2))
        summary = summarize_fields(frame)
        self.assertEqual(summary["字段名称"].tolist(), ["姓名", "金额"])
        self.assertEqual(summary["缺失值数量"].tolist(), [0, 1])
        self.assertEqual(summary["缺失率（%）"].tolist(), [0.0, 50.0])
        self.assertEqual(summary["数据类型"].tolist(), [str(t) for t in frame.dtypes])

    def test_encoding_and_separator(self):
        frame = read_data("城市;数量\n上海;3".encode("gb18030"), "样例.csv", encoding="gb18030", separator=";")
        self.assertEqual(frame.iloc[0, 0], "上海")

    def test_quoted_csv(self):
        frame = read_data(b'a,b\n"first,second",1\n"two\nlines",2\n', "test.csv")
        self.assertEqual(frame.shape, (2, 2))
        self.assertEqual(frame.iloc[1, 0], "two\nlines")

    def test_multiple_sheets(self):
        buffer = BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            pd.DataFrame({"数值": [1]}).to_excel(writer, sheet_name="一月", index=False)
            pd.DataFrame({"数值": [2, 3]}).to_excel(writer, sheet_name="二月", index=False)
            pd.DataFrame().to_excel(writer, sheet_name="空表", index=False)
        data = buffer.getvalue()
        self.assertEqual(list_sheets(data), ["一月", "二月", "空表"])
        self.assertEqual(read_data(data, "样例.xlsx", "二月")["数值"].tolist(), [2, 3])
        for sheet in ["空表", "不存在"]:
            with self.assertRaises(DataReadError):
                read_data(data, "样例.xlsx", sheet)

    def test_header_only(self):
        frame = read_data(b"a,b\n", "test.csv")
        self.assertEqual(frame.shape, (0, 2))
        self.assertEqual(summarize_fields(frame)["缺失率（%）"].tolist(), [0.0, 0.0])

    def test_invalid_files(self):
        cases = [(b"", "a.csv"), (b" \n", "a.csv"), (b"bad", "a.xlsx"),
                 (b"a,b\n1,2,3", "a.csv"), (b"a,b\n1", "a.csv"),
                 (b'a,b\n"unclosed,2', "a.csv"), (b"\xff", "a.csv"),
                 (b"a", "a.txt"), (b"x" * (MAX_FILE_BYTES + 1), "a.csv")]
        for data, name in cases:
            with self.subTest(name=name, size=len(data)):
                with self.assertRaises(DataReadError):
                    read_data(data, name)
        with self.assertRaises(DataReadError):
            list_sheets(b"broken")


if __name__ == "__main__":
    unittest.main()
