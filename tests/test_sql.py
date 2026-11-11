"""SQL 白名单、隔离执行、资源限制与错误恢复测试。"""

from dataclasses import replace
import multiprocessing as mp
import os
import time
import unittest
from unittest.mock import patch

import pandas as pd

from src.sql_analysis import (
    QueryLimits, SQLAnalysisError, create_connection, example_queries, run_query, validate_sql,
)


def _memory_probe(sender):
    """用真实分配验证操作系统限制，不把大对象传回主测试进程。"""
    from src.process_limits import limit_process_memory

    handle = limit_process_memory(1024)
    try:
        allocation = bytearray(2 * 1024 * 1024 * 1024)
        sender.send(False)
        del allocation
    except MemoryError:
        sender.send(True)
    finally:
        _ = handle
        sender.close()


def _crash_worker(sender, frame, sql, limits):
    """模拟原生引擎崩溃而非可捕获的 SQL 异常。"""
    os._exit(1)


class SQLValidationTests(unittest.TestCase):
    def test_safe_sql(self):
        for sql in [
            "-- 前置注释\n SELECT COUNT(*) FROM uploaded_data;",
            "SELECT 'DROP TABLE x; SELECT 1' AS text FROM uploaded_data LIMIT 1",
            'SELECT "金额" FROM "uploaded_data" WHERE "金额" > 0 ORDER BY "金额" DESC',
            "SELECT COUNT(*) FROM (SELECT * FROM uploaded_data WHERE a IS NOT NULL) t",
            # SQLGlot 将其规范化为双引号标识符，执行时不会触发 DuckDB 隐式文件扫描。
            "SELECT * FROM 'uploaded_data'",
        ]:
            with self.subTest(sql=sql):
                self.assertTrue(validate_sql(sql))

    def test_reject_unsafe_sql(self):
        queries = [
            "", "-- 注释", "SELECT", "SELECT * FROM", "SELECT 1; SELECT 2",
            "INSERT INTO uploaded_data VALUES (1)", "UPDATE uploaded_data SET a=1",
            "DELETE FROM uploaded_data", "DROP TABLE uploaded_data", "ALTER TABLE uploaded_data ADD a INT",
            "CREATE TABLE x AS SELECT 1", "COPY uploaded_data TO 'leak.csv'",
            "ATTACH 'secret.db' AS x", "DETACH x", "INSTALL httpfs", "LOAD httpfs",
            "PRAGMA version", "SET enable_external_access=true", "CALL checkpoint()",
            "EXPLAIN SELECT * FROM uploaded_data", "SELECT * INTO x FROM uploaded_data",
            "SELECT * FROM read_csv('secret.csv')", "SELECT * FROM read_parquet('https://example.com/a')",
            "SELECT * FROM 'secret.csv'",
            "SELECT * FROM main.uploaded_data", "SELECT * FROM information_schema.tables",
            "SELECT * FROM duckdb_settings()", "SELECT * FROM glob('*')",
            "SELECT read_blob('secret.txt')", "SELECT getenv('PATH')",
            "SELECT query('SELECT * FROM read_csv(''secret.csv'')')",
            "SELECT * FROM query_table('secret.csv')", "SELECT * FROM range(1000000000)",
            "WITH x AS (DELETE FROM uploaded_data RETURNING *) SELECT * FROM x",
            "WITH RECURSIVE x AS (SELECT 1) SELECT * FROM x",
            "SELECT * FROM uploaded_data UNION SELECT * FROM read_csv('secret.csv')",
            "SELECT (SELECT content FROM read_text('secret.txt')) FROM uploaded_data",
            "SELECT * FROM uploaded_data; DROP TABLE uploaded_data",
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                with self.assertRaises(SQLAnalysisError):
                    validate_sql(sql)

    def test_complexity_limits(self):
        with self.assertRaises(SQLAnalysisError):
            validate_sql("SELECT " + "1," * 600 + "1")
        with self.assertRaises(SQLAnalysisError):
            validate_sql("SELECT '" + "x" * 10000 + "'")


class SQLExecutionTests(unittest.TestCase):
    def setUp(self):
        self.frame = pd.DataFrame({"分类": ["甲", "甲", "乙"], "金额": [10.0, 20.0, 30.0]})

    def test_aggregate_filter_group_order(self):
        result = run_query(self.frame,
            'SELECT "分类", COUNT(*) AS n, SUM("金额") AS s, AVG("金额") AS a '
            'FROM uploaded_data WHERE "金额" >= 20 GROUP BY "分类" ORDER BY s DESC')
        self.assertEqual(result.data.values.tolist(), [["乙", 1, 30.0, 30.0], ["甲", 1, 20.0, 20.0]])
        self.assertFalse(result.truncated)

    def test_examples_match_actual_columns(self):
        frame = self.frame.rename(columns={"分类": '类"别; DROP TABLE x --'})
        for sql in example_queries(frame).values():
            with self.subTest(sql=sql):
                run_query(frame, sql)

    def test_row_limit_and_empty_result(self):
        frame = pd.DataFrame({"a": range(1010)})
        result = run_query(frame, "SELECT * FROM uploaded_data ORDER BY a")
        self.assertEqual(len(result.data), 1000)
        self.assertTrue(result.truncated)
        result = run_query(frame, "SELECT * FROM uploaded_data WHERE a < 0")
        self.assertTrue(result.data.empty)
        result = run_query(frame, "SELECT * FROM uploaded_data LIMIT 1000")
        self.assertFalse(result.truncated)

    def test_errors_and_recovery(self):
        original = self.frame.copy(deep=True)
        for sql in ["DROP TABLE uploaded_data", "SELECT nonexistent FROM uploaded_data", "SELECT * FROM read_csv('x')"]:
            with self.assertRaises(SQLAnalysisError):
                run_query(self.frame, sql)
        pd.testing.assert_frame_equal(self.frame, original)
        self.assertEqual(run_query(self.frame, "SELECT COUNT(*) AS n FROM uploaded_data").data.iloc[0, 0], 3)

    def test_ambiguous_column_names(self):
        with self.assertRaisesRegex(SQLAnalysisError, "大小写"):
            run_query(pd.DataFrame({"A": [1], "a": [2]}), "SELECT a FROM uploaded_data")

    def test_input_result_column_limits(self):
        for limits, sql in [
            (replace(QueryLimits(), max_input_bytes=1), "SELECT * FROM uploaded_data"),
            (replace(QueryLimits(), max_result_bytes=1), "SELECT * FROM uploaded_data"),
            (replace(QueryLimits(), max_columns=1), "SELECT * FROM uploaded_data"),
        ]:
            with self.subTest(limits=limits):
                with self.assertRaises(SQLAnalysisError):
                    run_query(self.frame, sql, limits)

    def test_timeout_reclaims_process(self):
        before = {p.pid for p in mp.active_children()}
        started = time.monotonic()
        with self.assertRaisesRegex(SQLAnalysisError, "超时"):
            run_query(self.frame, "SELECT COUNT(*) FROM uploaded_data", replace(QueryLimits(), timeout_seconds=0.001))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual({p.pid for p in mp.active_children()}, before)

    def test_worker_crash_is_handled(self):
        before = {p.pid for p in mp.active_children()}
        with patch("src.sql_analysis._worker", _crash_worker):
            with self.assertRaisesRegex(SQLAnalysisError, "异常退出"):
                run_query(self.frame, "SELECT COUNT(*) FROM uploaded_data")
        self.assertEqual({p.pid for p in mp.active_children()}, before)

    def test_real_expensive_query_times_out(self):
        frame = pd.DataFrame({"a": range(50000)})
        sql = ("SELECT SUM(a.a * b.a + c.a) FROM uploaded_data a CROSS JOIN uploaded_data b "
               "CROSS JOIN uploaded_data c WHERE (a.a + b.a + c.a) % 17 = 0")
        with self.assertRaisesRegex(SQLAnalysisError, "超时|内存"):
            run_query(frame, sql, replace(QueryLimits(), timeout_seconds=2))

    def test_duckdb_memory_limit(self):
        frame = pd.DataFrame({"a": range(100000)})
        with self.assertRaises(SQLAnalysisError):
            run_query(frame, "SELECT a, COUNT(*) FROM uploaded_data GROUP BY a",
                      replace(QueryLimits(), duckdb_memory_mb=1))


class DuckDBSecurityTests(unittest.TestCase):
    def test_os_process_memory_limit(self):
        context = mp.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_memory_probe, args=(sender,))
        try:
            process.start()
            sender.close()
            self.assertTrue(receiver.poll(10), "内存限制测试未及时返回")
            self.assertTrue(receiver.recv(), "操作系统未阻止超限分配")
        finally:
            receiver.close()
            if process.is_alive():
                process.terminate()
            process.join(2)
            process.close()

    def test_security_config_and_direct_file_access(self):
        import duckdb

        with create_connection(QueryLimits()) as connection:
            for key in ["enable_external_access", "autoload_known_extensions", "autoinstall_known_extensions", "python_enable_replacements"]:
                self.assertFalse(connection.execute(f"SELECT current_setting('{key}')").fetchone()[0])
            self.assertTrue(connection.execute("SELECT current_setting('lock_configuration')").fetchone()[0])
            # 绕过 AST 验证也应在实际打开本地文件/网络前被 DuckDB 拒绝。
            for sql in [
                "SELECT * FROM read_csv('C:/Windows/win.ini')",
                "SELECT * FROM read_csv('https://example.com/data.csv')",
                "COPY (SELECT 1) TO 'datapilot_should_not_exist.csv'",
                "ATTACH 'datapilot_should_not_exist.db' AS external_db",
                "LOAD httpfs",
            ]:
                with self.subTest(sql=sql):
                    with self.assertRaises(duckdb.PermissionException):
                        connection.execute(sql)
            for sql in ["SET enable_external_access=true", "SET memory_limit='1TB'"]:
                with self.subTest(sql=sql):
                    with self.assertRaises(duckdb.InvalidInputException):
                        connection.execute(sql)


if __name__ == "__main__":
    unittest.main()
