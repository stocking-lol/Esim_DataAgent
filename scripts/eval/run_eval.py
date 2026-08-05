"""
eSIM NL2SQL 评估脚本
--------------------
对测试集运行评估，计算核心指标：

  - Execution Accuracy (执行准确率): 生成的 SQL 通过安全校验且涉及的表与基准表一致
  - Exact Match (精确匹配率):       生成 SQL 归一化后与 gold SQL 字符串完全一致
  - 分组统计:                       按类别(category)与难度(difficulty)拆分指标

两种运行模式：
  1. 静态模式（默认，无需 Vanna Agent）：
     校验测试集中 gold SQL 的语法合法性与表白名单合规率，作为基准质量基线。
  2. 实时模式（--live，需 Agent 已初始化或指定 --endpoint）：
     对每道题调用 NL2SQL 管线获取生成 SQL，与 gold SQL 比对，计算真实 EM / ExecAcc。

用法：
    python scripts/eval/run_eval.py                # 静态基线
    python scripts/eval/run_eval.py --live          # 实时评估（需 Agent 就绪）
    python scripts/eval/run_eval.py --live --endpoint http://localhost:8000

输出：
    scripts/eval/report.json  +  控制台摘要
"""

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import sqlglot
from sqlglot import exp

_EVAL_DIR = Path(__file__).resolve().parent
_TEST_SET = _EVAL_DIR / "test_set.json"
_REPORT = _EVAL_DIR / "report.json"


# ============================================================
# SQL 归一化与解析工具
# ============================================================

def normalize_sql(sql: str) -> str:
    """归一化 SQL：小写、折叠空白、去除末尾分号"""
    if not sql:
        return ""
    s = sql.strip().lower()
    s = s.rstrip(";")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def extract_tables(sql: str) -> set[str]:
    """从 SQL 中提取涉及的表名（小写）"""
    try:
        parsed = sqlglot.parse_one(sql, dialect="mysql")
        if parsed is None:
            return set()
        return {t.name.lower() for t in parsed.find_all(exp.Table)}
    except Exception:
        return set()


def gold_static_check(gold_sql: str, valid_tables: set[str]) -> dict:
    """静态校验单条 gold SQL"""
    info = {"parse_ok": False, "tables_ok": False, "tables": []}
    try:
        parsed = sqlglot.parse_one(gold_sql, dialect="mysql")
        info["parse_ok"] = parsed is not None
    except Exception:
        info["parse_ok"] = False
    tables = extract_tables(gold_sql)
    info["tables"] = sorted(tables)
    info["tables_ok"] = all(t in valid_tables for t in tables)
    return info


# ============================================================
# 实时评估（调用 NL2SQL 管线）
# ============================================================

def _live_collect_sql(question: str, endpoint: str | None) -> dict:
    """获取一条问题生成的 SQL

    优先通过 HTTP 端点（若提供），否则尝试进程内 execute_query。
    返回 {"sql": str, "blocked": bool, "error": str|None, "source": str}
    """
    if endpoint:
        try:
            import httpx
            # 使用演示 admin 凭据获取 token
            resp = httpx.post(
                f"{endpoint}/api/v1/auth/login",
                json={"username": "admin", "password": "esim_admin_2026"},
                timeout=10,
            )
            token = resp.json().get("data", {}).get("access_token")
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            r = httpx.post(
                f"{endpoint}/api/v1/query",
                json={"question": question},
                headers=headers,
                timeout=120,
            )
            body = r.json()
            data = body.get("data", {})
            if body.get("code") == 1001:
                return {"sql": "", "blocked": True, "error": data.get("block_reason"), "source": "endpoint"}
            return {"sql": data.get("sql", ""), "blocked": False,
                    "error": data.get("error"), "source": "endpoint"}
        except Exception as e:
            return {"sql": "", "blocked": False, "error": str(e), "source": "endpoint"}

    # 进程内调用
    try:
        from app.core.vanna_instance import vanna_manager
        if not vanna_manager.is_initialized:
            return {"sql": "", "blocked": False, "error": "agent_not_initialized", "source": "inproc"}
        from app.services.query_service import execute_query
        result = __import__("asyncio").run(execute_query(question=question))
        return {"sql": result.sql, "blocked": result.blocked,
                "error": result.error, "source": "inproc"}
    except Exception as e:
        return {"sql": "", "blocked": False, "error": str(e), "source": "inproc"}


def run_live(test_set: dict, valid_tables: set[str], endpoint: str | None) -> dict:
    """实时模式：逐题调用管线并计算 EM / ExecAcc"""
    results = []
    for item in test_set["items"]:
        q = item["question"]
        gold = item["gold_sql"]
        gen = _live_collect_sql(q, endpoint)
        gen_sql = gen.get("sql", "") or ""
        norm_gen = normalize_sql(gen_sql)
        norm_gold = normalize_sql(gold)

        exact_match = (norm_gen == norm_gold) and bool(norm_gen)
        # 执行准确率：生成 SQL 非空、未被拦截、表集合 ⊆ 白名单 且与期望表一致
        gen_tables = extract_tables(gen_sql)
        tables_match = (gen_tables == set(t.lower() for t in item["expected_tables"]))
        exec_acc = bool(gen_sql) and not gen.get("blocked") and gen_tables.issubset(valid_tables) and tables_match

        results.append({
            "id": item["id"],
            "question": q,
            "category": item["category"],
            "difficulty": item["difficulty"],
            "gold_sql": gold,
            "gen_sql": gen_sql,
            "blocked": gen.get("blocked", False),
            "error": gen.get("error"),
            "exact_match": exact_match,
            "exec_acc": exec_acc,
        })
    return {"mode": "live", "results": results}


# ============================================================
# 指标聚合
# ============================================================

def aggregate(results: list[dict], key: str) -> dict:
    """按 key (category/difficulty) 聚合 EM 与 ExecAcc"""
    groups: dict[str, dict] = defaultdict(lambda: {"em": 0, "exec": 0, "total": 0})
    for r in results:
        g = groups[r[key]]
        g["total"] += 1
        if r.get("exact_match"):
            g["em"] += 1
        if r.get("exec_acc"):
            g["exec"] += 1
    out = {}
    for k, v in groups.items():
        out[k] = {
            "total": v["total"],
            "exact_match": v["em"],
            "exec_acc": v["exec"],
            "em_rate": round(v["em"] / v["total"], 4) if v["total"] else 0,
            "exec_rate": round(v["exec"] / v["total"], 4) if v["total"] else 0,
        }
    return out


def summarize(test_set: dict, valid_tables: set[str], live: bool, endpoint: str | None) -> dict:
    if live:
        live_data = run_live(test_set, valid_tables, endpoint)
        results = live_data["results"]
        overall = {
            "total": len(results),
            "exact_match": sum(1 for r in results if r["exact_match"]),
            "exec_acc": sum(1 for r in results if r["exec_acc"]),
            "blocked": sum(1 for r in results if r["blocked"]),
            "failed": sum(1 for r in results if r["error"] and not r["blocked"]),
        }
        overall["em_rate"] = round(overall["exact_match"] / overall["total"], 4) if overall["total"] else 0
        overall["exec_rate"] = round(overall["exec_acc"] / overall["total"], 4) if overall["total"] else 0
        return {
            "mode": "live",
            "overall": overall,
            "by_category": aggregate(results, "category"),
            "by_difficulty": aggregate(results, "difficulty"),
            "results": results,
        }

    # 静态模式
    per_item = [gold_static_check(it["gold_sql"], valid_tables) for it in test_set["items"]]
    total = len(per_item)
    parse_ok = sum(1 for p in per_item if p["parse_ok"])
    tables_ok = sum(1 for p in per_item if p["tables_ok"])
    invalid = [
        {"id": it["id"], "question": it["question"], "gold_sql": it["gold_sql"], "check": chk}
        for it, chk in zip(test_set["items"], per_item)
        if not (chk["parse_ok"] and chk["tables_ok"])
    ]
    return {
        "mode": "static",
        "total": total,
        "parse_ok": parse_ok,
        "tables_ok": tables_ok,
        "parse_rate": round(parse_ok / total, 4) if total else 0,
        "tables_rate": round(tables_ok / total, 4) if total else 0,
        "category_distribution": _category_dist(test_set),
        "difficulty_distribution": _difficulty_dist(test_set),
        "invalid_items": invalid,
    }


def _category_dist(test_set: dict) -> dict:
    d: dict[str, int] = defaultdict(int)
    for it in test_set["items"]:
        d[it["category"]] += 1
    return dict(d)


def _difficulty_dist(test_set: dict) -> dict:
    d: dict[str, int] = defaultdict(int)
    for it in test_set["items"]:
        d[it["difficulty"]] += 1
    return dict(d)


# ============================================================
# 入口
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="eSIM NL2SQL 评估脚本")
    parser.add_argument("--live", action="store_true", help="实时模式（需 Agent 就绪）")
    parser.add_argument("--endpoint", default=None, help="NL2SQL 服务地址（实时模式）")
    args = parser.parse_args()

    if not _TEST_SET.exists():
        print(f"[EVAL] 测试集不存在: {_TEST_SET}，请先运行 build_test_set.py", file=sys.stderr)
        sys.exit(1)

    test_set = json.loads(_TEST_SET.read_text(encoding="utf-8"))

    # 白名单表（来自 Schema Linking 配置）
    try:
        from app.core.schema_linking import TABLE_METADATA
        valid_tables = set(TABLE_METADATA.keys())
    except Exception:
        valid_tables = {
            "users", "plans", "orders", "operators", "esim_profiles",
            "data_usage", "roaming_packages",
        }

    t0 = time.perf_counter()
    report = summarize(test_set, valid_tables, live=args.live, endpoint=args.endpoint)
    elapsed = round(time.perf_counter() - t0, 2)

    report["meta"] = {
        "test_set_version": test_set.get("version"),
        "total_questions": test_set.get("total"),
        "valid_tables": sorted(valid_tables),
        "elapsed_seconds": elapsed,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    _REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # 控制台摘要
    print("=" * 60)
    print(f"eSIM NL2SQL 评估报告  (模式: {report['mode']})")
    print("=" * 60)
    if args.live:
        o = report["overall"]
        print(f"总题数:        {o['total']}")
        print(f"Exact Match:  {o['exact_match']}/{o['total']} = {o['em_rate']*100:.1f}%")
        print(f"Exec Accuracy:{o['exec_acc']}/{o['total']} = {o['exec_rate']*100:.1f}%")
        print(f"被拦截:        {o['blocked']}   失败: {o['failed']}")
        print("\n按类别:")
        for k, v in sorted(report["by_category"].items()):
            print(f"  {k:14s} EM={v['em_rate']*100:5.1f}%  Exec={v['exec_rate']*100:5.1f}%  (n={v['total']})")
        print("\n按难度:")
        for k, v in sorted(report["by_difficulty"].items()):
            print(f"  {k:14s} EM={v['em_rate']*100:5.1f}%  Exec={v['exec_rate']*100:5.1f}%  (n={v['total']})")
    else:
        print(f"总题数:           {report['total']}")
        print(f"语法合法率:       {report['parse_ok']}/{report['total']} = {report['parse_rate']*100:.1f}%")
        print(f"表白名单合规率:   {report['tables_ok']}/{report['total']} = {report['tables_rate']*100:.1f}%")
        print(f"类别分布:         {report['category_distribution']}")
        print(f"难度分布:         {report['difficulty_distribution']}")
        if report["invalid_items"]:
            print(f"\n⚠️ {len(report['invalid_items'])} 道题目 gold SQL 不合规:")
            for it in report["invalid_items"][:10]:
                print(f"  - {it['id']}: {it['question']}")
    print(f"\n报告已写入: {_REPORT}")
    print(f"耗时: {elapsed}s")


if __name__ == "__main__":
    main()
