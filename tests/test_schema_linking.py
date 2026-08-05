"""Schema Linking 测试"""
import pytest
from app.core.schema_linking import schema_linker, TABLE_METADATA, ROLE_PERMISSIONS


class TestSchemaLinking:
    """Schema Linking 优化器测试"""

    def test_extract_entities_users(self):
        """提取用户相关实体"""
        entities = schema_linker.extract_entities("本月新增多少eSIM用户")
        assert "users" in entities

    def test_extract_entities_plans(self):
        """提取套餐相关实体"""
        entities = schema_linker.extract_entities("各套餐的销量排名")
        assert "plans" in entities

    def test_extract_entities_multi_table(self):
        """提取多表实体"""
        entities = schema_linker.extract_entities("各运营商的ARPU值和用户数")
        assert "operators" in entities
        assert "users" in entities

    def test_rank_tables_relevance(self):
        """表排序相关性"""
        ranked = schema_linker.rank_tables("查询流量使用TOP10用户")
        # users 和 data_usage 应该排在前面
        table_names = [name for name, _ in ranked]
        assert "data_usage" in table_names[:3]
        assert "users" in table_names[:3]

    def test_get_top_k_tables(self):
        """获取 Top-K 表"""
        top = schema_linker.get_top_k_tables("本月新增多少用户", k=3, role="analyst")
        assert len(top) <= 3
        assert "users" in top

    def test_get_top_k_tables_viewer_filtered(self):
        """viewer 角色表过滤"""
        top = schema_linker.get_top_k_tables(
            "查询所有Profile的状态", k=5, role="viewer"
        )
        # viewer 不能访问 esim_profiles
        assert "esim_profiles" not in top

    def test_check_table_access_admin(self):
        """admin 可访问所有表"""
        for table in TABLE_METADATA:
            assert schema_linker.check_table_access("admin", table)

    def test_check_table_access_viewer_blocked(self):
        """viewer 不能访问 esim_profiles"""
        assert not schema_linker.check_column_access("viewer", "esim_profiles", "iccid")

    def test_check_column_access_viewer_allowed(self):
        """viewer 可访问 plans 的所有列"""
        assert schema_linker.check_column_access("viewer", "plans", "price")

    def test_check_column_access_viewer_restricted(self):
        """viewer 访问 users 表受限列"""
        # viewer 可以看 users.id
        assert schema_linker.check_column_access("viewer", "users", "id")
        # viewer 不能看 users.phone_number
        assert not schema_linker.check_column_access("viewer", "users", "phone_number")

    def test_filter_ddl_by_role_admin(self):
        """admin DDL 不被过滤"""
        ddl_map = schema_linker.filter_ddl_by_role("admin")
        assert len(ddl_map) == len(TABLE_METADATA)

    def test_filter_ddl_by_role_viewer(self):
        """viewer DDL 被过滤"""
        ddl_map = schema_linker.filter_ddl_by_role("viewer")
        # viewer 只能看 plans, operators, roaming_packages
        assert "esim_profiles" not in ddl_map
        assert "plans" in ddl_map
        assert "operators" in ddl_map

    def test_get_schema_context(self):
        """获取 Schema 上下文"""
        context = schema_linker.get_schema_context(
            "各套餐的销量排名", role="analyst", max_tables=3
        )
        assert "plans" in context
        assert "orders" in context

    def test_get_schema_context_viewer_restricted(self):
        """viewer 的 Schema 上下文敏感列被过滤"""
        context = schema_linker.get_schema_context(
            "查询所有用户信息", role="viewer", max_tables=5
        )
        # viewer 可以看 users 表，但敏感列被过滤
        assert "users" in context
        # 不应包含手机号、邮箱等敏感列
        assert "phone_number" not in context
        assert "email" not in context
        assert "iccid" not in context
        assert "imsi" not in context
        # 不应包含 esim_profiles（viewer 无权访问）
        assert "esim_profiles" not in context
