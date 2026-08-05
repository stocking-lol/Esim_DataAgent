-- ============================================
-- eSIM NL2SQL Platform - Read-Only Views
-- 只读视图：隐藏敏感字段，供只读用户使用
-- ============================================

USE esim_platform;

-- ============================================
-- 1. v_users - 用户视图（隐藏 phone_number/email/iccid/imsi）
-- ============================================
DROP VIEW IF EXISTS v_users;
CREATE OR REPLACE VIEW v_users AS
SELECT
    id,
    NULL                                    AS phone_number,
    NULL                                    AS email,
    NULL                                    AS iccid,
    NULL                                    AS imsi,
    mvno_id,
    status,
    region,
    created_at,
    updated_at
FROM users;

-- ============================================
-- 2. v_plans - 套餐视图（全字段，无敏感信息）
-- ============================================
DROP VIEW IF EXISTS v_plans;
CREATE OR REPLACE VIEW v_plans AS
SELECT
    id,
    name,
    data_volume_mb,
    voice_minutes,
    sms_count,
    price,
    currency,
    validity_days,
    type,
    mvno_id,
    status,
    description,
    created_at,
    updated_at
FROM plans;

-- ============================================
-- 3. v_orders - 订单视图（全字段，无敏感信息）
-- ============================================
DROP VIEW IF EXISTS v_orders;
CREATE OR REPLACE VIEW v_orders AS
SELECT
    id,
    user_id,
    plan_id,
    order_no,
    status,
    amount,
    currency,
    payment_method,
    mvno_id,
    created_at,
    activated_at,
    cancelled_at,
    updated_at
FROM orders;

-- ============================================
-- 4. v_esim_profiles - eSIM Profile 视图（隐藏 iccid/imsi/activation_code）
-- ============================================
DROP VIEW IF EXISTS v_esim_profiles;
CREATE OR REPLACE VIEW v_esim_profiles AS
SELECT
    id,
    user_id,
    NULL                                    AS iccid,
    NULL                                    AS imsi,
    profile_status,
    NULL                                    AS activation_code,
    mno_id,
    mvno_id,
    created_at,
    activated_at,
    updated_at
FROM esim_profiles;

-- ============================================
-- 5. v_data_usage - 流量使用视图（全字段，无敏感信息）
-- ============================================
DROP VIEW IF EXISTS v_data_usage;
CREATE OR REPLACE VIEW v_data_usage AS
SELECT
    id,
    user_id,
    iccid,
    usage_mb,
    roaming_flag,
    country_code,
    usage_date,
    recorded_at
FROM data_usage;

-- ============================================
-- 6. v_operators - 运营商视图（全字段，无敏感信息）
-- ============================================
DROP VIEW IF EXISTS v_operators;
CREATE OR REPLACE VIEW v_operators AS
SELECT
    id,
    name,
    type,
    mcc_mnc,
    country,
    status,
    contact_info,
    created_at,
    updated_at
FROM operators;

-- ============================================
-- 7. v_roaming_packages - 漫游包视图（全字段，无敏感信息）
-- ============================================
DROP VIEW IF EXISTS v_roaming_packages;
CREATE OR REPLACE VIEW v_roaming_packages AS
SELECT
    id,
    name,
    countries,
    data_volume_mb,
    duration_days,
    price,
    currency,
    operator_id,
    status,
    created_at,
    updated_at
FROM roaming_packages;

-- ============================================
-- 创建只读 MySQL 用户并授权
-- ============================================
-- 创建用户（如果不存在）
CREATE USER IF NOT EXISTS 'esim_readonly'@'localhost' IDENTIFIED BY 'esim_readonly_2026';
CREATE USER IF NOT EXISTS 'esim_readonly'@'%' IDENTIFIED BY 'esim_readonly_2026';

-- 刷新权限
FLUSH PRIVILEGES;

-- 授予只读用户对所有视图的 SELECT 权限（localhost）
GRANT SELECT ON esim_platform.v_users TO 'esim_readonly'@'localhost';
GRANT SELECT ON esim_platform.v_plans TO 'esim_readonly'@'localhost';
GRANT SELECT ON esim_platform.v_orders TO 'esim_readonly'@'localhost';
GRANT SELECT ON esim_platform.v_esim_profiles TO 'esim_readonly'@'localhost';
GRANT SELECT ON esim_platform.v_data_usage TO 'esim_readonly'@'localhost';
GRANT SELECT ON esim_platform.v_operators TO 'esim_readonly'@'localhost';
GRANT SELECT ON esim_platform.v_roaming_packages TO 'esim_readonly'@'localhost';

-- 尝试为远程主机授权（可能因权限不足而失败，不影响 localhost）
GRANT SELECT ON esim_platform.v_users TO 'esim_readonly'@'%';
GRANT SELECT ON esim_platform.v_plans TO 'esim_readonly'@'%';
GRANT SELECT ON esim_platform.v_orders TO 'esim_readonly'@'%';
GRANT SELECT ON esim_platform.v_esim_profiles TO 'esim_readonly'@'%';
GRANT SELECT ON esim_platform.v_data_usage TO 'esim_readonly'@'%';
GRANT SELECT ON esim_platform.v_operators TO 'esim_readonly'@'%';
GRANT SELECT ON esim_platform.v_roaming_packages TO 'esim_readonly'@'%';

FLUSH PRIVILEGES;
