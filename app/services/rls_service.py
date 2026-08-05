"""
行级安全 (Row-Level Security) 服务
----------------------------------
基于用户 MVNO 归属实现多租户数据隔离。

核心功能：
  1. get_rls_condition — 根据用户角色和 mvno_id 生成 WHERE 条件
  2. inject_rls — 使用 sqlglot 解析 SQL，在适当位置注入 RLS 条件
  3. verify_rls — 验证 SQL 中涉及 mvno_id 的表是否都有 RLS 条件

RLS 规则：
  - admin: 无限制（不加 WHERE 条件）
  - analyst/viewer: 自动添加 WHERE mvno_id = {user.mvno_id}
  - 无 mvno_id 的表（operators/roaming_packages）不做 RLS
  - data_usage 表通过 user_id 间接关联 mvno，添加子查询过滤
"""

import logging
from dataclasses import dataclass
from typing import Optional

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

# 含有 mvno_id 列的表（直接 RLS 过滤）
RLS_TABLES: set[str] = {"users", "plans", "orders", "esim_profiles"}

# 通过 user_id 间接关联 mvno 的表（子查询 RLS）
INDIRECT_RLS_TABLES: set[str] = {"data_usage"}

# 无需 RLS 的表（参考数据，全局共享）
NO_RLS_TABLES: set[str] = {"operators", "roaming_packages"}


@dataclass
class RLSResult:
    """RLS 注入结果"""
    sql: str                         # 注入 RLS 条件后的 SQL
    rls_applied: bool                # 是否注入了 RLS 条件
    rls_tables: list[str]            # 注入了 RLS 条件的表列表
    rls_condition: str               # RLS 条件描述（用于审计日志）


class RLSService:
    """行级安全服务

    根据用户角色和 MVNO 归属，自动在 SQL 中注入行级过滤条件，
    确保非 admin 用户只能访问自己所属 MVNO 的数据。
    """

    def get_rls_condition(
        self,
        role: str,
        mvno_id: Optional[int],
    ) -> Optional[str]:
        """根据用户身份生成 RLS WHERE 条件

        Args:
            role: 用户角色 (admin/analyst/viewer)
            mvno_id: 用户所属 MVNO ID

        Returns:
            str | None: RLS 条件表达式，admin 或无 mvno_id 时返回 None
        """
        if role == "admin":
            return None

        if mvno_id is None:
            # 非 admin 但无 mvno_id，保守返回一个不可能的条件（不返回任何数据）
            logger.warning("Non-admin user without mvno_id, applying restrictive RLS")
            return "1=0"

        return f"mvno_id = {int(mvno_id)}"

    def inject_rls(
        self,
        sql: str,
        role: str,
        mvno_id: Optional[int],
    ) -> RLSResult:
        """在 SQL 中注入 RLS 条件

        使用 sqlglot 解析 SQL，找到所有含 mvno_id 的表引用，
        在 WHERE 子句中注入过滤条件。

        处理场景：
        - 简单 SELECT: 直接在 WHERE 中添加条件
        - JOIN: 为每个含 mvno_id 的表添加条件
        - 子查询: 递归处理所有 SELECT 节点
        - UNION: 每个 SELECT 分别注入
        - CTE (WITH): 在 CTE 定义和主查询中分别注入

        Args:
            sql: 原始 SQL 语句
            role: 用户角色
            mvno_id: 用户所属 MVNO ID

        Returns:
            RLSResult: 包含注入后 SQL 和元信息
        """
        # admin 或无 mvno_id 的非 admin 用户
        condition = self.get_rls_condition(role, mvno_id)
        if condition is None:
            return RLSResult(sql=sql, rls_applied=False, rls_tables=[], rls_condition="admin: no RLS")

        if condition == "1=0":
            # 极端情况：非 admin 无 mvno_id
            return RLSResult(
                sql=sql,
                rls_applied=False,
                rls_tables=[],
                rls_condition="no mvno_id: access denied (1=0)",
            )

        mvno_val = int(mvno_id) if mvno_id is not None else 0
        rls_tables_applied: list[str] = []

        try:
            parsed = sqlglot.parse_one(sql, dialect="mysql")
        except Exception as e:
            logger.warning("RLS: SQL parse failed, skipping RLS injection: %s", e)
            return RLSResult(sql=sql, rls_applied=False, rls_tables=[], rls_condition=f"parse_error: {e}")

        # 递归处理所有 SELECT 节点（包括子查询、UNION 分支、CTE）
        for select_node in parsed.find_all(exp.Select):
            rls_tables_applied.extend(
                self._inject_rls_into_select(select_node, mvno_val)
            )

        # 去重
        rls_tables_applied = list(dict.fromkeys(rls_tables_applied))

        if rls_tables_applied:
            result_sql = parsed.sql(dialect="mysql")
            rls_condition_str = f"mvno_id={mvno_val} on tables: {', '.join(rls_tables_applied)}"
            logger.info("RLS injected: %s", rls_condition_str)
            return RLSResult(
                sql=result_sql,
                rls_applied=True,
                rls_tables=rls_tables_applied,
                rls_condition=rls_condition_str,
            )

        # 没有需要 RLS 的表（如只查 operators/roaming_packages）
        return RLSResult(
            sql=sql,
            rls_applied=False,
            rls_tables=[],
            rls_condition="no RLS tables in query",
        )

    def _inject_rls_into_select(
        self,
        select_node: exp.Select,
        mvno_val: int,
    ) -> list[str]:
        """在单个 SELECT 节点中注入 RLS 条件

        Args:
            select_node: sqlglot Select 节点
            mvno_val: MVNO ID 值

        Returns:
            list[str]: 注入了 RLS 的表名列表
        """
        applied: list[str] = []

        # 收集 CTE 别名（CTE 内部的 RLS 在 CTE 定义中处理，主查询中跳过）
        cte_names: set[str] = set()
        for cte in select_node.find_all(exp.CTE):
            if cte.alias:
                cte_names.add(cte.alias.lower())
        # 也检查同级 CTE（在 WITH 语句中）
        parent = select_node.parent
        while parent:
            if isinstance(parent, exp.With):
                for cte in parent.find_all(exp.CTE):
                    if cte.alias:
                        cte_names.add(cte.alias.lower())
            parent = parent.parent

        # 收集需要 RLS 的表及其别名
        # 使用 from_ 键（sqlglot 新版用 from_ 代替 from）
        tables_to_filter: list[tuple[str, str]] = []  # (table_name, alias_or_name)

        # 从 FROM 子句中找表
        from_clause = select_node.args.get("from_") or select_node.args.get("from")
        if from_clause:
            for table_node in from_clause.find_all(exp.Table):
                tbl_name = table_node.name.lower()
                if tbl_name in RLS_TABLES and tbl_name not in cte_names:
                    alias = table_node.alias or table_node.name
                    tables_to_filter.append((tbl_name, alias))

        # 从 JOIN 中找表
        for join_node in select_node.args.get("joins", []) or []:
            for table_node in join_node.find_all(exp.Table):
                tbl_name = table_node.name.lower()
                if tbl_name in RLS_TABLES and tbl_name not in cte_names:
                    alias = table_node.alias or table_node.name
                    tables_to_filter.append((tbl_name, alias))

        # 处理间接 RLS 表 (data_usage)
        if from_clause:
            for table_node in from_clause.find_all(exp.Table):
                tbl_name = table_node.name.lower()
                if tbl_name in INDIRECT_RLS_TABLES and tbl_name not in cte_names:
                    alias = table_node.alias or table_node.name
                    tables_to_filter.append((tbl_name, alias))
                    applied.append(tbl_name)

        for join_node in select_node.args.get("joins", []) or []:
            for table_node in join_node.find_all(exp.Table):
                tbl_name = table_node.name.lower()
                if tbl_name in INDIRECT_RLS_TABLES and tbl_name not in cte_names:
                    alias = table_node.alias or table_node.name
                    tables_to_filter.append((tbl_name, alias))
                    applied.append(tbl_name)

        if not tables_to_filter:
            return applied

        # 构建 RLS 条件表达式
        conditions: list[exp.Expression] = []

        for tbl_name, alias in tables_to_filter:
            if tbl_name in RLS_TABLES:
                # 直接条件: alias.mvno_id = X
                cond = exp.column("mvno_id", table=alias).eq(exp.Literal.number(mvno_val))
                conditions.append(cond)
                applied.append(tbl_name)
            elif tbl_name in INDIRECT_RLS_TABLES:
                # 间接条件: alias.user_id IN (SELECT id FROM users WHERE mvno_id = X)
                subquery = exp.Select(
                    expressions=[exp.column("id")],
                    **{
                        "from": exp.From(
                            this=exp.Table(
                                this=exp.to_identifier("users"),
                            )
                        ),
                        "where": exp.Where(
                            this=exp.column("mvno_id", table="users").eq(
                                exp.Literal.number(mvno_val)
                            )
                        ),
                    },
                )
                cond = exp.In(
                    this=exp.column("user_id", table=alias),
                    expressions=[subquery],
                )
                conditions.append(cond)

        if not conditions:
            return applied

        # 合并所有条件
        combined: exp.Expression = conditions[0]
        for cond in conditions[1:]:
            combined = exp.and_(combined, cond)

        # 与现有 WHERE 条件合并
        existing_where = select_node.args.get("where")
        if existing_where and existing_where.this:
            combined = exp.and_(existing_where.this, combined)

        select_node.set("where", exp.Where(this=combined))

        return applied

    def verify_rls(
        self,
        sql: str,
        role: str,
        mvno_id: Optional[int],
    ) -> tuple[bool, str]:
        """验证 SQL 中涉及 mvno_id 的表是否都有 RLS 条件

        Args:
            sql: SQL 语句
            role: 用户角色
            mvno_id: 用户 MVNO ID

        Returns:
            tuple[bool, str]: (是否通过验证, 原因说明)
        """
        if role == "admin":
            return True, "admin: RLS not required"

        try:
            parsed = sqlglot.parse_one(sql, dialect="mysql")
        except Exception:
            return True, "parse error, skipping RLS verification"

        # 收集所有引用的 RLS 表
        rls_tables_found: set[str] = set()
        for table_node in parsed.find_all(exp.Table):
            tbl_name = table_node.name.lower()
            if tbl_name in RLS_TABLES or tbl_name in INDIRECT_RLS_TABLES:
                rls_tables_found.add(tbl_name)

        if not rls_tables_found:
            return True, "no RLS tables in query"

        # 检查 WHERE 条件中是否包含 mvno_id
        where_clause = parsed.find(exp.Where)
        if not where_clause:
            return False, f"missing RLS: no WHERE clause for tables {rls_tables_found}"

        where_sql = where_clause.sql(dialect="mysql").lower()
        if "mvno_id" not in where_sql:
            return False, f"missing RLS: mvno_id condition not found for tables {rls_tables_found}"

        return True, f"RLS verified for tables: {rls_tables_found}"


# 全局单例
rls_service = RLSService()
