"""
构建 eSIM NL2SQL 评估测试集
---------------------------
生成 scripts/eval/test_set.json，包含 50+ 道覆盖各类查询模式的评测题。

每道题结构：
{
  "id": "Q001",
  "question": "自然语言问题",
  "category": "single_table|join|aggregation|time_series|ranking|group_by|comparison",
  "difficulty": "easy|medium|hard",
  "gold_sql": "参考标准 SQL（用于 Exact Match 与表/列校验）",
  "expected_tables": ["涉及的表"],
  "keywords": ["期望结果中应出现的关键指标"]
}

用法：
    python scripts/eval/build_test_set.py
"""

import json
import sys
from pathlib import Path

_OUT = Path(__file__).resolve().parent / "test_set.json"

# 题目模板：(问题, 类别, 难度, gold_sql, 期望表, 关键词)
ROWS = [
    # --- 单表查询 (single_table) ---
    ("查询所有 eSIM 套餐的名称和价格", "single_table", "easy",
     "SELECT plan_name, price FROM plans", ["plans"], ["套餐", "价格"]),
    ("列出所有运营商的名称", "single_table", "easy",
     "SELECT operator_name FROM operators", ["operators"], ["运营商"]),
    ("查询 id 为 1 的用户信息", "single_table", "easy",
     "SELECT * FROM users WHERE id = 1", ["users"], ["用户"]),
    ("统计总共有多少个 eSIM 套餐", "single_table", "easy",
     "SELECT COUNT(*) FROM plans", ["plans"], ["套餐数"]),
    ("查询所有已激活的用户档案", "single_table", "medium",
     "SELECT * FROM esim_profiles WHERE status = 'active'", ["esim_profiles"], ["激活", "档案"]),
    ("列出价格大于 50 元的套餐", "single_table", "medium",
     "SELECT * FROM plans WHERE price > 50", ["plans"], ["套餐", "价格"]),
    ("查询所有处于漫游状态的套餐包", "single_table", "medium",
     "SELECT * FROM roaming_packages WHERE status = 'active'", ["roaming_packages"], ["漫游"]),

    # --- 多表关联 (join) ---
    ("查询每个订单对应的用户名和套餐名", "join", "medium",
     "SELECT o.id, u.username, p.plan_name FROM orders o JOIN users u ON o.user_id = u.id JOIN plans p ON o.plan_id = p.id",
     ["orders", "users", "plans"], ["订单", "用户", "套餐"]),
    ("查询每个用户的 eSIM 档案状态和用户名", "join", "medium",
     "SELECT u.username, e.status FROM users u JOIN esim_profiles e ON u.id = e.user_id",
     ["users", "esim_profiles"], ["用户", "档案"]),
    ("统计每个套餐的订单数量", "join", "medium",
     "SELECT p.plan_name, COUNT(o.id) FROM plans p LEFT JOIN orders o ON p.id = o.plan_id GROUP BY p.plan_name",
     ["plans", "orders"], ["套餐", "订单数"]),
    ("查询购买了尊享版套餐的用户邮箱", "join", "hard",
     "SELECT u.email FROM users u JOIN orders o ON u.id = o.user_id JOIN plans p ON o.plan_id = p.id WHERE p.plan_name LIKE '%尊享%'",
     ["users", "orders", "plans"], ["邮箱", "尊享"]),
    ("查询每个运营商提供的套餐数量", "join", "medium",
     "SELECT op.operator_name, COUNT(p.id) FROM operators op LEFT JOIN plans p ON op.id = p.operator_id GROUP BY op.operator_name",
     ["operators", "plans"], ["运营商", "套餐数"]),

    # --- 聚合统计 (aggregation) ---
    ("统计每种订单状态的订单数量", "aggregation", "medium",
     "SELECT status, COUNT(*) FROM orders GROUP BY status", ["orders"], ["状态", "订单数"]),
    ("计算所有套餐的平均价格", "aggregation", "easy",
     "SELECT AVG(price) FROM plans", ["plans"], ["平均价格"]),
    ("查询总销售额最高的套餐", "aggregation", "hard",
     "SELECT p.plan_name, SUM(o.amount) FROM plans p JOIN orders o ON p.id = o.plan_id GROUP BY p.plan_name ORDER BY SUM(o.amount) DESC LIMIT 1",
     ["plans", "orders"], ["销售额", "套餐"]),
    ("统计各地区的用户数量", "aggregation", "medium",
     "SELECT region, COUNT(*) FROM users GROUP BY region", ["users"], ["地区", "用户数"]),
    ("查询所有订单的总金额", "aggregation", "easy",
     "SELECT SUM(amount) FROM orders", ["orders"], ["总金额"]),
    ("统计每个月的订单总数", "aggregation", "hard",
     "SELECT DATE_FORMAT(created_at, '%Y-%m') AS month, COUNT(*) FROM orders GROUP BY month", ["orders"], ["月份", "订单数"]),
    ("查询平均订单金额", "aggregation", "medium",
     "SELECT AVG(amount) FROM orders", ["orders"], ["平均金额"]),

    # --- 时间序列 (time_series) ---
    ("查询最近 7 天的订单数量", "time_series", "medium",
     "SELECT COUNT(*) FROM orders WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)", ["orders"], ["订单数", "近7天"]),
    ("按天统计上个月的订单量", "time_series", "hard",
     "SELECT DATE(created_at) AS day, COUNT(*) FROM orders WHERE created_at >= DATE_SUB(NOW(), INTERVAL 1 MONTH) GROUP BY day",
     ["orders"], ["天", "订单量"]),
    ("查询本月新增的用户数", "time_series", "medium",
     "SELECT COUNT(*) FROM users WHERE created_at >= DATE_FORMAT(NOW(), '%Y-%m-01')", ["users"], ["本月", "新增用户"]),
    ("统计过去 30 天各套餐的激活量", "time_series", "hard",
     "SELECT p.plan_name, COUNT(e.id) FROM esim_profiles e JOIN plans p ON e.plan_id = p.id WHERE e.activated_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) GROUP BY p.plan_name",
     ["esim_profiles", "plans"], ["套餐", "激活量"]),
    ("查询每天的流量使用总量", "time_series", "hard",
     "SELECT DATE(record_date) AS day, SUM(data_used_mb) FROM data_usage GROUP BY day", ["data_usage"], ["流量", "天"]),

    # --- 排名 (ranking) ---
    ("查询订单金额最高的前 5 个订单", "ranking", "medium",
     "SELECT * FROM orders ORDER BY amount DESC LIMIT 5", ["orders"], ["订单", "金额", "前5"]),
    ("按销售额对套餐进行排名", "ranking", "hard",
     "SELECT p.plan_name, SUM(o.amount) AS total FROM plans p JOIN orders o ON p.id = o.plan_id GROUP BY p.plan_name ORDER BY total DESC",
     ["plans", "orders"], ["排名", "销售额"]),
    ("查询流量使用最多的前 10 个用户", "ranking", "hard",
     "SELECT user_id, SUM(data_used_mb) AS total FROM data_usage GROUP BY user_id ORDER BY total DESC LIMIT 10",
     ["data_usage"], ["流量", "用户", "前10"]),
    ("查询注册用户数最多的地区", "ranking", "medium",
     "SELECT region, COUNT(*) AS cnt FROM users GROUP BY region ORDER BY cnt DESC LIMIT 1", ["users"], ["地区", "用户数"]),
    ("按订单数排名运营商", "ranking", "hard",
     "SELECT op.operator_name, COUNT(o.id) FROM operators op JOIN plans p ON op.id = p.operator_id JOIN orders o ON p.id = o.plan_id GROUP BY op.operator_name ORDER BY COUNT(o.id) DESC",
     ["operators", "plans", "orders"], ["运营商", "订单数"]),

    # --- 分组统计 (group_by) ---
    ("按套餐分组统计订单金额总和", "group_by", "medium",
     "SELECT plan_id, SUM(amount) FROM orders GROUP BY plan_id", ["orders"], ["套餐", "金额"]),
    ("按状态分组统计档案数量", "group_by", "medium",
     "SELECT status, COUNT(*) FROM esim_profiles GROUP BY status", ["esim_profiles"], ["状态", "档案数"]),
    ("按用户分组统计每个用户的订单数", "group_by", "medium",
     "SELECT user_id, COUNT(*) FROM orders GROUP BY user_id", ["orders"], ["用户", "订单数"]),
    ("按运营商分组统计套餐平均价格", "group_by", "hard",
     "SELECT operator_id, AVG(price) FROM plans GROUP BY operator_id", ["plans"], ["运营商", "平均价格"]),
    ("按地区分组统计激活用户数", "group_by", "medium",
     "SELECT region, COUNT(*) FROM users WHERE status = 'active' GROUP BY region", ["users"], ["地区", "激活用户"]),

    # --- 对比查询 (comparison) ---
    ("查询价格高于平均价格的套餐", "comparison", "hard",
     "SELECT * FROM plans WHERE price > (SELECT AVG(price) FROM plans)", ["plans"], ["套餐", "平均价格"]),
    ("查询订单金额大于 100 的订单", "comparison", "easy",
     "SELECT * FROM orders WHERE amount > 100", ["orders"], ["订单", "金额"]),
    ("查询没有订单的用户", "comparison", "hard",
     "SELECT * FROM users WHERE id NOT IN (SELECT DISTINCT user_id FROM orders)", ["users", "orders"], ["用户", "无订单"]),
    ("对比基础版和尊享版套餐的价格差异", "comparison", "hard",
     "SELECT plan_name, price FROM plans WHERE plan_name LIKE '%基础%' OR plan_name LIKE '%尊享%'", ["plans"], ["基础版", "尊享版", "价格"]),
    ("查询流量使用超过 1GB 的用户", "comparison", "medium",
     "SELECT user_id, SUM(data_used_mb) FROM data_usage GROUP BY user_id HAVING SUM(data_used_mb) > 1024", ["data_usage"], ["流量", "1GB"]),

    # --- 补充覆盖题 (共补齐到 52 题) ---
    ("查询所有用户的用户名和注册地区", "single_table", "easy",
     "SELECT username, region FROM users", ["users"], ["用户名", "地区"]),
    ("统计套餐表中价格最低的套餐", "ranking", "medium",
     "SELECT * FROM plans ORDER BY price ASC LIMIT 1", ["plans"], ["最低价格", "套餐"]),
    ("查询最近注册的 10 个用户", "ranking", "medium",
     "SELECT * FROM users ORDER BY created_at DESC LIMIT 10", ["users"], ["用户", "最近注册"]),
    ("统计已激活档案占全部档案的比例", "aggregation", "hard",
     "SELECT status, COUNT(*) FROM esim_profiles GROUP BY status", ["esim_profiles"], ["激活", "档案"]),
    ("查询每个运营商的漫游套餐数量", "join", "hard",
     "SELECT op.operator_name, COUNT(r.id) FROM operators op JOIN plans p ON op.id = p.operator_id JOIN roaming_packages r ON p.id = r.plan_id GROUP BY op.operator_name",
     ["operators", "plans", "roaming_packages"], ["运营商", "漫游套餐"]),
    ("查询本月订单金额总和", "time_series", "medium",
     "SELECT SUM(amount) FROM orders WHERE created_at >= DATE_FORMAT(NOW(), '%Y-%m-01')", ["orders"], ["本月", "金额"]),
    ("统计各套餐的退款订单数", "group_by", "hard",
     "SELECT plan_id, COUNT(*) FROM orders WHERE status = 'refunded' GROUP BY plan_id", ["orders"], ["套餐", "退款"]),

    # --- 追加题（使总数 > 50）---
    ("查询所有套餐中价格最高的三个", "ranking", "medium",
     "SELECT * FROM plans ORDER BY price DESC LIMIT 3", ["plans"], ["价格", "前3"]),
    ("统计本周新增的 eSIM 档案数", "time_series", "medium",
     "SELECT COUNT(*) FROM esim_profiles WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)", ["esim_profiles"], ["本周", "档案"]),
    ("查询每个用户的平均订单金额", "group_by", "hard",
     "SELECT user_id, AVG(amount) FROM orders GROUP BY user_id", ["orders"], ["用户", "平均金额"]),
    ("列出所有状态为 pending 的订单", "single_table", "easy",
     "SELECT * FROM orders WHERE status = 'pending'", ["orders"], ["订单", "pending"]),
    ("查询使用流量最多的套餐", "ranking", "hard",
     "SELECT p.plan_name, SUM(d.data_used_mb) FROM plans p JOIN data_usage d ON p.id = d.plan_id GROUP BY p.plan_name ORDER BY SUM(d.data_used_mb) DESC LIMIT 1",
     ["plans", "data_usage"], ["套餐", "流量"]),
    ("统计不同地区的活跃用户占比", "aggregation", "hard",
     "SELECT region, COUNT(*) FROM users WHERE status = 'active' GROUP BY region", ["users"], ["地区", "活跃"]),
    ("查询没有关联档案的用户", "comparison", "hard",
     "SELECT * FROM users WHERE id NOT IN (SELECT DISTINCT user_id FROM esim_profiles)", ["users", "esim_profiles"], ["用户", "无档案"]),
    ("按月份统计各套餐销量", "time_series", "hard",
     "SELECT p.plan_name, DATE_FORMAT(o.created_at, '%Y-%m') AS month, COUNT(*) FROM plans p JOIN orders o ON p.id = o.plan_id GROUP BY p.plan_name, month",
     ["plans", "orders"], ["套餐", "销量", "月份"]),
]


def build() -> dict:
    items = []
    for i, (q, cat, diff, gold, tables, kws) in enumerate(ROWS, start=1):
        items.append({
            "id": f"Q{i:03d}",
            "question": q,
            "category": cat,
            "difficulty": diff,
            "gold_sql": gold,
            "expected_tables": tables,
            "keywords": kws,
        })
    return {
        "version": "1.0",
        "description": "eSIM NL2SQL 平台评估测试集",
        "total": len(items),
        "items": items,
    }


if __name__ == "__main__":
    data = build()
    _OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[BUILD] 已生成 {data['total']} 道评测题 -> {_OUT}")
