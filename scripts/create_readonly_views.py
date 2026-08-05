#!/usr/bin/env python
"""
create_readonly_views.py
------------------------
创建只读视图和只读 MySQL 用户。

连接 MySQL（root），执行 scripts/add_readonly_views.sql 中的 SQL，
逐条打印每条语句的执行结果。

用法:
    cd G:/Esim_DataAgent/esim-nl2sql-platform
    PYTHONPATH=. "E:/anaconda/envs/esim-nl2sql/python.exe" scripts/create_readonly_views.py
"""

import sys
import os
from pathlib import Path

import pymysql

# --- 从 settings 读取配置 ---
from app.config.settings import settings

DB_HOST = settings.DATABASE_HOST
DB_PORT = settings.DATABASE_PORT
DB_USER = settings.DATABASE_USER
DB_PASSWORD = settings.DATABASE_PASSWORD
DB_NAME = settings.DATABASE_NAME

SQL_FILE = Path(__file__).resolve().parent / "add_readonly_views.sql"

# 需要创建的视图列表（用于逐个验证）
VIEWS = [
    "v_users",
    "v_plans",
    "v_orders",
    "v_esim_profiles",
    "v_data_usage",
    "v_operators",
    "v_roaming_packages",
]


def split_sql_statements(sql_text: str) -> list[str]:
    """将 SQL 文本按分号拆分为独立语句，忽略字符串内的分号。"""
    statements = []
    current = []
    in_single_quote = False
    in_double_quote = False
    i = 0
    while i < len(sql_text):
        ch = sql_text[i]
        # 处理转义
        if ch == "\\" and i + 1 < len(sql_text):
            current.append(ch)
            current.append(sql_text[i + 1])
            i += 2
            continue
        if ch == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        if ch == ";" and not in_single_quote and not in_double_quote:
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
        else:
            current.append(ch)
        i += 1
    # 尾部
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def main():
    print("=" * 60)
    print("eSIM NL2SQL Platform - 创建只读视图")
    print("=" * 60)

    # 读取 SQL 文件
    if not SQL_FILE.exists():
        print(f"[FAIL] SQL 文件不存在: {SQL_FILE}")
        sys.exit(1)

    sql_text = SQL_FILE.read_text(encoding="utf-8")
    statements = split_sql_statements(sql_text)
    print(f"共 {len(statements)} 条 SQL 语句\n")

    # 连接 MySQL
    try:
        conn = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            charset="utf8mb4",
            autocommit=True,
        )
    except Exception as e:
        print(f"[FAIL] 无法连接 MySQL: {e}")
        sys.exit(1)

    print(f"[OK] 已连接 MySQL {DB_HOST}:{DB_PORT}/{DB_NAME}\n")

    success_count = 0
    fail_count = 0
    cursor = conn.cursor()

    for idx, stmt in enumerate(statements, 1):
        # 取语句前 80 字符作为预览
        preview = stmt.replace("\n", " ")[:80]
        # 跳过空语句和纯注释
        upper = stmt.upper().lstrip()
        if upper.startswith("--"):
            continue

        # 确定语句类型用于日志
        if upper.startswith("DROP VIEW"):
            label = "DROP VIEW"
        elif upper.startswith("CREATE OR REPLACE VIEW"):
            # 提取视图名
            parts = stmt.split()
            try:
                view_idx = parts.index("VIEW") + 1
                view_name = parts[view_idx] if view_idx < len(parts) else "?"
                label = f"CREATE VIEW {view_name}"
            except (ValueError, IndexError):
                label = "CREATE VIEW"
        elif upper.startswith("CREATE USER"):
            label = "CREATE USER"
        elif upper.startswith("GRANT"):
            label = "GRANT"
        elif upper.startswith("FLUSH"):
            label = "FLUSH"
        elif upper.startswith("USE"):
            label = "USE"
        else:
            label = "SQL"

        try:
            cursor.execute(stmt)
            success_count += 1
            print(f"  [{idx:3d}] [OK] {label}")
        except Exception as e:
            fail_count += 1
            print(f"  [{idx:3d}] [FAIL] {label}: {e}")

    cursor.close()

    # 验证视图
    print("\n" + "-" * 60)
    print("验证视图:")
    print("-" * 60)
    cursor = conn.cursor()
    for view_name in VIEWS:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {view_name}")
            count = cursor.fetchone()[0]
            print(f"  [OK] {view_name:25s} -> {count} 行")
        except Exception as e:
            print(f"  [FAIL] {view_name:25s} -> {e}")
    cursor.close()

    # 验证只读用户
    print("\n" + "-" * 60)
    print("验证只读用户 esim_readonly:")
    print("-" * 60)
    try:
        ro_conn = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user="esim_readonly",
            password="esim_readonly_2026",
            database=DB_NAME,
            charset="utf8mb4",
        )
        ro_cursor = ro_conn.cursor()
        ro_cursor.execute("SELECT COUNT(*) FROM v_users")
        count = ro_cursor.fetchone()[0]
        print(f"  [OK] esim_readonly 可查询 v_users -> {count} 行")

        # 验证无法访问基表
        try:
            ro_cursor.execute("SELECT COUNT(*) FROM users")
            print(f"  [WARN] esim_readonly 可以访问基表 users（应当被拒绝）")
        except pymysql.err.MySQLError as e:
            print(f"  [OK] esim_readonly 被拒绝访问基表 users: {e.args[0]}")

        ro_cursor.close()
        ro_conn.close()
    except Exception as e:
        print(f"  [FAIL] 无法用 esim_readonly 连接: {e}")

    conn.close()

    print("\n" + "=" * 60)
    print(f"完成: {success_count} 成功, {fail_count} 失败")
    print("=" * 60)

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
