"""通过 Streamlit 官方测试工具验证界面状态及错误提示。"""

from pathlib import Path
from io import BytesIO
from unittest.mock import patch
import unittest

import pandas as pd
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")


class UploadedSample:
    name = "测试.csv"

    def __init__(self, content):
        self.content = content

    def getvalue(self):
        return self.content


class AppTests(unittest.TestCase):
    def test_initial_page(self):
        app = AppTest.from_file(APP).run()
        self.assertFalse(app.exception)
        self.assertEqual(app.title[0].value, "📊 DataPilot")
        self.assertEqual(len(app.metric), 0)

    def test_uploaded_csv(self):
        with patch("streamlit.file_uploader", return_value=UploadedSample(b"a,b\n1,2\n3,")):
            app = AppTest.from_file(APP).run()
            self.assertFalse(app.exception)
            self.assertEqual([metric.value for metric in app.metric], ["2", "2", "1"])
            self.assertEqual(len(app.dataframe), 2)

    def test_invalid_upload(self):
        with patch("streamlit.file_uploader", return_value=UploadedSample(b"")):
            app = AppTest.from_file(APP).run()
            self.assertFalse(app.exception)
            self.assertIn("文件为空", app.error[0].value)
            self.assertEqual(len(app.metric), 0)

    def test_excel_sheet_selection(self):
        buffer = BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            pd.DataFrame({"值": [1]}).to_excel(writer, sheet_name="第一表", index=False)
            pd.DataFrame({"值": [2, 3]}).to_excel(writer, sheet_name="第二表", index=False)
        sample = UploadedSample(buffer.getvalue())
        sample.name = "测试.xlsx"
        with patch("streamlit.file_uploader", return_value=sample):
            app = AppTest.from_file(APP).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.metric[0].value, "1")
            app.selectbox[0].select("第二表").run()
            self.assertFalse(app.exception)
            self.assertEqual(app.metric[0].value, "2")

    def test_sql_execution_and_error(self):
        with patch("streamlit.file_uploader", return_value=UploadedSample(b"a,b\n1,2\n3,4")):
            app = AppTest.from_file(APP).run()
            app.text_area[0].set_value("SELECT SUM(a) AS total FROM uploaded_data")
            app.button[1].click().run(timeout=15)
            self.assertFalse(app.exception)
            self.assertEqual(app.metric[3].label, "返回行数")
            self.assertEqual(app.dataframe[2].value.iloc[0, 0], 4)
            app.text_area[0].set_value("DROP TABLE uploaded_data")
            app.button[1].click().run(timeout=15)
            self.assertFalse(app.exception)
            self.assertTrue(app.error)
            self.assertEqual(len(app.dataframe), 2)
            self.assertEqual(len(app.metric), 3)

    def test_sql_examples_and_dataset_change(self):
        sample = UploadedSample(b"a,b\n1,2\n3,4")
        with patch("streamlit.file_uploader", return_value=sample):
            app = AppTest.from_file(APP).run()
            app.selectbox[3].select("统计总行数").run()
            app.button[0].click().run()
            self.assertIn("COUNT(*)", app.text_area[0].value)
            app.button[1].click().run(timeout=15)
            self.assertEqual(app.dataframe[2].value.iloc[0, 0], 2)
            sample.content = b"new_field\n10"
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.dataframe), 2)
            self.assertEqual(len(app.metric), 3)
            self.assertIn("SELECT *", app.text_area[0].value)


if __name__ == "__main__":
    unittest.main()
