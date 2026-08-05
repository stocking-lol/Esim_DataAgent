-- ============================================
-- eSIM NL2SQL Platform - Database Schema
-- eSIM 运营数据模型 v1.0
-- ============================================

-- 创建数据库（如果不存在）
CREATE DATABASE IF NOT EXISTS esim_platform
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE esim_platform;

-- ============================================
-- 1. operators - 运营商信息表
-- ============================================
DROP TABLE IF EXISTS data_usage;
DROP TABLE IF EXISTS esim_profiles;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS roaming_packages;
DROP TABLE IF EXISTS plans;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS operators;

CREATE TABLE operators (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(100)    NOT NULL COMMENT '运营商名称',
    type        ENUM('MNO', 'MVNO') NOT NULL COMMENT '运营商类型：MNO移动网络运营商/MVNO虚拟运营商',
    mcc_mnc     VARCHAR(6)      NOT NULL COMMENT 'MCC+MNC码（移动国家码+移动网络码）',
    country     VARCHAR(100)    NOT NULL COMMENT '所属国家/地区',
    status      ENUM('active','inactive') NOT NULL DEFAULT 'active' COMMENT '运营状态',
    contact_info VARCHAR(200)   DEFAULT NULL COMMENT '联系方式',
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    UNIQUE KEY uk_mcc_mnc (mcc_mnc),
    INDEX idx_type (type),
    INDEX idx_country (country),
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='运营商信息表';


-- ============================================
-- 2. users - eSIM用户信息表
-- ============================================
CREATE TABLE users (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    phone_number VARCHAR(20)    DEFAULT NULL COMMENT '手机号码',
    email       VARCHAR(255)    DEFAULT NULL COMMENT '邮箱地址',
    iccid       VARCHAR(22)     NOT NULL COMMENT 'eSIM芯片唯一标识符（19-20位，8986开头）',
    imsi        VARCHAR(15)     DEFAULT NULL COMMENT 'IMSI国际移动用户身份码（MCC+MNC+MSIN）',
    mvno_id     INT             NOT NULL COMMENT '所属虚拟运营商ID',
    status      ENUM('active','inactive','suspended') NOT NULL DEFAULT 'active' COMMENT '用户状态',
    region      VARCHAR(100)    DEFAULT NULL COMMENT '所属地区（国家/城市）',
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
    updated_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    UNIQUE KEY uk_iccid (iccid),
    UNIQUE KEY uk_imsi (imsi),
    INDEX idx_mvno_id (mvno_id),
    INDEX idx_status (status),
    INDEX idx_region (region),
    INDEX idx_created_at (created_at),
    INDEX idx_status_created (status, created_at),

    CONSTRAINT fk_users_mvno FOREIGN KEY (mvno_id) REFERENCES operators(id)
        ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='eSIM用户信息表';


-- ============================================
-- 3. plans - 套餐信息表
-- ============================================
CREATE TABLE plans (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(200)    NOT NULL COMMENT '套餐名称',
    data_volume_mb  INT             NOT NULL DEFAULT 0 COMMENT '数据流量（MB）',
    voice_minutes   INT             NOT NULL DEFAULT 0 COMMENT '语音分钟数',
    sms_count       INT             NOT NULL DEFAULT 0 COMMENT '短信条数',
    price           DECIMAL(10,2)   NOT NULL DEFAULT 0.00 COMMENT '套餐价格',
    currency        VARCHAR(10)     NOT NULL DEFAULT 'CNY' COMMENT '币种',
    validity_days   INT             NOT NULL DEFAULT 30 COMMENT '有效期（天）',
    type            ENUM('local','roaming','global') NOT NULL COMMENT '套餐类型：本地/漫游/全球',
    mvno_id         INT             NOT NULL COMMENT '所属运营商ID',
    status          ENUM('active','inactive','discontinued') NOT NULL DEFAULT 'active' COMMENT '套餐状态',
    description     TEXT            DEFAULT NULL COMMENT '套餐描述',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    INDEX idx_type (type),
    INDEX idx_mvno_id (mvno_id),
    INDEX idx_status (status),
    INDEX idx_price (price),
    INDEX idx_type_status (type, status),

    CONSTRAINT fk_plans_mvno FOREIGN KEY (mvno_id) REFERENCES operators(id)
        ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='套餐信息表';


-- ============================================
-- 4. orders - 订单信息表
-- ============================================
CREATE TABLE orders (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    user_id         INT             NOT NULL COMMENT '用户ID',
    plan_id         INT             NOT NULL COMMENT '套餐ID',
    order_no        VARCHAR(50)     NOT NULL COMMENT '订单号',
    status          ENUM('pending','paid','activated','cancelled','refunded') NOT NULL DEFAULT 'pending' COMMENT '订单状态',
    amount          DECIMAL(10,2)   NOT NULL DEFAULT 0.00 COMMENT '订单金额',
    currency        VARCHAR(10)     NOT NULL DEFAULT 'CNY' COMMENT '币种',
    payment_method  VARCHAR(50)     DEFAULT NULL COMMENT '支付方式（alipay/wechat/card等）',
    mvno_id         INT             NOT NULL COMMENT '所属运营商ID',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '下单时间',
    activated_at    DATETIME        DEFAULT NULL COMMENT '激活时间',
    cancelled_at    DATETIME        DEFAULT NULL COMMENT '取消时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    UNIQUE KEY uk_order_no (order_no),
    INDEX idx_user_id (user_id),
    INDEX idx_plan_id (plan_id),
    INDEX idx_mvno_id (mvno_id),
    INDEX idx_status (status),
    INDEX idx_created_at (created_at),
    INDEX idx_user_status (user_id, status),
    INDEX idx_mvno_created (mvno_id, created_at),

    CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_orders_plan FOREIGN KEY (plan_id) REFERENCES plans(id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_orders_mvno FOREIGN KEY (mvno_id) REFERENCES operators(id)
        ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='订单信息表';


-- ============================================
-- 5. esim_profiles - eSIM Profile管理表
-- ============================================
CREATE TABLE esim_profiles (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    user_id         INT             NOT NULL COMMENT '用户ID',
    iccid           VARCHAR(22)     NOT NULL COMMENT 'eSIM芯片标识符',
    imsi            VARCHAR(15)     DEFAULT NULL COMMENT 'IMSI用户身份模块标识',
    profile_status  ENUM('downloaded','installed','active','enabled','disabled','deleted') NOT NULL DEFAULT 'downloaded' COMMENT 'Profile状态',
    activation_code VARCHAR(100)    DEFAULT NULL COMMENT '激活码',
    mno_id          INT             NOT NULL COMMENT '归属MNO运营商ID',
    mvno_id         INT             NOT NULL COMMENT '所属MVNO运营商ID',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    activated_at    DATETIME        DEFAULT NULL COMMENT '激活时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    INDEX idx_user_id (user_id),
    INDEX idx_iccid (iccid),
    INDEX idx_profile_status (profile_status),
    INDEX idx_mno_id (mno_id),
    INDEX idx_mvno_id (mvno_id),
    INDEX idx_created_at (created_at),
    INDEX idx_mvno_status (mvno_id, profile_status),
    INDEX idx_user_status (user_id, profile_status),

    CONSTRAINT fk_profiles_user FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_profiles_mno FOREIGN KEY (mno_id) REFERENCES operators(id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_profiles_mvno FOREIGN KEY (mvno_id) REFERENCES operators(id)
        ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='eSIM Profile管理表';


-- ============================================
-- 6. data_usage - 流量使用记录表
-- ============================================
CREATE TABLE data_usage (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    user_id         INT             NOT NULL COMMENT '用户ID',
    iccid           VARCHAR(22)     NOT NULL COMMENT 'eSIM芯片标识符',
    usage_mb        DECIMAL(10,2)   NOT NULL DEFAULT 0.00 COMMENT '使用流量（MB）',
    roaming_flag    TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否漫游：0=否 1=是',
    country_code    VARCHAR(10)     DEFAULT NULL COMMENT '使用地国家码（如CN/US/JP）',
    usage_date      DATE            NOT NULL COMMENT '使用日期',
    recorded_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录时间',

    INDEX idx_user_id (user_id),
    INDEX idx_iccid (iccid),
    INDEX idx_usage_date (usage_date),
    INDEX idx_roaming_flag (roaming_flag),
    INDEX idx_country_code (country_code),
    INDEX idx_user_date (user_id, usage_date),
    INDEX idx_date_roaming (usage_date, roaming_flag),

    CONSTRAINT fk_usage_user FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='流量使用记录表';


-- ============================================
-- 7. roaming_packages - 漫游包信息表
-- ============================================
CREATE TABLE roaming_packages (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(200)    NOT NULL COMMENT '漫游包名称',
    countries       VARCHAR(500)    NOT NULL COMMENT '适用国家/地区（逗号分隔）',
    data_volume_mb  INT             NOT NULL DEFAULT 0 COMMENT '数据流量（MB）',
    duration_days   INT             NOT NULL DEFAULT 1 COMMENT '有效时长（天）',
    price           DECIMAL(10,2)   NOT NULL DEFAULT 0.00 COMMENT '价格',
    currency        VARCHAR(10)     NOT NULL DEFAULT 'CNY' COMMENT '币种',
    operator_id     INT             NOT NULL COMMENT '运营商ID',
    status          ENUM('active','inactive') NOT NULL DEFAULT 'active' COMMENT '状态',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    INDEX idx_operator_id (operator_id),
    INDEX idx_status (status),
    INDEX idx_price (price),

    CONSTRAINT fk_roaming_operator FOREIGN KEY (operator_id) REFERENCES operators(id)
        ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='漫游包信息表';


-- ============================================
-- 审计日志表（平台内部使用）
-- ============================================
CREATE TABLE IF NOT EXISTS query_audit_log (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id         INT             DEFAULT NULL COMMENT '查询用户ID',
    username        VARCHAR(100)    DEFAULT NULL COMMENT '用户名',
    question        TEXT            NOT NULL COMMENT '原始自然语言问题',
    generated_sql   TEXT            DEFAULT NULL COMMENT '生成的SQL语句',
    execution_status VARCHAR(50)    DEFAULT NULL COMMENT '执行状态：success/error/blocked',
    error_message   TEXT            DEFAULT NULL COMMENT '错误信息',
    execution_time_ms INT           DEFAULT 0 COMMENT '执行时间（毫秒）',
    row_count       INT             DEFAULT 0 COMMENT '返回行数',
    ip_address      VARCHAR(45)     DEFAULT NULL COMMENT '请求IP',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录时间',

    INDEX idx_user_id (user_id),
    INDEX idx_exec_status (execution_status),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='查询审计日志表';
