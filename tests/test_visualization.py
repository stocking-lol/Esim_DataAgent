"""
Day 19: 查询结果可视化 — 测试套件
--------------------------------
覆盖：
1. recommend_chart_type 决策逻辑（line/bar/pie/table）
2. generate_chart_config 配置生成
3. generate_chart_html（plotly）生成可用性
"""

import pytest

from app.services.visualization import (
    generate_chart_config,
    generate_chart_html,
    recommend_chart_type,
)


class TestRecommendChartType:
    def test_time_series_line(self):
        data = [
            {"month": "2026-01", "orders": 10},
            {"month": "2026-02", "orders": 20},
        ]
        assert recommend_chart_type(data, ["month", "orders"]) == "line"

    def test_category_pie(self):
        data = [
            {"plan": "基础版", "count": 30},
            {"plan": "标准版", "count": 50},
            {"plan": "尊享版", "count": 20},
        ]
        # 类别数 <= 8 且单数值列 -> pie
        assert recommend_chart_type(data, ["plan", "count"]) == "pie"

    def test_many_categories_bar(self):
        data = [{"region": f"R{i}", "amount": i * 10} for i in range(12)]
        assert recommend_chart_type(data, ["region", "amount"]) == "bar"

    def test_empty_data_table(self):
        assert recommend_chart_type([], ["a", "b"]) == "table"

    def test_single_column_table(self):
        assert recommend_chart_type([{"a": 1}], ["a"]) == "table"


class TestGenerateChartConfig:
    def test_line_config(self):
        data = [
            {"month": "2026-01", "orders": 10},
            {"month": "2026-02", "orders": 20},
        ]
        cfg = generate_chart_config(data, ["month", "orders"], question="月度订单趋势")
        assert cfg["type"] == "line"
        assert cfg["category_column"] == "month"
        assert cfg["labels"] == ["2026-01", "2026-02"]
        assert "orders" in cfg["series"]
        assert cfg["title"] == "月度订单趋势"

    def test_pie_config(self):
        data = [
            {"plan": "A", "count": 30},
            {"plan": "B", "count": 70},
        ]
        cfg = generate_chart_config(data, ["plan", "count"])
        assert cfg["type"] == "pie"
        assert cfg["labels"] == ["A", "B"]
        assert cfg["values"] == [30, 70]
        assert cfg["value_column"] == "count"

    def test_bar_config(self):
        data = [
            {"region": "R1", "amount": 100},
            {"region": "R2", "amount": 200},
            {"region": "R3", "amount": 150},
        ]
        cfg = generate_chart_config(data, ["region", "amount"], chart_type="bar")
        assert cfg["type"] == "bar"
        assert cfg["series"]["amount"] == [100, 200, 150]

    def test_table_config(self):
        data = [{"a": 1, "b": 2, "c": 3}]
        cfg = generate_chart_config(data, ["a", "b", "c"])
        assert cfg["type"] == "table"
        assert cfg["data"] == data
        assert cfg["columns"] == ["a", "b", "c"]

    def test_empty_config(self):
        cfg = generate_chart_config([], [])
        assert cfg["type"] == "table"

    def test_forced_chart_type(self):
        data = [{"plan": "A", "count": 30}, {"plan": "B", "count": 70}]
        cfg = generate_chart_config(data, ["plan", "count"], chart_type="bar")
        assert cfg["type"] == "bar"


class TestGenerateChartHtml:
    def test_line_html(self):
        data = [{"month": "2026-01", "orders": 10}, {"month": "2026-02", "orders": 20}]
        cfg = generate_chart_config(data, ["month", "orders"])
        html = generate_chart_html(cfg)
        assert "<div" in html and "<script" in html

    def test_pie_html(self):
        data = [{"plan": "A", "count": 30}, {"plan": "B", "count": 70}]
        cfg = generate_chart_config(data, ["plan", "count"])
        html = generate_chart_html(cfg)
        assert "<div" in html

    def test_table_html(self):
        data = [{"a": 1, "b": 2}]
        cfg = generate_chart_config(data, ["a", "b"])
        html = generate_chart_html(cfg)
        assert "<div" in html
