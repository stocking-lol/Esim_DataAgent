"""
迁移脚本：为 query_audit_log 表补充缺失列
-----------------------------------------
Day 12 引入了 conversation_id 与 security_blocked 两个字段（代码层），
但早期 init_db.sql 创建的表未包含这两列，导致审计日志写入静默失败。
本脚本检测并补齐这两个列，保证代码与数据库 schema 一致。

用法：
    python scripts/migrate_audit_columns.py
"""

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中（脚本从 scripts/ 运行时需要）
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sqlalchemy import inspect, text

from app.config.database import db_manager, get_raw_db


def migrate() -> None:
    engine = db_manager._engine
    with engine.connect() as conn:
        inspector = inspect(engine)
        existing = {c["name"] for c in inspector.get_columns("query_audit_log")}
        print(f"[MIGRATE] 现有列: {sorted(existing)}")

        needed = {
            "conversation_id": "VARCHAR(36) DEFAULT NULL COMMENT '对话ID'",
            "security_blocked": "INT NOT NULL DEFAULT 0 COMMENT '是否被安全网关拦截: 0=否, 1=是'",
        }

        applied = []
        for col, ddl in needed.items():
            if col not in existing:
                sql = f"ALTER TABLE query_audit_log ADD COLUMN {col} {ddl}"
                conn.execute(text(sql))
                conn.commit()
                applied.append(col)
                print(f"[MIGRATE] 已添加列: {col}")
            else:
                print(f"[MIGRATE] 列已存在，跳过: {col}")

        if not applied:
            print("[MIGRATE] 无需迁移，schema 已是最新。")
        else:
            # 验证
            inspector2 = inspect(engine)
            after = {c["name"] for c in inspector2.get_columns("query_audit_log")}
            print(f"[MIGRATE] 迁移后列: {sorted(after)}")


if __name__ == "__main__":
    try:
        migrate()
        print("[MIGRATE] 完成。")
    except Exception as e:
        print(f"[MIGRATE] 失败: {e}", file=sys.stderr)
        sys.exit(1)
