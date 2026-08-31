"""
Mini Agent Runtime - 工具层
==========================
参考 Vanna 的 ToolRegistry / RunSqlTool 抽象，从零实现：

  - ToolSpec  : 工具规格（名称 / 描述 / 异步回调 / 访问组）
  - ToolRegistry : 工具注册与查找中心
  - SqlTool   : SQL 执行工具

SqlTool 的安全设计（本项目自研的差异化能力）：
  1. 执行前强制过 `sql_gateway.validate_sql()`（fail-closed：解析失败默认拦截）；
  2. 非 admin 注入 RLS 行级条件（复用 rls_service）；
  3. 添加 MAX_EXECUTION_TIME 超时提示 + 行数上限；
  4. dry_run 模式：用 sqlglot 模拟执行（语法/表/列校验），不连数据库——
     这是自研底座能离线跑 54 题评估的关键。
"""

import asyncio
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import sqlglot
from sqlglot import exp

from app.config.settings import settings
from app.core.sql_security import sql_gateway
from app.services.rls_service import rls_service

logger = logging.getLogger(__name__)

# 合法表集合（与评估口径一致）
VALID_TABLES = {
    "users", "plans", "orders", "esim_profiles",
    "data_usage", "operators", "roaming_packages",
}

# dry_run 校验用：表 → 合法列集合（从 init_db.sql 精简提取）
_SCHEMA_COLUMNS: dict[str, set[str]] = {
    "operators": {"id", "name", "type", "mcc_mnc", "country", "status",
                  "contact_info", "created_at", "updated_at"},
    "users": {"id", "phone_number", "email", "iccid", "imsi", "mvno_id",
              "status", "region", "created_at", "updated_at"},
    "plans": {"id", "name", "data_volume_mb", "voice_minutes", "sms_count",
              "price", "currency", "validity_days", "type", "mvno_id",
              "status", "description", "created_at", "updated_at"},
    "orders": {"id", "user_id", "plan_id", "order_no", "status", "amount",
               "currency", "payment_method", "mvno_id", "created_at",
               "activated_at", "cancelled_at", "updated_at"},
    "esim_profiles": {"id", "user_id", "iccid", "imsi", "profile_status",
                      "activation_code", "mno_id", "mvno_id", "created_at",
                      "activated_at", "updated_at"},
    "data_usage": {"id", "user_id", "iccid", "used_mb", "country_code",
                   "roaming_flag", "started_at", "ended_at", "created_at"},
    "roaming_packages": {"id", "name", "coverage_countries", "data_volume_mb",
                         "price", "currency", "validity_days", "mvno_id",
                         "status", "created_at", "updated_at"},
}


@dataclass
class ToolSpec:
    """工具规格"""
    name: str
    description: str
    func: Callable[..., Awaitable["ToolResult"]]
    access_groups: list[str] = field(default_factory=lambda: ["admin"])


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    sql: str = ""
    data: Optional[list[dict]] = None
    columns: Optional[list[str]] = None
    error: str = ""
    blocked: bool = False
    block_reason: str = ""
    retryable: bool = True        # 是否可触发 Agent 自愈重试


class ToolRegistry:
    """工具注册中心（自研实现）

    设计要点：
      - 注册时校验工具名唯一，避免覆盖；
      - 按 access_groups 做权限分组（对应 Vanna 的 access_groups 概念）；
      - 查询未知工具返回 None，由调用方决定降级策略。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool '{spec.name}' already registered")
        self._tools[spec.name] = spec
        logger.debug("Tool registered: %s (groups=%s)", spec.name, spec.access_groups)

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def list(self) -> list[str]:
        return sorted(self._tools.keys())

    def has(self, name: str) -> bool:
        return name in self._tools

    def can_access(self, name: str, group: str) -> bool:
        spec = self._tools.get(name)
        if spec is None:
            return False
        # admin 组拥有最高权限，可访问任意已注册工具
        if group == "admin":
            return True
        return group in spec.access_groups


class SqlTool:
    """SQL 执行工具（自研，内置安全钩子）

    Args:
        dry_run: True 时不连数据库，用 sqlglot 模拟执行（语法/表/列校验），
                 用于离线评估与单元测试。
    """

    def __init__(self, dry_run: bool = False) -> None:
        self._dry_run = dry_run
        self._conn = None  # 懒加载 pymysql 连接（兼容字段）
        self._local = threading.local()  # 坑⑭：每线程独立连接，配合 to_thread

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    # --- 对外主入口 ---

    async def execute(
        self,
        sql: str,
        role: str = "admin",
        mvno_id: Optional[int] = None,
    ) -> ToolResult:
        """执行 SQL（带安全网关 + RLS + 超时提示）"""
        if not sql or not sql.strip():
            return ToolResult(success=False, error="empty SQL", retryable=False)

        # 1. 安全网关校验（fail-closed）
        try:
            check = sql_gateway.validate_sql(sql)
            if not check.passed:
                logger.warning("SQL BLOCKED by %s: %s", check.layer, check.reason)
                return ToolResult(
                    success=False,
                    sql=sql,
                    blocked=True,
                    block_reason=check.reason,
                    error=check.reason,
                    retryable=False,   # 安全拦截不可重试
                )
            sql = check.sql_after_check or sql
        except Exception as e:          # 安全子系统异常 → fail-closed
            logger.error("Security check failed (fail-closed, blocking): %s", e)
            return ToolResult(
                success=False,
                sql=sql,
                blocked=True,
                block_reason=f"安全校验子系统异常: {e}",
                error=str(e),
                retryable=False,
            )

        # 2. RLS 行级注入（非 admin）
        try:
            rls = rls_service.inject_rls(sql, role=role, mvno_id=mvno_id)
            if rls.rls_condition == "no mvno_id: access denied (1=0)":
                # 坑③ 修复：非 admin 无 mvno_id 必须拒绝，不得放行
                return ToolResult(
                    success=False,
                    sql=sql,
                    blocked=True,
                    block_reason="当前用户无租户归属（mvno_id），已拒绝访问",
                    error="当前用户无租户归属（mvno_id），已拒绝访问",
                    retryable=False,
                )
            # 坑⑦：RLS 注入后二次校验（verify_rls），失败即拦截
            try:
                ok, reason = rls_service.verify_rls(sql=rls.sql, role=role, mvno_id=mvno_id)
            except Exception as e:
                ok, reason = False, f"RLS 校验异常: {e}"
            if not ok:
                return ToolResult(
                    success=False,
                    sql=sql,
                    blocked=True,
                    block_reason=f"RLS 校验失败: {reason}",
                    error=f"RLS 校验失败: {reason}",
                    retryable=False,
                )
            sql = rls.sql
        except Exception as e:
            logger.warning("RLS injection failed (proceed without RLS): %s", e)

        # 3. 执行
        if self._dry_run:
            return self._dry_run_execute(sql)
        return await self._real_execute(sql)

    # --- dry_run：模拟执行（sqlglot 校验） ---

    def _dry_run_execute(self, sql: str) -> ToolResult:
        """不连数据库：语法 + 表 + 列 三级校验，模拟真实执行错误"""
        # 语法校验（模拟 errno 1064）
        try:
            parsed = sqlglot.parse_one(sql, dialect="mysql")
        except Exception as e:
            return ToolResult(
                success=False,
                sql=sql,
                error=f"(pymysql.err.ProgrammingError) (1064, \"You have an error in your "
                      f"SQL syntax: {e}\")",
                retryable=True,
            )
        if parsed is None:
            return ToolResult(
                success=False, sql=sql, error="(1064, 'SQL syntax error')", retryable=True)

        # 表校验（模拟 errno 1146）
        tables = {t.name.lower() for t in parsed.find_all(exp.Table)}
        bad_tables = tables - VALID_TABLES
        if bad_tables:
            return ToolResult(
                success=False,
                sql=sql,
                error=f"(pymysql.err.ProgrammingError) (1146, \"Table '"
                      f"{sorted(bad_tables)[0]}' doesn't exist\")",
                retryable=True,
            )

        # 列校验（模拟 errno 1054）
        # 收集 FROM/JOIN 涉及的表：单表查询时可用它校验所有裸列；
        # 多表 JOIN 时仅校验显式限定（table.column）的列。
        from_tables = {t.name.lower() for t in parsed.find_all(exp.Table)}
        single_table = from_tables.pop() if len(from_tables) == 1 else None
        for column in parsed.find_all(exp.Column):
            col_name = column.name.lower()
            table = (column.table or "").lower()
            if col_name in {"id", "created_at", "updated_at", "mvno_id"}:
                continue  # 通用列跳过（可能存在于任何表）
            if table and table in _SCHEMA_COLUMNS:
                if col_name not in _SCHEMA_COLUMNS[table]:
                    return self._col_error(sql, column.name, table)
            elif not table and single_table and single_table in _SCHEMA_COLUMNS:
                if col_name not in _SCHEMA_COLUMNS[single_table]:
                    return self._col_error(sql, column.name, single_table)

        return ToolResult(success=True, sql=sql, data=[], columns=[])

    @staticmethod
    def _col_error(sql: str, column: str, table: str) -> ToolResult:
        return ToolResult(
            success=False,
            sql=sql,
            error=f"(pymysql.err.ProgrammingError) (1054, \"Unknown column "
                  f"'{column}' in '{table}'\")",
            retryable=True,
        )

    # --- 真实执行（pymysql） ---

    async def _real_execute(self, sql: str) -> ToolResult:
        """连接 MySQL 真实执行（含超时提示 + 行数上限）

        坑⑭：同步 DB 调用丢进线程池执行，避免阻塞事件循环。
        """
        sql = _add_timeout_hint(sql)
        return await asyncio.to_thread(self._real_execute_sync, sql)

    def _real_execute_sync(self, sql: str) -> ToolResult:
        """线程内执行的同步 DB 逻辑（每线程独立连接，避免跨线程共享）"""
        import pymysql

        try:
            conn = self._get_conn()
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql)
                rows = cur.fetchmany(settings.MAX_QUERY_ROWS)
                columns = [d[0] for d in cur.description] if cur.description else []
            return ToolResult(
                success=True,
                sql=sql,
                data=rows,
                columns=columns,
                retryable=False,
            )
        except Exception as e:
            logger.warning("SQL execution error: %s", str(e)[:200])
            return ToolResult(success=False, sql=sql, error=str(e), retryable=True)

    def _get_conn(self):
        import pymysql
        conn = getattr(self._local, "conn", None)
        if conn is None or not conn.open:
            conn = pymysql.connect(
                host=settings.DATABASE_HOST,
                port=settings.DATABASE_PORT,
                user=settings.DATABASE_USER,
                password=settings.DATABASE_PASSWORD,
                database=settings.DATABASE_NAME,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=10,
            )
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None and conn.open:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
        if self._conn is not None and self._conn.open:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def _add_timeout_hint(sql: str) -> str:
    """为 SELECT 语句添加 MySQL 超时提示（复用 query_service 同款逻辑）"""
    s = sql.strip()
    if not s.lower().startswith("select"):
        return s
    hint = f"/*+ MAX_EXECUTION_TIME({settings.QUERY_TIMEOUT_SECONDS * 1000}) */"
    m = re.match(r"(?is)^(select\s+)(.*)$", s)
    if not m:
        return s
    return f"{m.group(1)}{hint} {m.group(2)}"
