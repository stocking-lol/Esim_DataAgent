"""
eSIM NL2SQL 平台 —— 演示用例运行器
==================================

以可复现的方式展示平台能力：基础 NL2SQL（7 类查询模式）、安全防护
（SQL/Prompt/UNION 注入拦截）、行级租户隔离（RLS）、数据脱敏、自我纠错、
可视化，以及与「纯规则基线引擎」的对比。

两种运行模式：
  1) 静态展示（默认，离线、零 API 调用）
     直接打印演示集（scripts/eval/demo_set.json），并对 basic_nl2sql 类用例
     调用本地规则基线引擎，直观对比「规则基线能/不能做什么」。
  2) 实时连线演示（--live）
     向运行中的后端 POST /api/v1/query，打印平台真实生成的 SQL / 拦截原因 /
     脱敏 / 图表等结果。攻击类用例会演示被安全网关以 code=1001 拦截。

用法示例：
    python scripts/demo.py                         # 静态展示全部
    python scripts/demo.py --feature security      # 只看安全攻防
    python scripts/demo.py --live                  # 连线 http://localhost:8000
    python scripts/demo.py --live --endpoint http://localhost:8000 --token <JWT>
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

# 允许以脚本方式直接运行（scripts/ 下能 import 到 app 包）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.core.nl2sql_baseline import baseline_nl2sql  # noqa: E402

_DEMO_PATH = Path(__file__).resolve().parent / "eval" / "demo_set.json"

# 终端颜色（静态展示用）
C = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "blue": "\033[34m",
}


def _c(key: str, text: str) -> str:
    """仅在支持 ANSI 的终端上着色；管道/文件输出自动降级。"""
    if not sys.stdout.isatty():
        return text
    return f"{C.get(key, '')}{text}{C['reset']}"


def load_demos(feature: str | None = None) -> list[dict]:
    with open(_DEMO_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", [])
    if feature:
        items = [d for d in items if d.get("feature") == feature]
    return items


def run_static(items: list[dict]) -> None:
    print(_c("bold", "\n=== eSIM NL2SQL 平台 · 演示用例（静态展示 / 离线）==="))
    print(_c("dim", "说明：基础查询类会同时调用本地『规则基线引擎』，对比 Vanna(LLM) 的差异。\n"))

    for d in items:
        feat = d.get("feature", "?")
        title = d.get("title", "")
        q = d.get("question", "")
        gold = d.get("gold_sql", "")
        expected = d.get("expected", "")
        attack = d.get("attack", False)

        print(_c("blue", f"[{d.get('id')}] {title}  ") + _c("dim", f"({feat}/{d.get('category')})"))
        print(f"  问：{_c('bold', q)}")

        if attack:
            print(f"  预期：{_c('red', '被安全网关拦截 (code=1001, fail-closed)')}")
            print()
            continue

        if gold:
            print(f"  参考SQL：{_c('green', gold)}")

        # 对基础查询类，调用规则基线引擎做对比演示
        if feat == "basic_nl2sql":
            try:
                base = baseline_nl2sql.generate_sql(q)
                if base.handled:
                    mark = _c("green", "命中")
                    print(f"  规则基线：{mark} → {base.sql}")
                else:
                    mark = _c("red", "无法处理")
                    print(f"  规则基线：{mark}（{base.note}）")
            except Exception as e:  # pragma: no cover
                print(f"  规则基线：{_c('yellow', '引擎异常')} {e}")

        print(f"  预期：{expected}")
        print()

    print(_c("dim", "提示：运行 `python scripts/demo.py --live` 可连线真实后端查看 Vanna 生成的 SQL / 脱敏 / 图表。"))


def _post_query(endpoint: str, question: str, token: str | None) -> dict:
    url = endpoint.rstrip("/") + "/api/v1/query"
    payload = json.dumps({"question": question}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return json.loads(body)
        except Exception:
            return {"code": e.code, "message": body}
    except Exception as e:  # pragma: no cover
        return {"code": -1, "message": f"网络异常: {e}"}


def run_live(items: list[dict], endpoint: str, token: str | None) -> None:
    print(_c("bold", f"\n=== eSIM NL2SQL 平台 · 实时演示（{endpoint}）===\n"))
    for d in items:
        q = d.get("question", "")
        print(_c("blue", f"[{d.get('id')}] {d.get('title')} ") + _c("dim", f"({d.get('feature')})"))
        print(f"  问：{_c('bold', q)}")

        resp = _post_query(endpoint, q, token)
        code = resp.get("code")
        data = resp.get("data", {}) or {}

        if code == 1001 or data.get("blocked"):
            print(f"  {_c('red', '安全拦截')} (code={code}): {data.get('block_reason') or resp.get('message')}")
        elif code and code >= 400:
            print(f"  {_c('yellow', '服务返回')} (code={code}): {resp.get('message')}")
        else:
            sql = data.get("sql", "")
            rows = data.get("row_count", 0)
            masked = data.get("masked_columns") or []
            chart = data.get("chart") or {}
            retry = data.get("retry_count", 0)
            print(f"  {_c('green', '生成SQL')}：{sql}")
            print(f"  结果：{rows} 行 | 脱敏列：{masked or '无'} | 重试：{retry} 次 | 图表：{chart.get('type', '无')}")

        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="eSIM NL2SQL 平台演示用例运行器")
    parser.add_argument("--mode", choices=["static", "live"], default="static",
                        help="static=离线展示(默认); live=连线实时演示")
    parser.add_argument("--feature", default=None,
                        help="仅展示指定 feature（basic_nl2sql/security/rls/masking/self_correction/visualization/baseline_vs_vanna）")
    parser.add_argument("--endpoint", default="http://localhost:8000",
                        help="live 模式下的后端地址（默认 http://localhost:8000）")
    parser.add_argument("--token", default=None, help="live 模式下的 JWT（可选，用于触发 RLS/脱敏角色）")
    args = parser.parse_args()

    items = load_demos(args.feature)
    if not items:
        print(f"未找到匹配 feature={args.feature} 的演示用例。")
        return 1

    if args.mode == "live":
        run_live(items, args.endpoint, args.token)
    else:
        run_static(items)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
