"""
Day 22: 评估基准 — 测试套件
--------------------------
覆盖评估脚本的核心工具函数（归一化、表提取、gold SQL 静态校验）。
"""

import pytest

from scripts.eval import run_eval as ev


class TestEvalUtils:
    def test_normalize_sql(self):
        assert ev.normalize_sql("SELECT  * FROM  users ;") == "select * from users"
        assert ev.normalize_sql("  SELECT 1  ") == "select 1"

    def test_extract_tables(self):
        tables = ev.extract_tables("SELECT u.id FROM users u JOIN orders o ON u.id=o.user_id")
        assert tables == {"users", "orders"}
        # 子查询中的表也应被提取
        tables2 = ev.extract_tables("SELECT * FROM plans WHERE id IN (SELECT plan_id FROM orders)")
        assert {"plans", "orders"}.issubset(tables2)

    def test_gold_static_check_valid(self):
        valid = {"users", "plans", "orders"}
        info = ev.gold_static_check("SELECT * FROM users", valid)
        assert info["parse_ok"] is True
        assert info["tables_ok"] is True

    def test_gold_static_check_unknown_table(self):
        valid = {"users"}
        info = ev.gold_static_check("SELECT * FROM nonexistent", valid)
        assert info["parse_ok"] is True
        assert info["tables_ok"] is False

    def test_build_test_set_count(self):
        # 构建测试集并验证数量 >= 50
        data = ev.build() if hasattr(ev, "build") else None
        # build 在 build_test_set 模块中，这里直接从文件读取
        import json
        from pathlib import Path
        ts = Path(ev._TEST_SET)
        if ts.exists():
            obj = json.loads(ts.read_text(encoding="utf-8"))
            assert obj["total"] >= 50
