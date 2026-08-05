"""创建对话管理表"""
import pymysql

conn = pymysql.connect(
    host='localhost', user='root', password='oyyx20050402',
    database='esim_platform', charset='utf8mb4',
)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS conversations (
    id              VARCHAR(36)     PRIMARY KEY,
    user_id         INT             DEFAULT NULL,
    username        VARCHAR(100)    DEFAULT NULL,
    title           VARCHAR(200)    DEFAULT NULL,
    message_count   INT             NOT NULL DEFAULT 0,
    last_message_at DATETIME        DEFAULT NULL,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_user_id (user_id),
    INDEX idx_created_at (created_at),
    INDEX idx_last_message (last_message_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS conversation_messages (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    conversation_id VARCHAR(36)     NOT NULL,
    role            ENUM('user','assistant','system') NOT NULL,
    content         TEXT            NOT NULL,
    generated_sql   TEXT            DEFAULT NULL,
    sql_status      VARCHAR(20)     DEFAULT NULL,
    row_count       INT             DEFAULT NULL,
    execution_time_ms INT           DEFAULT NULL,
    error_message   TEXT            DEFAULT NULL,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_conversation_id (conversation_id),
    INDEX idx_role (role),
    INDEX idx_created_at (created_at),
    CONSTRAINT fk_msg_conversation FOREIGN KEY (conversation_id)
        REFERENCES conversations(id) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
""")

conn.commit()
cursor.execute("SHOW TABLES")
for row in cursor.fetchall():
    print(row[0])
conn.close()
print("Done")
