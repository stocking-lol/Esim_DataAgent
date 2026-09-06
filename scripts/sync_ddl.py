"""
DDL 同步命令行工具
------------------
检测并把 MySQL 真实表结构同步到 ChromaDB 训练库。

用法::

    # 只检测，不修改（默认，安全）
    python scripts/sync_ddl.py

    # 实际同步缺失/漂移的 DDL
    python scripts/sync_ddl.py --apply

    # 同步并删除多余的 DDL（MySQL 已无 / 不在白名单）
    python scripts/sync_ddl.py --apply --drop-orphans

    # 机器可读输出（供监控/告警消费）
    python scripts/sync_ddl.py --json

退出码::

    0  无漂移，或同步成功
    1  存在漂移（dry-run 模式下用于告警）
    2  执行出错
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main() -> int:
    parser = argparse.ArgumentParser(description="DDL 漂移检测与同步")
    parser.add_argument(
        "--apply", action="store_true",
        help="实际写入 ChromaDB（默认只检测不改）",
    )
    parser.add_argument(
        "--drop-orphans", action="store_true",
        help="删除多余的 DDL 记录（需配合 --apply）",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="以 JSON 输出，便于程序消费",
    )
    parser.add_argument(
        "--table", action="append", default=None,
        help="只检测指定表（可重复指定），默认用安全策略的白名单",
    )
    args = parser.parse_args()

    from app.services.ddl_sync_service import DDLSyncService  # noqa: PLC0415

    svc = DDLSyncService(allowed_tables=args.table)
    report = await svc.detect()

    if args.apply:
        await svc.apply(report, drop_orphans=args.drop_orphans)

    if args.as_json:
        print(json.dumps(
            {
                "checked": len(report.drifts),
                "missing": [d.table for d in report.missing],
                "drifted": [d.table for d in report.drifted],
                "orphaned": [d.table for d in report.orphaned],
                "applied": report.applied,
                "failed": [{"table": t, "error": e} for t, e in report.failed],
                "has_drift": report.has_drift,
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print()
        print(report.to_text())
        print()

    if report.failed:
        return 2
    # dry-run 下发现漂移返回 1，便于定时任务/告警据此判断
    if not args.apply and report.has_drift:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
