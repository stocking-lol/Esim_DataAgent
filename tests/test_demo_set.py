"""
演示用例集校验测试
------------------
保证 scripts/eval/demo_set.json 结构完整、gold_sql 可被 MySQL 方言解析、
category/feature 取值合法。gold_sql 直接对照真实 eSIM schema，语法错误会在此暴露。

运行：
    pytest tests/test_demo_set.py -v
"""

import json
from pathlib import Path

import pytest
import sqlglot

_DEMO_PATH = Path(__file__).resolve().parent.parent / "scripts" / "eval" / "demo_set.json"

REQUIRED_KEYS = ["id", "feature", "category", "title", "question", "gold_sql", "expected", "attack"]

ALLOWED_FEATURES = {
    "basic_nl2sql",
    "security",
    "rls",
    "masking",
    "self_correction",
    "visualization",
    "baseline_vs_vanna",
}

ALLOWED_CATEGORIES = {
    "single_table",
    "join",
    "aggregation",
    "time_series",
    "ranking",
    "group_by",
    "comparison",
    "sql_injection",
    "prompt_injection",
    "union_injection",
    "row_level_security",
    "data_masking",
    "error_recovery",
    "chart",
}


@pytest.fixture(scope="module")
def demos():
    with open(_DEMO_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_demo_file_is_valid_json(demos):
    assert isinstance(demos, dict)
    assert "items" in demos
    assert isinstance(demos["items"], list)
    assert len(demos["items"]) >= 10, "演示用例应覆盖多类场景（>=10）"


def test_each_demo_has_required_keys(demos):
    for d in demos["items"]:
        for key in REQUIRED_KEYS:
            assert key in d, f"{d.get('id', '?')} 缺少字段 {key}"
        assert isinstance(d["attack"], bool), f"{d['id']} 的 attack 必须为布尔"


def test_feature_and_category_in_allowlist(demos):
    for d in demos["items"]:
        assert d["feature"] in ALLOWED_FEATURES, f"{d['id']} 非法 feature: {d['feature']}"
        assert d["category"] in ALLOWED_CATEGORIES, f"{d['id']} 非法 category: {d['category']}"


def test_ids_are_unique(demos):
    ids = [d["id"] for d in demos["items"]]
    assert len(ids) == len(set(ids)), "演示用例 id 必须唯一"


def test_gold_sql_parses_for_non_attack(demos):
    """非攻击类且有 gold_sql 的用例，SQL 必须能被 MySQL 方言正确解析。"""
    for d in demos["items"]:
        gold = (d.get("gold_sql") or "").strip()
        if d["attack"] or not gold:
            continue
        try:
            sqlglot.parse_one(gold, read="mysql")
        except Exception as e:
            pytest.fail(f"{d['id']} 的 gold_sql 解析失败: {e}\nSQL: {gold}")


def test_security_demos_are_marked_attack(demos):
    """security feature 的用例必须标记为 attack（演示拦截）。"""
    for d in demos["items"]:
        if d["feature"] == "security":
            assert d["attack"] is True, f"{d['id']} 安全用例应标记 attack=true"


def test_feature_coverage_breadth(demos):
    """演示集应覆盖平台核心能力，而非只有基础查询。"""
    features = {d["feature"] for d in demos["items"]}
    for must in ["basic_nl2sql", "security", "rls", "masking", "self_correction", "visualization"]:
        assert must in features, f"演示集缺少核心能力演示: {must}"
