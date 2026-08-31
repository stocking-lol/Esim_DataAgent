#!/usr/bin/env python
"""修复孤儿对话（user_id IS NULL）

背景
----
早期实现中 ``get_optional_user`` 会吞掉 JWT 校验抛出的 401
（``except HTTPException: return None``），于是 token 过期后：

1. 创建对话接口拿到 user=None → 写入 ``user_id = NULL``
2. 列表接口按 ``user_id == 当前用户`` 过滤 → 这些对话永远查不到
3. 接口仍返回 HTTP 200，前端无感知 → 表现为「对话历史凭空消失」

本脚本用于**回溯修复**已产生的孤儿数据（根因修复见 app/core/auth.py）。

用法
----
    # 1) 只看不改（默认）
    python scripts/fix_orphan_conversations.py

    # 2) 归位到指定用户（按用户名）
    python scripts/fix_orphan_conversations.py --assign-to admin --apply

    # 3) 归位到指定用户（按 user_id）
    python scripts/fix_orphan_conversations.py --assign-to-id 1 --apply

安全特性
--------
- 默认 dry-run，必须显式 ``--apply`` 才写库
- 写库前自动备份到 ``conversation_orphan_backup_<时间戳>`` 表
- 目标用户必须真实存在，否则报错退出
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.config.database import db_manager  # noqa: E402


def fetch_orphans(conn) -> list[dict]:
    rows = conn.execute(
        text(
            """
            SELECT id, title, username, message_count, created_at, updated_at
            FROM conversations
            WHERE user_id IS NULL
            ORDER BY updated_at DESC
            """
        )
    ).fetchall()
    return [
        {
            "id": r[0],
            "title": r[1],
            "username": r[2],
            "message_count": r[3],
            "created_at": r[4],
            "updated_at": r[5],
        }
        for r in rows
    ]


def resolve_user(conn, username: str | None, user_id: int | None) -> tuple[int, str]:
    """解析目标用户，返回 (user_id, username)。用户不存在则抛错。"""
    if user_id is not None:
        row = conn.execute(
            text("SELECT id, username FROM app_users WHERE id = :i"),
            {"i": user_id},
        ).fetchone()
    else:
        row = conn.execute(
            text("SELECT id, username FROM app_users WHERE username = :u"),
            {"u": username},
        ).fetchone()
    if not row:
        raise SystemExit(f"目标用户不存在：{username or user_id}")
    return int(row[0]), str(row[1])


def backup(conn, orphans: list[dict]) -> str:
    """把待修改的对话备份到新表，返回表名。"""
    table = "conversation_orphan_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    conn.execute(
        text(
            f"""
            CREATE TABLE {table} AS
            SELECT * FROM conversations WHERE user_id IS NULL
            """
        )
    )
    print(f"[备份] 已创建 {table}（{len(orphans)} 行）")
    return table


def main() -> int:
    ap = argparse.ArgumentParser(description="修复 user_id IS NULL 的孤儿对话")
    ap.add_argument("--assign-to", help="归位到的用户名（如 admin）")
    ap.add_argument("--assign-to-id", type=int, help="归位到的 user_id")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="真正写库；不加则只打印计划（dry-run）",
    )
    args = ap.parse_args()

    if args.apply and not (args.assign_to or args.assign_to_id):
        ap.error("--apply 必须配合 --assign-to 或 --assign-to-id")

    with db_manager.engine.connect() as conn:
        orphans = fetch_orphans(conn)
        total = conn.execute(text("SELECT COUNT(*) FROM conversations")).fetchone()[0]

        print(f"对话总数: {total} | 孤儿（user_id IS NULL）: {len(orphans)}")
        if not orphans:
            print("无需修复。")
            return 0

        print("\n孤儿对话清单:")
        for o in orphans:
            print(
                f"  {o['id'][:8]} | 消息 {o['message_count']:>2} 条 | "
                f"{(o['title'] or '(无标题)')[:36]:<36} | 更新于 {o['updated_at']}"
            )

        if not args.apply:
            target = args.assign_to or args.assign_to_id
            print(
                f"\n[dry-run] 未做任何修改。确认无误后执行：\n"
                f"  python scripts/fix_orphan_conversations.py "
                f"--assign-to {target or 'admin'} --apply"
            )
            return 0

        uid, uname = resolve_user(conn, args.assign_to, args.assign_to_id)
        print(f"\n目标用户: {uname} (id={uid})")

        backup(conn, orphans)

        result = conn.execute(
            text(
                """
                UPDATE conversations
                SET user_id = :uid, username = :uname
                WHERE user_id IS NULL
                """
            ),
            {"uid": uid, "uname": uname},
        )
        conn.commit()
        print(f"[完成] 已将 {result.rowcount} 条孤儿对话归位到 {uname} (id={uid})")

        # 校验
        left = conn.execute(
            text("SELECT COUNT(*) FROM conversations WHERE user_id IS NULL")
        ).fetchone()[0]
        print(f"[校验] 剩余孤儿: {left}")
        return 0 if left == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
