-- ============================================================
-- 性能索引优化（学习计划·MySQL 索引落地）
-- ------------------------------------------------------------
-- 背景：users 表缺少 phone_number 索引，按手机号查询走全表扫描
--       （EXPLAIN type=ALL）。phone_number 为高基数列（50/50 去重），
--       建立普通 B+ 树索引收益明确。
--
-- 验证方式（EXPLAIN 对比，见 docs/db_index_optimization.md）：
--   BEFORE: SELECT ... WHERE phone_number = '...'
--           type=ALL（全表扫描）
--   AFTER : type=ref, key=idx_phone_number
-- ============================================================

-- users: 按手机号精确查询（业务高频，如用户身份核验）
ALTER TABLE users
    ADD INDEX idx_phone_number (phone_number)
    COMMENT '手机号查询索引（高基数列）';

-- esim_profiles: profile_status + created_at 组合（运营分析"某状态档案的时间分布"）
--   已有 idx_profile_status 单列与 idx_user_status；补充与时间组合的查询路径
ALTER TABLE esim_profiles
    ADD INDEX idx_status_created (profile_status, created_at)
    COMMENT '状态+创建时间组合索引（状态过滤后按时间排序）';

-- data_usage: 国家 + 日期组合（漫游流量地域分析"某国家某时段用量"）
--   已有 idx_country_code 与 idx_date_roaming；country 过滤后按 usage_date 排序可被覆盖
ALTER TABLE data_usage
    ADD INDEX idx_country_date (country_code, usage_date)
    COMMENT '国家+日期组合索引（地域流量分析）';
