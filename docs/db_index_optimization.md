# MySQL 索引优化实践记录（学习计划·MySQL 模块落地）

> 对应 `docs/backend_learning_plan.md` 第 1 模块（MySQL 索引/EXPLAIN）。
> 原则：每个知识点在项目真实库上验证，EXPLAIN 说话。

## 一、背景：发现索引缺口

审计 `users` 表索引后发现：**`phone_number` 无索引**。业务上"按手机号查用户"是高
频操作（用户身份核验），当前查询走全表扫描：

```sql
EXPLAIN SELECT id, phone_number, email FROM users WHERE phone_number = '13800000001';
-- BEFORE: type=ALL  key=NULL  rows=50   （全表扫描）
```

列特征：`users` 共 50 行，`phone_number` 去重 50/50 → **高基数列**，建普通 B+ 树
索引收益明确（高基数 = 索引选择性好，单次查找能快速收敛）。

## 二、修复：新增 3 个索引（`scripts/add_performance_indexes.sql`）

```sql
ALTER TABLE users         ADD INDEX idx_phone_number (phone_number);
ALTER TABLE esim_profiles ADD INDEX idx_status_created (profile_status, created_at);
ALTER TABLE data_usage    ADD INDEX idx_country_date (country_code, usage_date);
```

## 三、EXPLAIN 前后对比（真实库实测）

### 1. users.phone_number —— 真实有效的提升

| 阶段 | type | key | rows |
|---|---|---|---|
| BEFORE | **ALL**（全表扫描） | NULL | 50 |
| AFTER | **ref**（索引查找） | idx_phone_number | **1** |

> 50 行时已见类型变化；真实生产数据量（百万级）下差异是"秒级 vs 毫秒级"。
> `ref` = 普通索引等值匹配，是点查的理想访问类型。

### 2. data_usage 联合索引 —— 避免 filesort（有 ORDER BY 时优化器才选）

```sql
EXPLAIN SELECT * FROM data_usage
        WHERE country_code='US' ORDER BY usage_date LIMIT 10;
-- 结果: key=idx_country_date（联合索引，索引本身已按 usage_date 有序，无需 filesort）
```

教学点：**联合索引 (country_code, usage_date) 的最左前缀**——等值 country_code +
范围/排序 usage_date，索引已有序，避免 `Using filesort`。若只有单列索引
`idx_country_code`，`ORDER BY usage_date` 需额外排序。

### 3. 观察：优化器会"看数据量"选索引

esim_profiles 查询（`status='active' AND created_at>=...`）无 ORDER BY 时，优化器
选了单列 `idx_profile_status` 而非新联合索引——因为当前数据量小，单列过滤成本已
足够低，联合索引无额外收益。**索引建了不一定被用，优化器按成本估算决策**——
这正是"为什么 EXPLAIN 是索引调优的第一步"。

## 四、面试回答模板（3 句讲透）

> **讲现象**：我在项目里审计 EXPLAIN，发现 `users.phone_number` 查询是全表扫描
> （type=ALL），而手机号是高基数字段。
> **讲原理**：B+ 树索引把全表扫描变成 O(logN) 的索引查找；高基数列索引选择性好，
> `ref` 类型等值匹配直接定位。
> **讲取舍**：加联合索引时遵循最左前缀，并用 `EXPLAIN` 验证优化器是否真的用它
> （数据量小时优化器可能选更简单的单列索引，有 ORDER BY 时联合索引才能避免 filesort）。

## 五、延伸知识点（复习清单）

- [ ] B+ 树 vs 红黑树/Hash：为什么磁盘友好（页大小、扇出高、范围查询）
- [ ] 聚簇索引 vs 二级索引：回表是什么、覆盖索引如何避免
- [ ] 最左前缀原则：联合索引 (a,b,c) 能命中哪些查询
- [ ] EXPLAIN 关键列：type（ALL→index→range→ref→eq_ref→const 越靠右越好）、
      rows（预估扫描行数）、Extra（Using filesort / Using temporary 是危险信号）
- [ ] 隐式类型转换 / 函数包裹导致索引失效（`WHERE DATE(created_at)=...` vs 范围查询）
