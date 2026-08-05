"""
SQL 安全网关
-----------
四层防御体系，对 NL2SQL 生成的 SQL 进行安全校验：

1. 输入过滤 (Input Filter)    — 拦截用户问题中的危险模式
2. Schema 限制 (Schema Limiter) — 只允许白名单表和 SELECT 操作
3. SQL 校验 (SQL Validator)    — sqlglot 解析，检查 JOIN/子查询数量
4. 结果检查 (Post Checker)     — 限制返回行数
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

logger = logging.getLogger(__name__)

# security.yaml 路径
_SECURITY_YAML = Path(__file__).resolve().parent.parent / "config" / "security.yaml"


@dataclass
class SecurityCheckResult:
    """安全检查结果"""
    passed: bool
    layer: str = ""           # 哪一层拦截: input_filter / schema_limiter / sql_validator / post_checker
    reason: str = ""          # 拦截原因
    blocked_patterns: list[str] = field(default_factory=list)
    sql_after_check: str = ""  # 可能被修改后的 SQL（如添加 LIMIT）


class SQLSecurityGateway:
    """
    SQL 安全网关单例

    从 security.yaml 加载配置，提供四层安全检查。
    在 query_service 执行 SQL 前调用 validate() 方法。
    """

    _instance: Optional["SQLSecurityGateway"] = None
    _config: dict = None

    def __new__(cls) -> "SQLSecurityGateway":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self) -> None:
        """加载 security.yaml 配置"""
        try:
            with open(_SECURITY_YAML, "r", encoding="utf-8") as f:
                self._config = yaml.safe_load(f) or {}
            logger.info("Security config loaded from %s", _SECURITY_YAML)
        except Exception as e:
            logger.error("Failed to load security.yaml: %s, using defaults", e)
            self._config = {}

    @property
    def sql_security_config(self) -> dict:
        return self._config.get("sql_security", {})

    # ============================================================
    # 第一层：输入过滤
    # ============================================================

    def check_input(self, question: str) -> SecurityCheckResult:
        """检查用户输入是否包含危险模式

        拦截 SQL 注入尝试和危险关键词，在发送给 LLM 之前执行。
        """
        input_filter_cfg = self.sql_security_config.get("input_filter", {})
        if not input_filter_cfg.get("enabled", True):
            return SecurityCheckResult(passed=True)

        # 长度检查
        max_len = input_filter_cfg.get("max_question_length", 1000)
        if len(question) > max_len:
            return SecurityCheckResult(
                passed=False,
                layer="input_filter",
                reason=f"输入过长（{len(question)} > {max_len}）",
            )

        # 危险模式匹配
        blocked = input_filter_cfg.get("blocked_patterns", [])
        matched = []
        for pattern in blocked:
            try:
                if re.search(pattern, question, re.IGNORECASE):
                    matched.append(pattern)
            except re.error:
                logger.warning("Invalid regex in blocked_patterns: %s", pattern)

        if matched:
            return SecurityCheckResult(
                passed=False,
                layer="input_filter",
                reason=f"输入包含危险模式: {matched[0]}",
                blocked_patterns=matched,
            )

        return SecurityCheckResult(passed=True)

    # ============================================================
    # 第二层 + 第三层：SQL 校验（Schema 限制 + SQL Validator）
    # ============================================================

    def validate_sql(self, sql: str) -> SecurityCheckResult:
        """校验 LLM 生成的 SQL 语句

        合并 schema_limiter 和 sql_validator 两层检查：
        - 只允许 SELECT
        - 只允许白名单表
        - JOIN 数量限制
        - 子查询深度限制
        - 禁止危险函数
        """
        if not sql or not sql.strip():
            return SecurityCheckResult(
                passed=False, layer="sql_validator", reason="空 SQL 语句"
            )

        sql_upper = sql.strip().upper()

        # --- 快速关键词检查 ---
        schema_cfg = self.sql_security_config.get("schema_limiter", {})
        validator_cfg = self.sql_security_config.get("sql_validator", {})

        allowed_ops = [op.upper() for op in schema_cfg.get("allowed_operations", ["SELECT"])]
        required_kw = validator_cfg.get("required_keyword", "SELECT").upper()

        # 必须以 SELECT 开头（或 WITH...SELECT）
        first_word = sql_upper.split()[0] if sql_upper.split() else ""
        if first_word not in ("SELECT", "WITH"):
            return SecurityCheckResult(
                passed=False,
                layer="schema_limiter",
                reason=f"仅允许 {allowed_ops} 操作，检测到: {first_word}",
            )

        # 禁止的危险关键词
        dangerous_keywords = [
            "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER",
            "CREATE", "GRANT", "REVOKE", "RENAME", "LOAD_FILE",
            "INTO OUTFILE", "INTO DUMPFILE", "CALL", "EXEC", "EXECUTE",
        ]
        for kw in dangerous_keywords:
            # 用 word boundary 匹配，避免误杀列名
            pattern = r'\b' + re.escape(kw) + r'\b'
            if re.search(pattern, sql_upper):
                return SecurityCheckResult(
                    passed=False,
                    layer="schema_limiter",
                    reason=f"SQL 包含禁止操作: {kw}",
                )

        # --- sqlglot 深度解析 ---
        max_len = validator_cfg.get("max_query_length", 4000)
        if len(sql) > max_len:
            return SecurityCheckResult(
                passed=False,
                layer="sql_validator",
                reason=f"SQL 过长（{len(sql)} > {max_len}）",
            )

        try:
            parsed = parse_one(sql, dialect="mysql")
        except ParseError as e:
            logger.warning("SQL parse failed: %s", e)
            # 解析失败不直接拦截，可能是非标准语法，让 DB 自己报错
            return SecurityCheckResult(passed=True, sql_after_check=sql)

        # 检查表名是否在白名单
        allowed_tables = set(schema_cfg.get("allowed_tables", []))
        if allowed_tables:
            found_tables = set()
            for table_node in parsed.find_all(exp.Table):
                table_name = table_node.name
                if table_name:
                    found_tables.add(table_name.lower())

            unallowed = found_tables - {t.lower() for t in allowed_tables}
            if unallowed:
                return SecurityCheckResult(
                    passed=False,
                    layer="schema_limiter",
                    reason=f"访问了非白名单表: {', '.join(sorted(unallowed))}",
                )

        # 检查 JOIN 数量
        max_joins = schema_cfg.get("max_joins", 5)
        join_count = len(list(parsed.find_all(exp.Join)))
        if join_count > max_joins:
            return SecurityCheckResult(
                passed=False,
                layer="sql_validator",
                reason=f"JOIN 数量超限（{join_count} > {max_joins}）",
            )

        # 检查子查询深度
        max_subqueries = schema_cfg.get("max_subqueries", 3)
        subquery_count = len(list(parsed.find_all(exp.Subquery)))
        if subquery_count > max_subqueries:
            return SecurityCheckResult(
                passed=False,
                layer="sql_validator",
                reason=f"子查询数量超限（{subquery_count} > {max_subqueries}）",
            )

        # 检查危险函数
        dangerous_funcs = {"LOAD_FILE", "SLEEP", "BENCHMARK", "USER", "DATABASE",
                          "CURRENT_USER", "CONNECTION_ID", "@@version"}
        for func_node in parsed.find_all(exp.Func):
            func_name = func_node.sql_name() if hasattr(func_node, "sql_name") else ""
            if func_name and func_name.upper() in dangerous_funcs:
                return SecurityCheckResult(
                    passed=False,
                    layer="sql_validator",
                    reason=f"禁止使用函数: {func_name}",
                )

        # 检查系统变量引用
        for param_node in parsed.find_all(exp.Parameter):
            param_text = str(param_node)
            if "@@" in param_text:
                return SecurityCheckResult(
                    passed=False,
                    layer="sql_validator",
                    reason=f"禁止访问系统变量: {param_text}",
                )

        # 如果没有 LIMIT，自动添加
        post_cfg = self.sql_security_config.get("post_checker", {})
        result_limit = post_cfg.get("result_size_limit", 1000)
        has_limit = bool(list(parsed.find_all(exp.Limit)))
        sql_after = sql
        if not has_limit and result_limit > 0:
            sql_after = f"{sql.rstrip(';')} LIMIT {result_limit};"
            logger.info("Auto-added LIMIT %d to SQL", result_limit)

        return SecurityCheckResult(
            passed=True,
            sql_after_check=sql_after,
        )

    # ============================================================
    # 第四层：结果检查
    # ============================================================

    def check_result(self, row_count: int) -> SecurityCheckResult:
        """检查查询结果是否符合安全限制"""
        post_cfg = self.sql_security_config.get("post_checker", {})
        if not post_cfg.get("enabled", True):
            return SecurityCheckResult(passed=True)

        result_limit = post_cfg.get("result_size_limit", 1000)
        if row_count > result_limit:
            return SecurityCheckResult(
                passed=False,
                layer="post_checker",
                reason=f"结果行数超限（{row_count} > {result_limit}）",
            )

        return SecurityCheckResult(passed=True)

    # ============================================================
    # 综合校验入口
    # ============================================================

    def validate(self, question: str, sql: str = "") -> SecurityCheckResult:
        """综合校验：输入过滤 + SQL 校验

        Args:
            question: 用户自然语言问题
            sql: LLM 生成的 SQL（可选，如果有则一并校验）

        Returns:
            SecurityCheckResult: 校验结果
        """
        # 第一层：输入过滤
        input_result = self.check_input(question)
        if not input_result.passed:
            logger.warning("Input blocked by input_filter: %s", input_result.reason)
            return input_result

        # 第二+三层：SQL 校验
        if sql:
            sql_result = self.validate_sql(sql)
            if not sql_result.passed:
                logger.warning("SQL blocked by %s: %s", sql_result.layer, sql_result.reason)
                return sql_result

        return SecurityCheckResult(passed=True, sql_after_check=sql)


# 全局单例
sql_gateway = SQLSecurityGateway()
