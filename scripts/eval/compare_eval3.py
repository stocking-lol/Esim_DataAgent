"""
三路对比评估：Vanna（完整 Agent） vs 自研 Mini Agent vs 纯 LLM 直出（非 Agent）
================================================================================

技术选型论证的完整实验：同一套 54 题评估集、同一 LLM（DeepSeek-V3），
仅改变「架构形态」，量化 Agent 化（检索 + 工具 + 自愈）带来的增量价值。

三路定义：
  - naive    : 纯 LLM 直出（Non-Agent）—— 单次调用、全量 schema 硬塞、
               无检索、无工具、无自愈。代表业界最常见做法。
  - mini     : 自研 Mini Agent Runtime —— RAG 检索 + 工具执行(dry_run) +
               错误反馈自愈循环。本项目从零实现的 Agent 底座。
  - vanna    : Vanna 2.0 完整 Agent —— 生产底座（--live 连服务）。

指标：Coverage / EM / EA / 自我纠错（retries>0 占比 + 重试成功率）。

用法：
  python scripts/eval/compare_eval3.py --naive-limit 20 --mini-limit 20
  python scripts/eval/compare_eval3.py --naive --mini --vanna --endpoint http://localhost:8000
  python scripts/eval/compare_eval3.py --mini-only 5        # 只跑 Mini Agent 前 5 题
"""

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.eval.compare_eval import aggregate, exec_accuracy, evaluate_baseline  # noqa: E402
from scripts.eval.run_eval import normalize_sql  # noqa: E402

HERE = Path(__file__).resolve().parent
TEST_SET = HERE / "test_set.json"
REPORT = HERE / "compare_report3.json"


# ============================================================
# 路线一：纯 LLM 直出（Non-Agent）
# ============================================================

async def evaluate_naive(items: list[dict], limit: int) -> list[dict]:
    from app.core.mini_agent.naive import NaiveNL2SQL

    naive = NaiveNL2SQL()
    rows = []
    subset = items[:limit] if limit else items
    print(f"  [naive] 纯 LLM 直出评估（{len(subset)} 题，无检索/无工具/无自愈）...")
    for item in subset:
        q, gold = item["question"], item["gold_sql"]
        sql = await naive.generate(q)
        norm = normalize_sql(sql)
        rows.append({
            "id": item["id"], "category": item["category"],
            "difficulty": item["difficulty"], "question": q, "gold_sql": gold,
            "gen_sql": sql, "handled": bool(norm),
            "exact_match": bool(norm) and norm == normalize_sql(gold),
            "exec_acc": exec_accuracy(sql, item, blocked=False),
            "retries": 0, "self_corrected": False,
        })
        print(f"    {item['id']} {'OK' if rows[-1]['handled'] else '--'}")
    return rows


# ============================================================
# 路线二：自研 Mini Agent
# ============================================================

async def evaluate_mini(items: list[dict], limit: int, use_rag: bool = True) -> list[dict]:
    from app.core.chroma_store import chroma_store
    from app.core.mini_agent.runtime import build_default_runtime

    # 初始化 RAG 向量库（评估进程独立运行，需手动初始化；失败则降级为无 RAG）
    if use_rag:
        try:
            await chroma_store.initialize()
        except Exception as e:
            print(f"  [mini] 警告: ChromaDB 初始化失败，降级为无 RAG: {e}")
            use_rag = False

    runtime = build_default_runtime(dry_run=True, use_rag=use_rag)
    rows = []
    subset = items[:limit] if limit else items
    tag = "RAG" if use_rag else "no-RAG"
    print(f"  [mini:{tag}] 自研 Mini Agent 评估（{len(subset)} 题，dry_run 模拟执行）...")
    for item in subset:
        q, gold = item["question"], item["gold_sql"]
        resp = await runtime.ask(q)
        sql = resp.sql
        norm = normalize_sql(sql)
        rows.append({
            "id": item["id"], "category": item["category"],
            "difficulty": item["difficulty"], "question": q, "gold_sql": gold,
            "gen_sql": sql, "handled": resp.success,
            "exact_match": bool(norm) and norm == normalize_sql(gold),
            "exec_acc": exec_accuracy(sql, item, blocked=resp.blocked),
            "retries": resp.retries,
            "self_corrected": resp.retries > 0 and resp.success,
            "context_len": resp.context_len,
            "blocked": resp.blocked,
            "block_reason": resp.block_reason,
        })
        print(f"    {item['id']} {'OK' if resp.success else '--'}"
              f" (retries={resp.retries})")
    return rows


# ============================================================
# 路线三：Vanna（复用 compare_eval 的实时评估）
# ============================================================

def evaluate_vanna(items: list[dict], endpoint: str) -> list[dict]:
    from scripts.eval.compare_eval import evaluate_vanna as ev
    return ev(items, endpoint)


# ============================================================
# 入口
# ============================================================

def _summary(name: str, agg: dict) -> None:
    o = agg["overall"]
    print(f"  {name}: total={o['total']}  handled={o['handled_rate']*100:.1f}%  "
          f"EM={o['em']*100:.1f}%  EA={o['ea']*100:.1f}%  "
          f"self_corrected={o['self_corrected']*100:.1f}%")


async def _amain() -> None:
    ap = argparse.ArgumentParser(description="三路对比评估（naive / mini / vanna）")
    ap.add_argument("--naive", action="store_true", help="跑纯 LLM 直出路线")
    ap.add_argument("--mini", action="store_true", help="跑自研 Mini Agent 路线")
    ap.add_argument("--vanna", action="store_true", help="跑 Vanna 路线（需服务）")
    ap.add_argument("--endpoint", default="http://localhost:8000")
    ap.add_argument("--naive-limit", type=int, default=0, help="naive 题数（0=全部）")
    ap.add_argument("--mini-limit", type=int, default=0, help="mini 题数（0=全部）")
    ap.add_argument("--mini-only", type=int, default=0, help="仅 mini 前 N 题（快捷验证）")
    ap.add_argument("--no-rag", action="store_true", help="Mini Agent 关闭 RAG（对照组）")
    ap.add_argument("--vanna-limit", type=int, default=0, help="vanna 题数（0=全部）")
    args = ap.parse_args()

    items = json.load(open(TEST_SET, encoding="utf-8"))["items"]
    report: dict = {"meta": {"test_set_size": len(items)}}

    # 快捷模式
    if args.mini_only:
        args.mini, args.mini_limit = True, args.mini_only

    print("=" * 66)
    print("三路对比：Vanna(完整 Agent) vs 自研 Mini Agent vs 纯 LLM 直出(Non-Agent)")
    print("=" * 66)

    if args.naive:
        rows = await evaluate_naive(items, args.naive_limit)
        agg = aggregate(rows)
        report["naive"] = {"rows": rows, "aggregate": agg}
        _summary("naive(纯 LLM 直出)", agg)

    if args.mini:
        rows = await evaluate_mini(items, args.mini_limit, use_rag=not args.no_rag)
        agg = aggregate(rows)
        report["mini"] = {"rows": rows, "aggregate": agg}
        name = "mini(自研 Agent, no-RAG)" if args.no_rag else "mini(自研 Agent, RAG)"
        _summary(name, agg)

    if args.vanna:
        vitems = items[:args.vanna_limit] if args.vanna_limit else items
        print(f"  [vanna] 实时评估（endpoint={args.endpoint}, {len(vitems)} 题）...")
        rows = evaluate_vanna(vitems, args.endpoint)
        agg = aggregate(rows)
        report["vanna"] = {"rows": rows, "aggregate": agg}
        _summary("vanna(完整 Agent)", agg)

    # 结论对比（仅当至少两路存在）
    verdict = {}
    for k in ("naive", "mini", "vanna"):
        if k in report:
            verdict[f"{k}_ea"] = report[k]["aggregate"]["overall"]["ea"]
            verdict[f"{k}_em"] = report[k]["aggregate"]["overall"]["em"]
            verdict[f"{k}_self_correction"] = \
                report[k]["aggregate"]["overall"]["self_corrected"]
    if len(verdict) >= 4:
        report["verdict"] = verdict
        print("\n[结论]")
        for k, v in verdict.items():
            print(f"  {k} = {v*100:.1f}%")

    json.dump(report, open(REPORT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n报告已写入: {REPORT}")


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
