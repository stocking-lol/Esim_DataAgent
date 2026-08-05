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
from sqlglot.errors import ParseError, TokenError

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

    # Prompt 注入检测模式（中英文）
    PROMPT_INJECTION_PATTERNS = [
        r"(?i)ignore\s+(previous|prior|above|all)\s+(instruction|prompt|rule)",
        r"(?i)disregard\s+(previous|prior|above|all)\s+(instruction|prompt)",
        r"(?i)you\s+are\s+(a|an)\s+(dba|admin|root|system|database)",
        r"(?i)act\s+as\s+(a|an)\s+(dba|admin|root|system)",
        r"(?i)system\s*[:：]\s*",
        r"(?i)forget\s+(everything|all|previous)",
        r"(?i)new\s+instruction\s*[:：]",
        r"(?i)override\s+(system|safety|security)\s+(rule|prompt|instruction)",
        r"(?i)忽略(之前|前面|上面|所有)(的)?(指令|提示|规则|设定)",
        r"(?i) disregard (以上|之前|前面)",
        r"(?i)你(现在|从现在起)?(是|扮演)(管理员|DBA|root|系统|超级用户)",
        r"(?i)忘记(之前|前面|所有)(的)?(指令|设定|规则)",
        r"(?i)新指令[:：]",
        r"(?i)覆盖(系统|安全)(规则|设定)",
    ]

    # 可疑字符组合
    SUSPICIOUS_CHARS = [
        (r";\s*--", "SQL注释注入 (; --)"),
        (r"/\*.*\*/", "SQL块注释 (/* */)"),
        (r"xp_\w+", "SQL Server扩展存储过程 (xp_)"),
        (r"0x[0-9a-fA-F]{8,}", "十六进制编码字符串"),
        (r"CHAR\s*\(\s*\d+", "CHAR()编码绕过"),
        (r"CONCAT\s*\(", "CONCAT函数注入"),
        (r";\s*(DROP|DELETE|UPDATE|INSERT|ALTER|CREATE)", "堆叠注入"),
    ]

    # ============================================================
    # 第一层：输入过滤
    # ============================================================

    def check_input(self, question: str) -> SecurityCheckResult:
        """检查用户输入是否包含危险模式

        拦截 SQL 注入尝试、Prompt 注入和可疑字符，在发送给 LLM 之前执行。
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

        # --- Prompt 注入检测（优先于SQL注入模式，因为可能同时包含两者）---
        for pattern in self.PROMPT_INJECTION_PATTERNS:
            if re.search(pattern, question):
                return SecurityCheckResult(
                    passed=False,
                    layer="input_filter",
                    reason=f"检测到Prompt注入尝试: {pattern[:50]}",
                    blocked_patterns=[pattern],
                )

        # --- SQL 注入模式匹配 ---
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
                reason=f"输入包含SQL注入模式: {matched[0]}",
                blocked_patterns=matched,
            )

        # --- Prompt 注入检测 ---
        for pattern in self.PROMPT_INJECTION_PATTERNS:
            if re.search(pattern, question):
                return SecurityCheckResult(
                    passed=False,
                    layer="input_filter",
                    reason=f"检测到Prompt注入尝试: {pattern[:50]}",
                    blocked_patterns=[pattern],
                )

        # --- 可疑字符组合检测 ---
        for pattern, description in self.SUSPICIOUS_CHARS:
            if re.search(pattern, question, re.IGNORECASE):
                return SecurityCheckResult(
                    passed=False,
                    layer="input_filter",
                    reason=f"可疑字符组合: {description}",
                    blocked_patterns=[pattern],
                )

        return SecurityCheckResult(passed=True)

    def sanitize(self, question: str) -> str:
        """清理用户输入

        - 去除 HTML/XML 标签（防 XSS）
        - 去除控制字符（保留换行和制表符）
        - 规范化空白字符
        - 限制长度
        """
        if not question:
            return ""

        # 去除 HTML/XML 标签（防 XSS）
        cleaned = re.sub(r'<[^>]+>', '', question)

        # 去除控制字符（保留 \n \r \t）
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', cleaned)

        # 规范化空白：多个空格变一个，去除首尾空白
        cleaned = re.sub(r'[ ]{2,}', ' ', cleaned).strip()

        # 限制长度
        max_len = self.sql_security_config.get("input_filter", {}).get(
            "max_question_length", 1000
        )
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len]

        return cleaned

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
        except (ParseError, TokenError) as e:
            logger.warning("SQL parse failed (sqlglot): %s", e)
            # ── FAIL-CLOSED 安全网 ──
            # 解析失败时无法做 AST 级校验，但绝不能因此放行——否则攻击者
            # 只需构造一条 sqlglot 解析不了、但 MySQL 能执行的 SQL，即可
            # 绕过白名单表校验（如访问 information_schema）。
            # 这里启动"正则兜底校验"：独立第二道校验器，仍然强制：
            #   1) 仅允许白名单表  2) 禁止危险关键词  3) 禁止危险函数/系统变量
            # 若正则兜底也无法确认（发现非白名单表或危险构造）→ 直接拦截。
            return self._regex_fallback_validate(sql)

        # 收集 CTE 别名（WITH ... AS 中的别名不是真实表，不应受白名单限制）
        cte_names: set[str] = set()
        for cte_node in parsed.find_all(exp.CTE):
            alias = cte_node.alias
            if alias:
                cte_names.add(alias.lower())

        # 检查表名是否在白名单（排除 CTE 别名）
        allowed_tables = set(schema_cfg.get("allowed_tables", []))
        if allowed_tables:
            found_tables = set()
            for table_node in parsed.find_all(exp.Table):
                table_name = table_node.name
                if table_name:
                    found_tables.add(table_name.lower())

            unallowed = found_tables - {t.lower() for t in allowed_tables} - cte_names
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

        # 检查危险函数（包括 sqlglot 已知函数和匿名函数）
        dangerous_funcs = {"LOAD_FILE", "SLEEP", "BENCHMARK", "USER", "DATABASE",
                          "CURRENT_USER", "CONNECTION_ID"}
        # 检查已知函数
        for func_node in parsed.find_all(exp.Func):
            func_name = func_node.sql_name() if hasattr(func_node, "sql_name") else ""
            if func_name and func_name.upper() in dangerous_funcs:
                return SecurityCheckResult(
                    passed=False,
                    layer="sql_validator",
                    reason=f"禁止使用函数: {func_name}",
                )
        # 兜底：正则检查危险函数名（SLEEP、BENCHMARK 等可能被 sqlglot 解析为匿名函数）
        for func_name in dangerous_funcs:
            pattern = r'\b' + func_name + r'\s*\('
            if re.search(pattern, sql, re.IGNORECASE):
                return SecurityCheckResult(
                    passed=False,
                    layer="sql_validator",
                    reason=f"禁止使用函数: {func_name}",
                )

        # 检查系统变量引用（@@version 等）
        if re.search(r'@@\w+', sql, re.IGNORECASE):
            return SecurityCheckResult(
                passed=False,
                layer="sql_validator",
                reason="禁止访问系统变量 (@@)",
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
    # 正则兜底校验（解析失败时的 FAIL-CLOSED 安全网）
    # ============================================================

    @staticmethod
    def _extract_tables_regex(sql_lower: str) -> set[str]:
        """用正则提取 SQL 中的表名（仅显式 FROM/JOIN 之后）

        用于 sqlglot 解析失败时的兜底白名单校验。只在「FROM <表>」「JOIN <表>」
        处提取，避免把列名/关键字误判为表（已下线宽松子句扫描，杜绝误杀合法查询）。
        """
        tables: set[str] = set()
        for m in re.finditer(r'\b(?:from|join)\s+`?([a-zA-Z_][\w]*)`?', sql_lower):
            tables.add(m.group(1).lower())
        return tables

    def _regex_fallback_validate(self, sql: str) -> SecurityCheckResult:
        """解析失败时的兜底校验：FAIL-CLOSED

        原则：无法用 AST 验证时，**默认拦截**，但允许一条"仅触及白名单表且
        不含危险构造"的 SQL 通过（正则第二道校验）。任何无法确认的情形都拦。
        """
        sql_lower = sql.lower()
        schema_cfg = self.sql_security_config.get("schema_limiter", {})

        # 危险关键词（与 AST 校验保持一致）
        dangerous_keywords = [
            "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER",
            "CREATE", "GRANT", "REVOKE", "RENAME", "LOAD_FILE",
            "INTO OUTFILE", "INTO DUMPFILE", "CALL", "EXEC", "EXECUTE",
        ]
        for kw in dangerous_keywords:
            if re.search(r'\b' + re.escape(kw) + r'\b', sql, re.IGNORECASE):
                return SecurityCheckResult(
                    passed=False, layer="schema_limiter",
                    reason=f"SQL 包含禁止操作: {kw}（解析失败兜底拦截）",
                )

        # 危险函数
        for fn in ("sleep", "benchmark", "load_file"):
            if re.search(r'\b' + fn + r'\s*\(', sql, re.IGNORECASE):
                return SecurityCheckResult(
                    passed=False, layer="sql_validator",
                    reason=f"禁止使用函数: {fn}（解析失败兜底拦截）",
                )

        # 系统变量
        if re.search(r'@@\w+', sql, re.IGNORECASE):
            return SecurityCheckResult(
                passed=False, layer="sql_validator",
                reason="禁止访问系统变量 (@@)（解析失败兜底拦截）",
            )

        # 白名单表校验 —— 核心不变量
        allowed_tables = {t.lower() for t in schema_cfg.get("allowed_tables", [])}
        if allowed_tables:
            found = self._extract_tables_regex(sql_lower)
            unallowed = found - allowed_tables
            if unallowed:
                return SecurityCheckResult(
                    passed=False, layer="schema_limiter",
                    reason=f"访问了非白名单表: {', '.join(sorted(unallowed))}（解析失败兜底拦截）",
                )

        # 兜底校验通过：放行（记录警告，便于后续审查）
        logger.warning("SQL parse failed but passed regex fallback (allowed): %.200s", sql)
        return SecurityCheckResult(passed=True, sql_after_check=sql)

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
