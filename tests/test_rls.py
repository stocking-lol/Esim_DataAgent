"""
行级安全 (RLS) 测试
------------------
测试 MVNO 租户隔离功能，确保非 admin 用户只能访问自己所属 MVNO 的数据。

测试覆盖：
  1. admin 不注入 RLS 条件
  2. analyst 只看到自己 mvno_id 的数据
  3. JOIN 查询中 RLS 条件正确注入
  4. 子查询中 RLS 条件正确注入
  5. UNION 查询中 RLS 条件正确注入
  6. CTE (WITH) 查询中 RLS 条件正确注入
  7. 多表 JOIN 中每个表都注入 RLS
  8. 无 mvno_id 的表不注入 RLS
  9. RLS 绕过尝试被拦截
  10. verify_rls 验证功能
"""

import pytest

from app.services.rls_service import rls_service, RLSResult


class TestRLSInjection:
    """RLS 条件注入测试"""

    def test_admin_no_rls(self):
        """admin 用户不注入 RLS 条件"""
        sql = "SELECT * FROM users"
        result = rls_service.inject_rls(sql, role="admin", mvno_id=1)
        assert not result.rls_applied
        assert result.sql == sql
        assert "mvno_id" not in result.sql.lower()

    def test_analyst_rls_injected(self):
        """analyst 用户注入 mvno_id 条件"""
        sql = "SELECT * FROM users"
        result = rls_service.inject_rls(sql, role="analyst", mvno_id=1)
        assert result.rls_applied
        assert "users" in result.rls_tables
        assert "mvno_id" in result.sql.lower()
        assert "= 1" in result.sql

    def test_viewer_rls_injected(self):
        """viewer 用户注入 mvno_id 条件"""
        sql = "SELECT * FROM orders"
        result = rls_service.inject_rls(sql, role="viewer", mvno_id=2)
        assert result.rls_applied
        assert "orders" in result.rls_tables
        assert "mvno_id" in result.sql.lower()
        assert "= 2" in result.sql

    def test_rls_with_join(self):
        """JOIN 查询中 RLS 条件正确注入"""
        sql = """
        SELECT u.id, u.phone_number, o.amount
        FROM users u
        JOIN orders o ON u.id = o.user_id
        """
        result = rls_service.inject_rls(sql, role="analyst", mvno_id=1)
        assert result.rls_applied
        assert "users" in result.rls_tables
        assert "orders" in result.rls_tables
        # SQL 应包含两个 mvno_id 条件
        sql_lower = result.sql.lower()
        assert "mvno_id" in sql_lower
        # 确保条件中使用了别名
        assert "u.mvno_id" in sql_lower or "users.mvno_id" in sql_lower
        assert "o.mvno_id" in sql_lower or "orders.mvno_id" in sql_lower

    def test_rls_with_subquery(self):
        """子查询中 RLS 条件正确注入"""
        sql = """
        SELECT * FROM (
            SELECT id, phone_number, mvno_id FROM users
        ) AS sub
        """
        result = rls_service.inject_rls(sql, role="analyst", mvno_id=3)
        assert result.rls_applied
        assert "mvno_id" in result.sql.lower()
        assert "= 3" in result.sql

    def test_rls_with_union(self):
        """UNION 查询中每个 SELECT 都注入 RLS"""
        sql = """
        SELECT id, 'user' as type FROM users
        UNION
        SELECT id, 'order' as type FROM orders
        """
        result = rls_service.inject_rls(sql, role="analyst", mvno_id=1)
        assert result.rls_applied
        # mvno_id 应该出现至少两次（每个 SELECT 各一次）
        count = result.sql.lower().count("mvno_id")
        assert count >= 2, f"Expected mvno_id at least 2 times, got {count}"

    def test_rls_with_cte(self):
        """CTE (WITH) 查询中 RLS 条件正确注入"""
        sql = """
        WITH user_stats AS (
            SELECT id, status FROM users WHERE status = 'active'
        )
        SELECT * FROM user_stats
        """
        result = rls_service.inject_rls(sql, role="analyst", mvno_id=1)
        assert result.rls_applied
        assert "mvno_id" in result.sql.lower()

    def test_rls_multi_table_join(self):
        """4表 JOIN 中每个含 mvno_id 的表都注入 RLS"""
        sql = """
        SELECT u.id, p.name, o.amount, ep.iccid
        FROM users u
        JOIN plans p ON u.mvno_id = p.mvno_id
        JOIN orders o ON u.id = o.user_id
        JOIN esim_profiles ep ON u.id = ep.user_id
        """
        result = rls_service.inject_rls(sql, role="analyst", mvno_id=1)
        assert result.rls_applied
        # users, plans, orders, esim_profiles 都有 mvno_id
        assert "users" in result.rls_tables
        assert "plans" in result.rls_tables
        assert "orders" in result.rls_tables
        assert "esim_profiles" in result.rls_tables

    def test_no_rls_for_reference_tables(self):
        """无 mvno_id 的表不注入 RLS"""
        sql = "SELECT * FROM operators"
        result = rls_service.inject_rls(sql, role="analyst", mvno_id=1)
        assert not result.rls_applied
        assert "mvno_id" not in result.sql.lower()

    def test_no_rls_for_roaming_packages(self):
        """roaming_packages 表不注入 RLS"""
        sql = "SELECT * FROM roaming_packages"
        result = rls_service.inject_rls(sql, role="viewer", mvno_id=1)
        assert not result.rls_applied

    def test_rls_preserves_existing_where(self):
        """RLS 注入保留现有 WHERE 条件"""
        sql = "SELECT * FROM users WHERE status = 'active'"
        result = rls_service.inject_rls(sql, role="analyst", mvno_id=1)
        assert result.rls_applied
        sql_lower = result.sql.lower()
        assert "status" in sql_lower
        assert "active" in sql_lower
        assert "mvno_id" in sql_lower

    def test_rls_with_different_mvno_ids(self):
        """不同 mvno_id 注入不同条件值"""
        sql = "SELECT * FROM users"
        r1 = rls_service.inject_rls(sql, role="analyst", mvno_id=1)
        r2 = rls_service.inject_rls(sql, role="analyst", mvno_id=5)
        assert "= 1" in r1.sql
        assert "= 5" in r2.sql
        assert r1.sql != r2.sql

    def test_non_admin_without_mvno_id(self):
        """非 admin 用户无 mvno_id 时返回限制性条件"""
        condition = rls_service.get_rls_condition(role="analyst", mvno_id=None)
        assert condition == "1=0"


class TestRLSVerification:
    """RLS 验证测试"""

    def test_verify_admin_passes(self):
        """admin 用户 RLS 验证通过"""
        ok, msg = rls_service.verify_rls(
            "SELECT * FROM users", role="admin", mvno_id=None
        )
        assert ok

    def test_verify_rls_present(self):
        """有 mvno_id 条件的 SQL 验证通过"""
        sql = "SELECT * FROM users WHERE mvno_id = 1"
        ok, msg = rls_service.verify_rls(sql, role="analyst", mvno_id=1)
        assert ok

    def test_verify_rls_missing(self):
        """缺少 mvno_id 条件的 SQL 验证失败"""
        sql = "SELECT * FROM users WHERE status = 'active'"
        ok, msg = rls_service.verify_rls(sql, role="analyst", mvno_id=1)
        assert not ok
        assert "missing" in msg.lower() or "mvno_id" in msg.lower()

    def test_verify_no_rls_tables(self):
        """无 RLS 表的查询验证通过"""
        sql = "SELECT * FROM operators"
        ok, msg = rls_service.verify_rls(sql, role="analyst", mvno_id=1)
        assert ok


class TestRLSBypass:
    """RLS 绕过测试"""

    def test_rls_cannot_be_bypassed_by_subquery(self):
        """子查询中的表也受 RLS 保护"""
        sql = """
        SELECT * FROM users
        WHERE id IN (
            SELECT user_id FROM orders WHERE amount > 100
        )
        """
        result = rls_service.inject_rls(sql, role="analyst", mvno_id=1)
        assert result.rls_applied
        # 子查询中的 orders 表也应注入 RLS
        assert "orders" in result.rls_tables

    def test_rls_on_count_query(self):
        """COUNT 查询也注入 RLS"""
        sql = "SELECT COUNT(*) FROM users"
        result = rls_service.inject_rls(sql, role="analyst", mvno_id=1)
        assert result.rls_applied
        assert "mvno_id" in result.sql.lower()

    def test_rls_on_aggregate_query(self):
        """聚合查询注入 RLS"""
        sql = """
        SELECT mvno_id, COUNT(*) as cnt
        FROM users
        GROUP BY mvno_id
        """
        result = rls_service.inject_rls(sql, role="analyst", mvno_id=1)
        assert result.rls_applied
        # 即使 GROUP BY 包含 mvno_id，RLS 也应注入
        assert result.sql.lower().count("mvno_id") >= 2
