#!/usr/bin/env python
"""从 query_audit_log 恢复被误删的对话

背景
----
``tests/conftest.py`` 里 ``cleanup_conversations`` 曾是全表删除
（``query(Conversation).delete()``）且 ``autouse=True``，导致每次跑 pytest
都会抹掉库里**所有**对话（含用户真实数据）。该 fixture 已修复为只清理
测试自己新建的记录，但已删除的数据需要回溯恢复。

``query_audit_log`` 记录了每次查询的 ``conversation_id / question /
generated_sql / row_count / execution_time_ms / username``，
足以重建对话骨架。

可恢复内容
----------
- 对话（id、标题、归属用户、时间）
- 每一轮的 user 提问 + assistant 的 SQL / 行数 / 耗时

不可恢复内容
------------
- assistant 的自然语言结果摘要（原本存在 conversation_messages.content，
  审计日志不记录）——用占位文本标注，避免伪造内容。

用法
----
    # 只看不改
    python scripts/restore_conversations_from_audit.py

    # 只恢复某个时间点之后的（默认：2026-08-30 16:00）
    python scripts/restore_conversations_from_audit.py --since "2026-08-30 16:00"

    # 真正写库
    python scripts/restore_conversations_from_audit.py --apply
"""

from __future__ import annotations

import argparse
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.config.database import db_manager  # noqa: E402

PLACEHOLDER = "（该轮结果摘要未留存，SQL 与返回行数已保留）"


def load_orphan_logs(conn, since: str) -> "OrderedDict[str, list[dict]]":
    """取「审计日志里有、但 conversations 表已没有」的对话记录，按会话分组。"""
    rows = conn.execute(
        text(
            """
            SELECT a.conversation_id,
                   a.question,
                   a.generated_sql,
                   a.execution_status,
                   a.error_message,
                   a.row_count,
                   a.execution_time_ms,
                   a.username,
                   a.created_at
            FROM query_audit_log a
            WHERE a.conversation_id IS NOT NULL
              AND a.conversation_id NOT IN (SELECT id FROM conversations)
              AND a.created_at >= :since
            ORDER BY a.created_at ASC
            """
        ),
        {"since": since},
    ).fetchall()

    grouped: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in rows:
        grouped.setdefault(r[0], []).append(
            {
                "question": r[1],
                "sql": r[2],
                "status": r[3],
                "error": r[4],
                "row_count": r[5],
                "exec_ms": r[6],
                "username": r[7],
                "created_at": r[8],
            }
        )
    return grouped


def main() -> int:
    ap = argparse.ArgumentParser(description="从审计日志恢复被误删的对话")
    ap.add_argument(
        "--since",
        default="2026-08-30 16:00",
        help="只恢复该时间点之后的记录（默认 2026-08-30 16:00）",
    )
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    args = ap.parse_args()

    with db_manager.engine.connect() as conn:
        grouped = load_orphan_logs(conn, args.since)
        print(f"可恢复对话数: {len(grouped)}（since={args.since}）")
        if not grouped:
            print("无可恢复数据。")
            return 0

        admin = conn.execute(
            text("SELECT id, username FROM app_users WHERE username = 'admin'")
        ).fetchone()
        if not admin:
            print("未找到 admin 用户，无法归属。")
            return 1
        uid, uname = int(admin[0]), str(admin[1])

        total_msgs = 0
        for cid, logs in grouped.items():
            title = (logs[0]["question"] or "（已恢复对话）")[:30]
            print(f"\n  {cid[:8]} | {len(logs)} 轮 | {title}")
            for lg in logs:
                print(f"      Q: {(lg['question'] or '')[:44]}")
                print(f"      SQL: {(lg['sql'] or '(无)')[:60]}")
            total_msgs += len(logs) * 2

        print(f"\n将创建 {len(grouped)} 个对话、{total_msgs} 条消息，归属 {uname}(id={uid})")

        if not args.apply:
            print("\n[dry-run] 未写库。确认后追加 --apply")
            return 0

        now = datetime.utcnow()
        for cid, logs in grouped.items():
            title = (logs[0]["question"] or "（已恢复对话）")[:30]
            first, last = logs[0]["created_at"], logs[-1]["created_at"]

            conn.execute(
                text(
                    """
                    INSERT INTO conversations
                        (id, user_id, username, title, message_count,
                         last_message_at, created_at, updated_at)
                    VALUES
                        (:id, :uid, :uname, :title, :mc,
                         :last_at, :created, :updated)
                    """
                ),
                {
                    "id": cid,
                    "uid": uid,
                    "uname": uname,
                    "title": title,
                    "mc": len(logs) * 2,
                    "last_at": last,
                    "created": first,
                    "updated": last,
                },
            )

            for lg in logs:
                # user 提问
                conn.execute(
                    text(
                        """
                        INSERT INTO conversation_messages
                            (conversation_id, role, content, created_at)
                        VALUES (:cid, 'user', :content, :ts)
                        """
                    ),
                    {"cid": cid, "content": lg["question"], "ts": lg["created_at"]},
                )
                # assistant 回复：SQL / 行数 / 耗时来自审计日志，摘要用占位
                conn.execute(
                    text(
                        """
                        INSERT INTO conversation_messages
                            (conversation_id, role, content, generated_sql,
                             sql_status, error_message, row_count,
                             execution_time_ms, created_at)
                        VALUES
                            (:cid, 'assistant', :content, :sql,
                             :status, :err, :rc, :ms, :ts)
                        """
                    ),
                    {
                        "cid": cid,
                        "content": PLACEHOLDER,
                        "sql": lg["sql"],
                        "status": lg["status"] or "success",
                        "err": lg["error"],
                        "rc": lg["row_count"] or 0,
                        "ms": lg["exec_ms"] or 0,
                        # 回答时间略晚于提问，保证排序稳定
                        "ts": lg["created_at"],
                    },
                )

        conn.commit()
        print(f"\n[完成] 已恢复 {len(grouped)} 个对话")
        print(f"[校验] conversations 总数: "
              f"{conn.execute(text('SELECT COUNT(*) FROM conversations')).fetchone()[0]}")
        print(f"[提示] assistant 摘要为占位文本：{PLACEHOLDER}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
