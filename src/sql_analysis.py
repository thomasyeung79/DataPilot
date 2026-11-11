"""只读 SQL 白名单验证与隔离执行；不执行用户提供的 Python。"""

from dataclasses import dataclass
import multiprocessing as mp
import time

import pandas as pd


AST_REASON_CODES = frozenset({"UNSUPPORTED_NODE", "STATEMENT_TYPE", "TABLE_SOURCE",
                              "AST_COMPLEXITY", "MISSING_PROJECTION", "PARSE_ERROR",
                              "SQL_LENGTH_LIMIT", "EMPTY_SQL"})


def safe_ast_metadata(node, reason):
    """只接受解析器内建节点类型名和固定原因代码，不接受节点文本或用户标识符。"""
    from sqlglot import exp
    node_type = vars(exp).get(node) if isinstance(node, str) else None
    safe_node = (node if isinstance(node_type, type) and issubclass(node_type, exp.Expression)
                 and node_type.__name__ == node else None)
    safe_reason = reason if isinstance(reason, str) and reason in AST_REASON_CODES else None
    return safe_node, safe_reason


class SQLAnalysisError(ValueError):
    """可以向界面展示的中文查询错误。"""

    def __init__(self, message, *, category=None, ast_node=None, ast_reason=None):
        super().__init__(message)
        self.category = category
        self.ast_node, self.ast_reason = safe_ast_metadata(ast_node, ast_reason)


@dataclass(frozen=True)
class QueryLimits:
    """仅由应用代码设置，界面不允许用户扩大这些限制。"""

    timeout_seconds: float = 10.0
    max_rows: int = 1000
    duckdb_memory_mb: int = 128
    process_memory_mb: int = 1024
    max_input_bytes: int = 64 * 1024 * 1024
    max_result_bytes: int = 4 * 1024 * 1024
    max_columns: int = 100


@dataclass
class QueryResult:
    data: pd.DataFrame
    truncated: bool


# 默认拒绝未知 AST 节点/函数。添加 SQL 能力必须同时补充安全测试。
ALLOWED_NODES = frozenset({
    "Select", "From", "Table", "Identifier", "Column", "Star", "Alias", "TableAlias",
    "Where", "Group", "Having", "Order", "Ordered", "Limit", "Offset", "Distinct",
    "Literal", "Null", "Boolean", "Paren", "Subquery", "Join",
    "And", "Or", "Not", "EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Is", "In",
    "Between", "Like", "ILike", "Add", "Sub", "Mul", "Div", "Mod", "Neg",
    "Count", "Sum", "Avg", "Min", "Max", "Round", "Abs", "Lower", "Upper",
    "Length", "Coalesce",
})
MAX_SQL_LENGTH = 10_000
MAX_AST_NODES = 500


def validate_sql(sql: str) -> str:
    """使用 DuckDB 方言解析整条语句，返回经过验证并重新生成的 SQL。"""
    import sqlglot
    from sqlglot import exp

    if not isinstance(sql, str) or not sql.strip():
        raise SQLAnalysisError("请输入 SELECT 查询。", ast_reason="EMPTY_SQL")
    if len(sql) > MAX_SQL_LENGTH:
        raise SQLAnalysisError("SQL 长度超过 10000 字符限制。", ast_reason="SQL_LENGTH_LIMIT")
    try:
        # 禁止解析器在不支持语法的警告中回显用户 SQL；不改变解析/白名单规则。
        statements = sqlglot.parse(sql, read="duckdb", error_message_context=0)
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise SQLAnalysisError("仅允许单条只读 SELECT 查询，不允许多语句或修改操作。",
                                   ast_node=type(statements[0]).__name__ if len(statements) == 1 else None,
                                   ast_reason="STATEMENT_TYPE")
        tree = statements[0]
        if any(not select.expressions for select in tree.find_all(exp.Select)):
            raise SQLAnalysisError("SELECT 必须指定查询字段或表达式。", ast_reason="MISSING_PROJECTION")
        for index, node in enumerate(tree.walk()):
            if index >= MAX_AST_NODES:
                raise SQLAnalysisError("SQL 结构过于复杂，请简化查询。", ast_reason="AST_COMPLEXITY")
            if type(node).__name__ not in ALLOWED_NODES:
                raise SQLAnalysisError("查询包含未开放的语法或函数；仅支持基础只读分析。",
                                       ast_node=type(node).__name__, ast_reason="UNSUPPORTED_NODE")
            if isinstance(node, exp.Table):
                if (not isinstance(node.this, exp.Identifier)
                        or node.name.lower() != "uploaded_data"
                        or node.db or node.catalog):
                    raise SQLAnalysisError("只能查询 uploaded_data，禁止其他数据源、文件及表函数。",
                                           category="SQL_TABLE_ERROR", ast_node="Table", ast_reason="TABLE_SOURCE")
        for node in tree.walk():
            node.comments = None
        return tree.sql(dialect="duckdb")
    except SQLAnalysisError as exc:
        if exc.category is None:
            exc.category = "SQL_AST_REJECTED"
        raise
    except Exception as exc:
        raise SQLAnalysisError("SQL 解析失败，请检查 SELECT 语法。", category="SQL_PARSE_ERROR",
                               ast_reason="PARSE_ERROR") from exc


def create_connection(limits: QueryLimits):
    """创建私有内存数据库并锁定安全配置。配置失败则不降级运行。"""
    import duckdb

    connection = duckdb.connect(":memory:", config={
        "enable_external_access": "false",
        "autoload_known_extensions": "false",
        "autoinstall_known_extensions": "false",
        "allow_unsigned_extensions": "false",
        "allow_community_extensions": "false",
        "python_enable_replacements": "false",
        "memory_limit": f"{limits.duckdb_memory_mb}MB",
        "threads": "1",
        "temp_directory": "",
        "max_temp_directory_size": "0B",
    })
    try:
        connection.execute("SET disabled_filesystems = 'LocalFileSystem'")
        connection.execute("SET lock_configuration = true")
    except Exception:
        connection.close()
        raise
    return connection


def _execute(frame: pd.DataFrame, sql: str, limits: QueryLimits) -> QueryResult:
    """仅供受资源限制的工作进程调用。"""
    import duckdb

    canonical_sql = validate_sql(sql)
    with create_connection(limits) as connection:
        # 两个解析器均需确认只有一条 SELECT；执行的是规范化 SQL。
        statements = connection.extract_statements(canonical_sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise SQLAnalysisError("DuckDB 语句类型检查失败，仅允许 SELECT。")
        connection.register("uploaded_data", frame)
        cursor = connection.execute(
            f'SELECT * FROM ({canonical_sql}) AS "_datapilot_result" LIMIT {limits.max_rows + 1}'
        )
        if len(cursor.description) > limits.max_columns:
            raise SQLAnalysisError("结果列数超过限制，请选择更少字段。")
        result = cursor.fetchdf()
        truncated = len(result) > limits.max_rows
        result = result.head(limits.max_rows)
        if result.memory_usage(index=True, deep=True).sum() > limits.max_result_bytes:
            raise SQLAnalysisError("结果数据量超过 4 MB 限制，请减少行数或字段。")
        return QueryResult(result, truncated)


def _worker(sender, frame, sql, limits):
    """资源限制生效后才开始解析和执行；错误不向用户泄露路径或底层堆栈。"""
    try:
        from src.process_limits import limit_process_memory

        memory_handle = limit_process_memory(limits.process_memory_mb)
        # 保留句柄引用直到进程结束。
        _ = memory_handle
        try:
            result = _execute(frame, sql, limits)
            sender.send((True, result))
        except SQLAnalysisError as exc:
            sender.send((False, str(exc)))
        except MemoryError:
            sender.send((False, "查询内存不足，请缩小数据或简化查询。"))
        except Exception as exc:
            import duckdb

            if isinstance(exc, duckdb.OutOfMemoryException):
                message = "查询超出 DuckDB 内存限制，请简化查询。"
            elif isinstance(exc, (duckdb.BinderException, duckdb.CatalogException)):
                message = "查询失败，请检查字段名称、表名和聚合用法；中文或特殊字段名请用双引号包围。"
            else:
                message = "查询执行失败，可能存在类型不匹配、资源不足或不支持的语法。"
            sender.send((False, message))
    except Exception:
        try:
            sender.send((False, "查询进程初始化失败或资源受限，已拒绝执行。"))
        except Exception:
            pass
    finally:
        sender.close()


def run_query(frame: pd.DataFrame, sql: str, limits: QueryLimits | None = None) -> QueryResult:
    """每次创建独立进程；父进程计时并强制回收超时或异常进程。"""
    limits = limits or QueryLimits()
    if not sql or not sql.strip():
        raise SQLAnalysisError("请输入 SELECT 查询。")
    if len(sql) > MAX_SQL_LENGTH:
        raise SQLAnalysisError("SQL 长度超过 10000 字符限制。")
    if frame.memory_usage(index=True, deep=True).sum() > limits.max_input_bytes:
        raise SQLAnalysisError("当前数据内存超过 64 MB，SQL 分析前请缩小数据；预览仍可使用。")
    names = [str(column).casefold() for column in frame.columns]
    if len(names) != len(set(names)):
        raise SQLAnalysisError("字段名存在仅大小写不同的重名，可能导致 SQL 字段混淆；请先在源文件中重命名。预览仍可使用。")
    context = mp.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(sender, frame, sql, limits), daemon=True)
    started = time.monotonic()
    try:
        process.start()
        sender.close()
        remaining = max(0, limits.timeout_seconds - (time.monotonic() - started))
        if not receiver.poll(remaining):
            raise SQLAnalysisError("查询超时，已终止查询进程；请简化查询或减少数据。")
        try:
            success, payload = receiver.recv()
        except (EOFError, OSError) as exc:
            raise SQLAnalysisError("查询进程异常退出，可能超出内存限制；请简化查询。") from exc
        if not success:
            raise SQLAnalysisError(payload)
        return payload
    except SQLAnalysisError:
        raise
    except Exception as exc:
        raise SQLAnalysisError("无法完成本地查询，查询进程已回收，请重试。") from exc
    finally:
        sender.close()
        receiver.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            process.close()


def quote_identifier(name) -> str:
    """转义实际字段名中的双引号，避免示例拼接成其他 SQL。"""
    return '"' + str(name).replace('"', '""') + '"'


def example_queries(frame: pd.DataFrame) -> dict[str, str]:
    """只引用实际字段，数值聚合跳过布尔列和复数列。"""
    examples = {
        "查看前 20 行": "SELECT * FROM uploaded_data LIMIT 20",
        "统计总行数": 'SELECT COUNT(*) AS "总行数" FROM uploaded_data',
    }
    if len(frame.columns):
        field = quote_identifier(frame.columns[0])
        examples["按首个字段分组计数"] = (
            f'SELECT {field}, COUNT(*) AS "记录数" FROM uploaded_data '
            f'GROUP BY {field} ORDER BY COUNT(*) DESC LIMIT 20'
        )
        examples["筛选首个字段非空"] = f"SELECT * FROM uploaded_data WHERE {field} IS NOT NULL LIMIT 20"
    for index, dtype in enumerate(frame.dtypes):
        if pd.api.types.is_numeric_dtype(dtype) and not (
            pd.api.types.is_bool_dtype(dtype) or pd.api.types.is_complex_dtype(dtype)
        ):
            field = quote_identifier(frame.columns[index])
            examples["数值字段求和与平均"] = (
                f'SELECT SUM({field}) AS "合计", AVG({field}) AS "平均值" FROM uploaded_data'
            )
            break
    return examples
