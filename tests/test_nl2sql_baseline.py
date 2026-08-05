"""
非 Vanna NL2SQL 基线引擎测试
=========================
验证纯规则引擎对 7 类意图的确定性生成，以及其固有脆弱性（超出模板即退化）。

这是 Vanna（LLM）技术选型的对照基线：确定性、零成本，但只能处理写死的句式。
"""

from app.core.nl2sql_baseline import baseline_nl2sql, BaselineNL2SQL


def _gen(q):
    return baseline_nl2sql.generate_sql(q)


# ============================================================
# 7 类意图覆盖（确定性生成）
# ============================================================

class TestCoverage:
    """基线能稳定处理的 7 类句式"""

    def test_single_table_list(self):
        r = _gen("显示套餐列表")
        assert r.handled and r.intent.startswith("single_table")
        assert r.sql == "SELECT * FROM plans"

    def test_aggregation_count(self):
        r = _gen("用户总数是多少")
        assert r.handled
        assert r.sql == "SELECT COUNT(id) AS cnt FROM users"

    def test_group_by_region(self):
        r = _gen("各地区用户数量")
        assert r.handled and r.intent == "group_by(region)"
        assert "GROUP BY region" in r.sql
        assert "COUNT(id)" in r.sql

    def test_time_series_this_month(self):
        r = _gen("本月新增用户数")
        assert r.handled and "WHERE created_at >=" in r.sql
        assert "DATE_FORMAT(NOW(), '%Y-%m-01')" in r.sql

    def test_time_series_last_month(self):
        r = _gen("上月新增用户数")
        assert "DATE_SUB(CURDATE(), INTERVAL 1 MONTH)" in r.sql

    def test_join_orders_plans(self):
        r = _gen("各套餐的订单数")
        assert r.handled
        assert "JOIN plans p ON o.plan_id = p.id" in r.sql
        assert "GROUP BY p.name" in r.sql

    def test_ranking_top_n(self):
        r = _gen("各套餐订单销量排名前3")
        assert r.handled and r.intent == "ranking(top)"
        assert "ORDER BY order_count DESC LIMIT 3" in r.sql

    def test_region_roaming_orders(self):
        r = _gen("各地区漫游订单数")
        assert r.handled and r.intent == "group_by(region orders)"
        assert "p.type = 'roaming'" in r.sql
        assert "JOIN users u ON o.user_id = u.id" in r.sql
        assert "GROUP BY u.region" in r.sql

    def test_comparison_roaming_vs_normal(self):
        r = _gen("漫游订单与普通订单对比")
        assert r.handled and r.intent == "comparison(roaming vs normal)"
        assert "roaming_orders" in r.sql and "normal_orders" in r.sql
        assert "CASE WHEN p.type = 'roaming'" in r.sql


# ============================================================
# 固有脆弱性（规则引擎的边界）
# ============================================================

class TestBrittleness:
    """基线无法理解、或退化生成错误 SQL 的情形 —— 正是 LLM 方案的差距"""

    def test_out_of_schema_returns_unhandled(self):
        """完全超出硬编码 schema 知识 → 诚实声明无法处理"""
        r = _gen("今天天气怎么样")
        assert r.handled is False
        assert "无法识别" in r.note

    def test_novel_phrasing_degrades(self):
        """换种说法（'利润最高的运营商'）基线只能退化成列表，无法做聚合排名"""
        r = _gen("告诉我利润最高的运营商")
        assert r.handled
        # 基线没有"利润/最高"聚合能力，只能退化成 SELECT * —— 这是错误/无用的
        assert r.sql == "SELECT * FROM operators"

    def test_semantic_filter_ignored(self):
        """'激活失败的用户'含语义过滤(status)，基线忽略之，只做计数 —— 结果错误"""
        r = _gen("统计上月激活失败的用户")
        assert r.handled
        # 时间对了，但 status='failed' 过滤被忽略，语义不完整
        assert "status" not in r.sql
