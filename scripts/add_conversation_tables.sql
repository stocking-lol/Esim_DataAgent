-- ============================================
-- eSIM NL2SQL Platform - 对话管理表
-- Day 6 新增：多轮对话持久化
-- ============================================

USE esim_platform;

-- 对话会话表
CREATE TABLE IF NOT EXISTS conversations (
    id              VARCHAR(36)     PRIMARY KEY COMMENT '对话ID (UUID)',
    user_id         INT             DEFAULT NULL COMMENT '用户ID',
    username        VARCHAR(100)    DEFAULT NULL COMMENT '用户名',
    title           VARCHAR(200)    DEFAULT NULL COMMENT '对话标题',
    message_count   INT             NOT NULL DEFAULT 0 COMMENT '消息数量',
    last_message_at DATETIME        DEFAULT NULL COMMENT '最后消息时间',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    INDEX idx_user_id (user_id),
    INDEX idx_created_at (created_at),
    INDEX idx_last_message (last_message_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='对话会话表';

-- 对话消息表
CREATE TABLE IF NOT EXISTS conversation_messages (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    conversation_id VARCHAR(36)     NOT NULL COMMENT '对话ID',
    role            ENUM('user','assistant','system') NOT NULL COMMENT '消息角色',
    content         TEXT            NOT NULL COMMENT '消息内容',
    generated_sql   TEXT            DEFAULT NULL COMMENT '生成的SQL（assistant消息）',
    sql_status      VARCHAR(20)     DEFAULT NULL COMMENT 'SQL执行状态',
    row_count       INT             DEFAULT NULL COMMENT '返回行数',
    execution_time_ms INT           DEFAULT NULL COMMENT '执行耗时(毫秒)',
    error_message   TEXT            DEFAULT NULL COMMENT '错误信息',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',

    INDEX idx_conversation_id (conversation_id),
    INDEX idx_role (role),
    INDEX idx_created_at (created_at),

    CONSTRAINT fk_msg_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='对话消息表';
