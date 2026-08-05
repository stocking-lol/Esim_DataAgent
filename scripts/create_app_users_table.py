"""
创建 app_users 表并预置初始用户
--------------------------------
- 创建 app_users 表（如不存在）
- 预置 3 个用户: admin / analyst / viewer
- 密码使用 bcrypt 哈希存储
"""

import sys
from pathlib import Path

# 确保项目根目录在 PYTHONPATH 中
project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import pymysql

from app.config.settings import settings
from app.utils.crypto import hash_password

# ---- 配置 ----
DB_HOST = settings.DATABASE_HOST
DB_PORT = settings.DATABASE_PORT
DB_USER = settings.DATABASE_USER
DB_PASSWORD = settings.DATABASE_PASSWORD
DB_NAME = settings.DATABASE_NAME

# 预置用户
SEED_USERS = [
    {
        "username": "admin",
        "email": "admin@esim-platform.local",
        "password": "esim_admin_2026",
        "role": "admin",
        "mvno_id": None,
    },
    {
        "username": "analyst",
        "email": "analyst@esim-platform.local",
        "password": "esim_analyst_2026",
        "role": "analyst",
        "mvno_id": 1,
    },
    {
        "username": "viewer",
        "email": "viewer@esim-platform.local",
        "password": "esim_viewer_2026",
        "role": "viewer",
        "mvno_id": 2,
    },
]


def create_table(cursor):
    """创建 app_users 表"""
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS app_users (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        username        VARCHAR(100)    NOT NULL UNIQUE COMMENT '用户名（唯一）',
        email           VARCHAR(255)    NOT NULL UNIQUE COMMENT '邮箱（唯一）',
        hashed_password VARCHAR(255)    NOT NULL COMMENT 'bcrypt 哈希密码',
        role            VARCHAR(20)     NOT NULL DEFAULT 'analyst' COMMENT '角色: admin/analyst/viewer',
        mvno_id         INT             DEFAULT NULL COMMENT '关联 MVNO ID（用于 RLS）',
        is_active       TINYINT(1)      NOT NULL DEFAULT 1 COMMENT '是否激活',
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
        updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
        INDEX idx_app_users_username (username),
        INDEX idx_app_users_email (email),
        INDEX idx_app_users_role (role),
        INDEX idx_app_users_mvno_id (mvno_id),
        INDEX idx_app_users_is_active (is_active)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='平台用户表'
    """)
    print("[OK] 表 app_users 创建成功（或已存在）")


def seed_users(cursor):
    """预置初始用户"""
    for user_data in SEED_USERS:
        # 检查用户是否已存在
        cursor.execute(
            "SELECT id FROM app_users WHERE username = %s", (user_data["username"],)
        )
        existing = cursor.fetchone()

        if existing:
            print(f"[SKIP] 用户 '{user_data['username']}' 已存在 (id={existing[0]})，跳过")
            continue

        hashed = hash_password(user_data["password"])
        cursor.execute(
            """
            INSERT INTO app_users (username, email, hashed_password, role, mvno_id, is_active)
            VALUES (%s, %s, %s, %s, %s, 1)
            """,
            (
                user_data["username"],
                user_data["email"],
                hashed,
                user_data["role"],
                user_data["mvno_id"],
            ),
        )
        print(
            f"[OK] 用户 '{user_data['username']}' 创建成功 "
            f"(role={user_data['role']}, mvno_id={user_data['mvno_id']})"
        )


def main():
    """主函数"""
    print("=" * 60)
    print("创建 app_users 表并预置初始用户")
    print("=" * 60)

    conn = pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
    )
    cursor = conn.cursor()

    try:
        create_table(cursor)
        conn.commit()

        seed_users(cursor)
        conn.commit()

        # 验证结果
        print("\n--- 当前 app_users 表内容 ---")
        cursor.execute(
            "SELECT id, username, email, role, mvno_id, is_active, created_at FROM app_users ORDER BY id"
        )
        for row in cursor.fetchall():
            print(
                f"  id={row[0]}, username={row[1]}, email={row[2]}, "
                f"role={row[3]}, mvno_id={row[4]}, is_active={row[5]}, created_at={row[6]}"
            )

        print("\n[DONE] 所有操作完成")
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] 操作失败: {e}")
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()
