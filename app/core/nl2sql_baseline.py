"""
非 Vanna 的 NL2SQL 基线引擎（Rule-based Baseline）
================================================

目的：
    本项目核心 NL2SQL 由 Vanna 2.0（LLM Agent）生成。为回答"不用 Vanna
    你怎么从零实现"，并量化 Vanna 相对朴素方案的优越性，这里提供一个**纯规则/
    模板驱动的 NL2SQL 基线**：完全不依赖 LLM、不依赖 Vanna，仅靠关键词匹配与
    模板拼装生成 SQL。

设计取舍（面试要点）：
    - 优点：确定性、零推理成本、零 API 调用、可解释、可单测。
    - 缺点：**脆弱**——只能处理"模板里写死"的句式；换个说法、来个歧义问句、
      涉及未覆盖的表/列就直接失败或生成错误 SQL。这正是 LLM 方案的差距所在。

覆盖范围（7 类，与 eval 测试集对齐）：
    single_table / join / aggregation / time_series / ranking / group_by / comparison
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# 基线条目：硬编码的 eSIM 业务 schema 知识（"从零实现"需要人工整理）
# ============================================================

@dataclass
class TableInfo:
    name: str
    keywords: list[str]
    count_dimension: str = "id"          # 计数维度（COUNT(x)）
    default_select: str = "*"            # 默认查询列
    groupable: list[str] = field(default_factory=list)  # 可分组维度
    time_column: Optional[str] = None    # 时间过滤列


# 仅覆盖已知表；任何超出此范围的问句基线都无法处理（体现脆弱性）
BASELINE_TABLES: dict[str, TableInfo] = {
    "users": TableInfo(
        name="users",
        keywords=["用户", "客户", "注册", "user", "customer", " subscriber"],
        count_dimension="id",
        groupable=["region", "status"],
        time_column="created_at",
    ),
    "plans": TableInfo(
        name="plans",
        keywords=["套餐", "计划", "plan", "资费", "产品"],
        count_dimension="id",
        groupable=["type", "status"],
    ),
    "orders": TableInfo(
        name="orders",
        keywords=["订单", "订购", "购买", "order", "purchase"],
        count_dimension="id",
        groupable=["status", "plan_id"],
        time_column="created_at",
    ),
    "operators": TableInfo(
        name="operators",
        keywords=["运营商", "mvno", "operator", "虚拟"],
        count_dimension="id",
    ),
    "roaming_packages": TableInfo(
        name="roaming_packages",
        keywords=["漫游包", "roaming package", "国际漫游产品"],
        count_dimension="id",
    ),
    "data_usage": TableInfo(
        name="data_usage",
        keywords=["流量", "用量", "data usage", "usage"],
        count_dimension="id",
        groupable=["country_code", "roaming_flag"],
        time_column="created_at",
    ),
    "esim_profiles": TableInfo(
        name="esim_profiles",
        keywords=["esim 档案", "profile", "档案", "开通"],
        count_dimension="id",
    ),
}


# ============================================================
# 意图关键词
# ============================================================

INTENT_COUNT = ["多少", "数量", "总数", "几个", "count", "统计"]
INTENT_RANK = ["最高", "最多", "最少", "排名", "前", "top", "销量", "排行"]
INTENT_GROUP_REGION = ["各地区", "每个地区", "按地区", "各区域", "分地区"]
INTENT_ROAMING = ["漫游", "roaming"]
INTENT_LIST = ["列表", "所有", "全部", "一览", "show", "list"]
INTENT_COMPARE = ["对比", "比较", "vs", " versus", " versus "]

# 同义词 → 额外推断表（规则引擎靠人工补充同义词来扩大覆盖，体现"从零实现"的工程成本）
BOOST_TABLES: dict[str, list[str]] = {
    "地区": ["users"], "区域": ["users"], "各省市": ["users"],
    "销量": ["orders", "plans"], "售卖": ["orders", "plans"], "卖了": ["orders", "plans"],
    "漫游": ["orders", "plans"],
}

# 时间表达式 → (operator, value)
TIME_PATTERNS = [
    (r"本月|这个月|当月", "DATE_FORMAT(NOW(), '%Y-%m-01')", "ge_month_start"),
    (r"上月|上个月|前一个月", "DATE_SUB(CURDATE(), INTERVAL 1 MONTH)", "ge_last_month"),
    (r"上周|过去一周|近一周|最近七天|最近7天", "DATE_SUB(CURDATE(), INTERVAL 7 DAY)", "ge_7d"),
    (r"最近(\d+)\s*天|过去(\d+)\s*天", None, "ge_ndays"),
    (r"今天|当日|今日", "CURDATE()", "ge_today"),
    (r"本年|今年", "DATE_FORMAT(NOW(), '%Y-01-01')", "ge_year_start"),
]


@dataclass
class BaselineResult:
    sql: str = ""
    matched_tables: list[str] = field(default_factory=list)
    intent: str = "unknown"
    handled: bool = False          # 基线是否能处理该问句
    note: str = ""


class BaselineNL2SQL:
    """纯规则 NL2SQL 引擎（无 LLM / 无 Vanna）"""

    def __init__(self) -> None:
        self.tables = BASELINE_TABLES

    # ---------- 表选择 ----------
    def _select_tables(self, q: str) -> list[str]:
        """按关键词匹配选出相关表（取命中最多的若干个）"""
        ql = q.lower()
        scored: dict[str, int] = {}
        for name, info in self.tables.items():
            score = sum(1 for kw in info.keywords if kw.lower() in ql)
            if score > 0:
                scored[name] = scored.get(name, 0) + score
        # 同义词推断：补充隐含相关表
        for kw, extra in BOOST_TABLES.items():
            if kw in q:
                for e in extra:
                    scored[e] = scored.get(e, 0) + 1
        ranked = sorted(scored.items(), key=lambda x: -x[1])
        return [n for n, _ in ranked]

    # ---------- 时间条件 ----------
    def _time_condition(self, q: str, column: str) -> Optional[str]:
        for pattern, value, kind in TIME_PATTERNS:
            m = re.search(pattern, q, re.IGNORECASE)
            if m:
                if kind == "ge_ndays":
                    n = m.group(1) or m.group(2)
                    return f"{column} >= DATE_SUB(CURDATE(), INTERVAL {n} DAY)"
                return f"{column} >= {value}"
        return None

    # ---------- 计数/列表意图 ----------
    def _build_single(self, table: str, q: str) -> BaselineResult:
        info = self.tables[table]
        # 列表类
        if any(k in q for k in INTENT_LIST):
            return BaselineResult(
                sql=f"SELECT {info.default_select} FROM {table}",
                matched_tables=[table], intent="single_table(list)", handled=True,
            )
        # 计数类（显式计数词，或含时间过滤隐含"统计新增"语义）
        tc = self._time_condition(q, info.time_column) if info.time_column else None
        is_count = any(k in q for k in INTENT_COUNT) or "统计" in q or tc is not None
        if is_count:
            dim = info.count_dimension
            # 按地区分组？
            if any(k in q for k in INTENT_GROUP_REGION) and "region" in info.groupable:
                return BaselineResult(
                    sql=f"SELECT region, COUNT({dim}) AS cnt FROM {table} GROUP BY region",
                    matched_tables=[table], intent="group_by(region)", handled=True,
                )
            sql = f"SELECT COUNT({dim}) AS cnt FROM {table}"
            # 时间过滤
            if tc:
                sql += f" WHERE {tc}"
            return BaselineResult(
                sql=sql, matched_tables=[table], intent="aggregation(count)", handled=True,
            )
        # 默认兜底：列表
        return BaselineResult(
            sql=f"SELECT {info.default_select} FROM {table}",
            matched_tables=[table], intent="single_table(default)", handled=True,
        )

    # ---------- 连接/排名/漫游 ----------
    def _build_join_or_rank(self, q: str, tables: list[str]) -> Optional[BaselineResult]:
        has_plans = "plans" in tables
        has_orders = "orders" in tables
        has_users = "users" in tables
        is_roaming = any(k in q for k in INTENT_ROAMING)
        is_region = any(k in q for k in INTENT_GROUP_REGION)

        # 对比意图优先（漫游 vs 普通）：订单 × 套餐 + CASE WHEN 聚合
        if (has_orders and has_plans) and any(k in q for k in INTENT_COMPARE):
            sql = (
                "SELECT "
                "SUM(CASE WHEN p.type = 'roaming' THEN 1 ELSE 0 END) AS roaming_orders, "
                "SUM(CASE WHEN p.type <> 'roaming' THEN 1 ELSE 0 END) AS normal_orders "
                "FROM orders o JOIN plans p ON o.plan_id = p.id"
            )
            return BaselineResult(sql=sql, matched_tables=["orders", "plans"],
                                  intent="comparison(roaming vs normal)", handled=True)

        # 优先：按地区分组（订单 × 用户 [× 套餐]）—— 覆盖"各地区订单/漫游订单"
        if has_orders and has_users and (is_region or (is_roaming and is_region)):
            join = "FROM orders o JOIN users u ON o.user_id = u.id"
            select_core = "u.region, COUNT(o.id) AS order_count"
            where = ""
            if is_roaming and has_plans:
                join = ("FROM orders o JOIN plans p ON o.plan_id = p.id "
                        "JOIN users u ON o.user_id = u.id")
                where = " WHERE p.type = 'roaming'"
                select_core = "u.region, COUNT(o.id) AS roaming_order_count"
            group = " GROUP BY u.region"
            sql = f"SELECT {select_core} {join}{where}{group}"
            return BaselineResult(sql=sql, matched_tables=["orders", "users"],
                                  intent="group_by(region orders)", handled=True)

        # 订单 × 套餐（销量排名 / 漫游销量）
        if has_orders and has_plans:
            join = ("FROM orders o JOIN plans p ON o.plan_id = p.id")
            select_core = "p.name, COUNT(o.id) AS order_count"
            where = ""
            if is_roaming:
                where = " WHERE p.type = 'roaming'"
            group = " GROUP BY p.name"
            sql = f"SELECT {select_core} {join}{where}{group}"
            # 排名 / Top N
            if any(k in q for k in INTENT_RANK):
                top_n = self._extract_top_n(q)
                sql += f" ORDER BY order_count DESC LIMIT {top_n}"
                intent = "ranking(top)"
            else:
                intent = "join(orders×plans)"
            if is_roaming:
                intent = "join(roaming orders)"
            return BaselineResult(sql=sql, matched_tables=["orders", "plans"],
                                  intent=intent, handled=True)

        # 对比意图：漫游订单 vs 普通订单（CASE WHEN 聚合）
        if (has_orders and has_plans) and any(k in q for k in INTENT_COMPARE):
            sql = (
                "SELECT "
                "SUM(CASE WHEN p.type = 'roaming' THEN 1 ELSE 0 END) AS roaming_orders, "
                "SUM(CASE WHEN p.type <> 'roaming' THEN 1 ELSE 0 END) AS normal_orders "
                "FROM orders o JOIN plans p ON o.plan_id = p.id"
            )
            return BaselineResult(sql=sql, matched_tables=["orders", "plans"],
                                  intent="comparison(roaming vs normal)", handled=True)

        return None

    @staticmethod
    def _extract_top_n(q: str, default: int = 5) -> int:
        m = re.search(r"前\s*(\d+)|top\s*(\d+)|(\d+)\s*个?排名", q, re.IGNORECASE)
        if m:
            for g in m.groups():
                if g:
                    return int(g)
        return default

    # ============================================================
    # 主入口
    # ============================================================
    def generate_sql(self, question: str) -> BaselineResult:
        """根据自然语言问题生成 SQL（纯规则）

        Returns:
            BaselineResult: 含 sql / handled（能否处理）/ intent
        """
        if not question or not question.strip():
            return BaselineResult(handled=False, note="空问题")

        q = question.strip()
        tables = self._select_tables(q)
        if not tables:
            return BaselineResult(
                handled=False,
                note="基线无法识别相关表（超出硬编码 schema 知识）",
            )

        # 优先尝试 连接/排名/漫游 意图（涉及多表）
        if len(tables) >= 2 or any(k in q for k in INTENT_ROAMING):
            res = self._build_join_or_rank(q, tables)
            if res:
                return res

        # 单表意图
        return self._build_single(tables[0], q)


# 全局单例
baseline_nl2sql = BaselineNL2SQL()
