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


def _fmt_metric(stats: dict, key: str) -> str:
    """格式化指标：多轮输出 mean±std，单轮输出 mean"""
    s = stats[key]
    if len(s["values"]) > 1:
        return f"{s['mean']:.1f}±{s['std']:.1f}%"
    return f"{s['mean']:.1f}%"


def _summary_trials(name: str, agg: dict, stats: dict | None = None) -> None:
    if not stats:
        return _summary(name, agg)
    o = agg["overall"]
    print(f"  {name}: total={o['total']}  handled={_fmt_metric(stats, 'handled_rate')}  "
          f"EM={_fmt_metric(stats, 'em')}  EA={_fmt_metric(stats, 'ea')}  "
          f"self_corrected={_fmt_metric(stats, 'self_corrected')}")
    if stats["ea"]["values"] and len(stats["ea"]["values"]) > 1:
        print(f"      各轮 EA: {stats['ea']['values']}")
        print(f"      各轮 self_corrected: {stats['self_corrected']['values']}")


async def _run_trials(name: str, eval_fn, items: list[dict], limit: int,
                      trials: int, **kw) -> tuple:
    """多轮重复评估：同一评估集跑 N 轮，返回 (末轮 rows, 各轮 aggregate, 统计)

    统计指标（ea/em/self_corrected/handled_rate）输出 均值 ± 标准差，
    消除 LLM 随机性对单次评估的影响——这是严格 A/B 对照实验的基础。

    Args:
        name: 路线名（日志用）
        eval_fn: 评估函数（同步或异步均可）
        items: 评估集
        limit: 题数限制（0=全部）
        trials: 重复轮数（>=1）

    Returns:
        (rows_last, per_trial_aggregates, stats)
        stats = {key: {"values": [...], "mean": float, "std": float}}（单位 %）
    """
    import inspect
    import statistics

    per_trial = []
    rows_last = None
    for t in range(1, trials + 1):
        print(f"  [{name}] 第 {t}/{trials} 轮（LLM 随机性通过多轮取均值消除）...")
        result = eval_fn(items, limit, **kw)
        if inspect.isawaitable(result):
            result = await result
        rows = result
        agg = aggregate(rows)
        per_trial.append(agg)
        rows_last = rows

    stats = {}
    for key in ("ea", "em", "self_corrected", "handled_rate"):
        vals = [a["overall"][key] for a in per_trial]
        stats[key] = {
            "values": [round(v * 100, 1) for v in vals],
            "mean": round(statistics.mean(vals) * 100, 1),
            "std": round(statistics.stdev(vals) * 100, 1) if trials > 1 else 0.0,
        }
    return rows_last, per_trial, stats


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
    ap.add_argument("--trials", type=int, default=1,
                    help="多轮重复评估轮数（>1 时输出均值±标准差，消除 LLM 随机性）")
    args = ap.parse_args()

    items = json.load(open(TEST_SET, encoding="utf-8"))["items"]
    report: dict = {"meta": {"test_set_size": len(items), "trials": args.trials}}

    # 快捷模式
    if args.mini_only:
        args.mini, args.mini_limit = True, args.mini_only

    if args.trials < 1:
        raise SystemExit("--trials 必须 >= 1")

    print("=" * 66)
    print("三路对比：Vanna(完整 Agent) vs 自研 Mini Agent vs 纯 LLM 直出(Non-Agent)")
    if args.trials > 1:
        print(f"多轮重复评估（trials={args.trials}，输出均值±标准差）")
    print("=" * 66)

    if args.naive:
        rows, per_trial, stats = await _run_trials(
            "naive", evaluate_naive, items, args.naive_limit, args.trials)
        agg = per_trial[-1]
        report["naive"] = {
            "rows": rows, "aggregate": agg,
            "trials": {"n": args.trials, "stats": stats,
                       "per_trial_aggregates": per_trial},
        }
        _summary_trials("naive(纯 LLM 直出)", agg, stats)

    if args.mini:
        rows, per_trial, stats = await _run_trials(
            "mini", evaluate_mini, items, args.mini_limit, args.trials,
            use_rag=not args.no_rag)
        agg = per_trial[-1]
        report["mini"] = {
            "rows": rows, "aggregate": agg,
            "trials": {"n": args.trials, "stats": stats,
                       "per_trial_aggregates": per_trial},
        }
        name = "mini(自研 Agent, no-RAG)" if args.no_rag else "mini(自研 Agent, RAG)"
        _summary_trials(name, agg, stats)

    if args.vanna:
        vitems = items[:args.vanna_limit] if args.vanna_limit else items
        print(f"  [vanna] 实时评估（endpoint={args.endpoint}, {len(vitems)} 题）...")
        rows, per_trial, stats = await _run_trials(
            "vanna", evaluate_vanna, vitems, 0, args.trials, endpoint=args.endpoint)
        agg = per_trial[-1]
        report["vanna"] = {
            "rows": rows, "aggregate": agg,
            "trials": {"n": args.trials, "stats": stats,
                       "per_trial_aggregates": per_trial},
        }
        _summary_trials("vanna(完整 Agent)", agg, stats)

    # 结论对比（仅当至少两路存在；多轮时使用均值）
    verdict = {}
    for k in ("naive", "mini", "vanna"):
        if k in report:
            tr = report[k].get("trials", {}).get("stats")
            ea = tr["ea"]["mean"] / 100 if tr else report[k]["aggregate"]["overall"]["ea"]
            em = tr["em"]["mean"] / 100 if tr else report[k]["aggregate"]["overall"]["em"]
            sc = tr["self_corrected"]["mean"] / 100 if tr \
                else report[k]["aggregate"]["overall"]["self_corrected"]
            verdict[f"{k}_ea"] = ea
            verdict[f"{k}_em"] = em
            verdict[f"{k}_self_correction"] = sc
    if len(verdict) >= 4:
        report["verdict"] = verdict
        print("\n[结论]（多轮均值）" if args.trials > 1 else "\n[结论]")
        for k, v in verdict.items():
            print(f"  {k} = {v*100:.1f}%")

    json.dump(report, open(REPORT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n报告已写入: {REPORT}")


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
