# 训练数据自动同步模块设计

> 目标：让 RAG 知识库（ChromaDB 的 ddl / documentation / sql_examples）随 MySQL schema 和用户真实查询自动演进，并在规模膨胀后保持检索质量。
>
> 状态：**设计稿，未实施**。

---

## 0. 现状：为什么需要这个模块

### 0.1 训练数据当前完全静态

全项目搜索 `add_ddl` / `add_documentation` / `add_sql_example` 的调用点，只有两个手动入口：

| 入口 | 位置 | 行为 |
|---|---|---|
| 初始化脚本 | `scripts/init_training.py` | 幂等，检测到已有数据即跳过，需 `--force` 才重建 |
| 管理 API | `POST /api/v1/train/{ddl,documentation,sql}` | 手工调用 |

没有任何自动机制：服务启动不同步、查询成功后不回写、schema 变更无感知。

### 0.2 实测已经产生漂移

比对 ChromaDB 训练 DDL 与 MySQL 实际结构（2026-08-30）：

```
ChromaDB 中的表: data_usage, esim_profiles, operators, orders, plans, roaming_packages, users
MySQL 实际表  : 上述 7 张 + app_users, conversations, conversation_messages,
                query_audit_log, v_data_usage, v_esim_profiles, v_operators,
                v_orders, v_plans, v_roaming_packages, v_users
```

上次 `scripts/add_performance_indexes.sql` 新增的 6 个索引未同步进 DDL：

| 表 | MySQL 有、DDL 无的索引 |
|---|---|
| users | `idx_phone_number` |
| orders | `idx_mvno_created`、`idx_user_status` |
| esim_profiles | `idx_mvno_status`、`idx_status_created`、`idx_user_status` |
| data_usage | `idx_country_date` |
| plans | `idx_type_status` |

> 说明：索引漂移**不影响** SQL 生成的正确性（LLM 只看表名/列名），但它是"知识库与数据库已经脱钩"的信号。真正的风险是未来**加列/删列**不同步 —— 会让 LLM 编造不存在的列。`v_*` 视图与 `app_users` 等平台表未训练是**有意为之**（RLS 已做行级过滤，不应让业务用户直查视图）。

### 0.3 必须先解决的隐患：ChromaDB 数据不共享

`k8s/app-deployment.yaml` 是 **2 副本**，但**没有挂载任何 PVC**：

```yaml
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: esim-app
          image: esim-nl2sql-platform:latest
          # ← 无 volumeMounts / volumes
```

后果：

1. 每个 Pod 的 `CHROMADB_PERSIST_DIR` 在各自容器文件系统内 → **两个副本各持一份独立知识库**
2. 在副本 A 上通过 API 训练的数据，副本 B 检索不到 → 查询结果**随负载均衡漂移而不一致**
3. Pod 重启即丢失，只有 `AUTO_INIT_TRAINING` 会重建 46 条基础数据，用户后续训练的全部蒸发
4. 定时更新任务若直接在两个副本上跑，两份数据会**分叉且无法收敛**

**这一条不解决，后面所有设计都是空中楼阁。**

---

## 1. 架构总览

```
                    ┌──────────────────────────┐
                    │   MySQL 8.0              │
                    │  ├─ information_schema   │  ← DDL 真值源
                    │  ├─ query_audit_log      │  ← 用户查询真值源(307 行)
                    │  └─ training_snapshot    │  ← 回滚快照(新增)
                    └────────────┬─────────────┘
                                 │
   ┌─────────────────────────────┼─────────────────────────────┐
   │  esim-app Pod A             │         esim-app Pod B      │
   │  ┌────────────────────────┐ │ ┌────────────────────────┐  │
   │  │ FastAPI + AsyncIOSched │ │ │ FastAPI + AsyncIOSched │  │
   │  │  job: sync_ddl   1h    │ │ │  job: sync_ddl   1h    │  │
   │  │  job: harvest_sql 1d   │ │ │  job: harvest_sql 1d   │  │
   │  │  job: prune_ex    1w   │ │ │  job: prune_ex    1w   │  │
   │  └───────────┬────────────┘ │ └───────────┬────────────┘  │
   └──────────────┼──────────────┴─────────────┼───────────────┘
                  │   抢锁（只有一个能跑）      │
                  └──────────┬─────────────────┘
                             ▼
                  ┌──────────────────────────┐
                  │  Redis（单副本）          │
                  │  SET lock:job:<name> NX PX│
                  └──────────────────────────┘
                             │ 抢到锁的副本执行
                             ▼
                  ┌──────────────────────────┐
                  │  ChromaDB Server         │  ← 独立 Deployment + PVC
                  │  (HttpClient 模式)        │     （替换当前进程内模式）
                  │  ddl / documentation /   │
                  │  sql_examples            │
                  └──────────────────────────┘
```

三条数据流：

| Job | 频率 | 源 | 目标 | 幂等 |
|---|---|---|---|---|
| `sync_ddl` | 每小时 | `information_schema` | `ddl` | ✅ 全量 diff |
| `harvest_sql` | 每天 02:00 | `query_audit_log` | `sql_examples` | ✅ 靠游标 |
| `prune_examples` | 每周日 03:00 | `sql_examples` | `sql_examples` + 归档表 | ✅ 评分排序 |

---

## 2. 技术选型

### 2.1 调度框架

| 方案 | 优势 | 劣势 | 结论 |
|---|---|---|---|
| **APScheduler** (AsyncIOScheduler) | 纯 Python、无外部依赖、与 FastAPI `lifespan` 天然集成、分钟级粒度 | 多副本需自行加分布式锁；无原生任务持久化 | ✅ **主选** |
| Celery + Beat | 成熟、支持重试/结果后端/优先级、任务持久化 | 引入 broker(RabbitMQ/Redis) + worker + flower，运维成本翻倍；本项目任务是轻量 I/O 密集，杀鸡用牛刀 | ❌ |
| K8s CronJob | 与 Web 进程完全隔离、不抢在线资源、天然单次执行 | 本地开发无 K8s 跑不了；日志与监控分离；需要单独镜像入口 | 🔵 **重型任务备选** |
| 外部 cron + curl 管理 API | 极简 | 无重试、无状态、鉴权裸露 | ❌ |

**选 APScheduler 的理由**：

1. 任务本身很轻 —— DDL diff 是几十次 `SHOW CREATE TABLE`，回写是几百条记录，单次执行在秒级，不需要队列削峰
2. 项目是 FastAPI 单体，没有消息队列基础设施，引入 Celery 会让 `docker-compose.yml` 和 K8s 清单膨胀一倍
3. `lifespan` 已经存在，接入只需 5 行
4. **多副本重复执行的问题 Celery Beat 同样存在**（Beat 本身也要单实例或用 `celery-redbeat` 加锁），APScheduler + Redis 锁是等价且更轻的解法
5. 若未来某个任务变重（例如全量重算 embedding），可平滑迁到 K8s CronJob，Job 逻辑代码复用

**关于任务持久化**：本项目三个任务都是**全量幂等 diff**，错过一次执行不会有状态损失（下次跑会自动补上）。因此用 `MemoryJobStore` 即可，不需要 Redis/SQLAlchemy jobstore。若后续加入"必须按时执行且需补跑"的任务，再换持久化 jobstore。

### 2.2 分布式锁

```python
# 加锁：SET key value NX PX ttl（原子）
acquired = await redis.set(f"lock:job:{name}", token, nx=True, px=ttl_ms)

# 释放：Lua 脚本比对 token 再删（避免误删别人的锁）
UNLOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""
```

要点：

- 用 `SET NX PX` 而非 `SETNX + EXPIRE`（后者非原子，进程崩溃会留下死锁）
- value 必须是随机 token，释放时 Lua 脚本比对 —— 防止 A 的锁过期后被 B 抢到，A 再来释放时误删 B 的锁
- TTL 要显著大于任务最长耗时（建议 `max_duration * 3`）
- 长任务需要**看门狗续租**：起一个协程每 `ttl/3` 秒执行 `PEXPIRE`，任务结束再释放。本项目任务秒级完成，暂不需要

### 2.3 ChromaDB 部署模式

| 模式 | 说明 | 结论 |
|---|---|---|
| PersistentClient（当前） | 进程内嵌，SQLite 后端，**不支持多进程并发写** | ❌ 2 副本下数据分叉 |
| PersistentClient + 共享 PVC(RWX) | 多 Pod 共享卷 | ❌ SQLite 在多进程写下会锁冲突/损坏 |
| **HttpClient + 独立 Server** | ChromaDB 独立 Deployment + PVC，app 用 `chromadb.HttpClient(host, port)` 连接 | ✅ **推荐** |

**选 HttpClient 的理由**：

- 彻底解决多副本数据一致性 —— 所有副本连同一个 Chroma 服务端
- 写入串行化由服务端保证，避免 SQLite 锁冲突
- 知识库可以独立于应用扩缩容、独立备份 PVC
- 改造成本低：`chroma_store.py` 里只有一处 `PersistentClient(...)`，改成 `HttpClient(host=..., port=...)` 即可，其余 CRUD 代码零改动

### 2.4 向量模型

现状：ONNX `all-MiniLM-L6-v2`（384 维，英文为主，中文能力弱）。

若自动回写大量**中文**问题，检索质量会退化。两个方向：

- 换 `paraphrase-multilingual-MiniLM-L12-v2`（384 维，支持中文）—— 需要重新生成全部 embedding
- 保持现状，但**回写前把中文问题改写为结构化的中英混合描述**（含表名/列名），用 SQL 的结构信息弥补语义模型的不足 ← 推荐先做这个，成本低

---

## 3. 三个 Job 的详细设计

### 3.1 Job 1：`sync_ddl` —— DDL 同步

```python
async def sync_ddl():
    # 1. 拉取 MySQL 真值（白名单表，排除 v_* 视图与平台表）
    mysql_ddl = {
        t: await fetch_show_create_table(t)
        for t in await list_business_tables()   # 白名单，排除 v_*/app_users/...
    }

    # 2. 读取 ChromaDB 现有 DDL
    chroma_ddl = {r.metadata["table_name"]: r.content for r in chroma_store.get_all("ddl")}

    # 3. 规范化后比对（去掉空白、AUTO_INCREMENT=n、注释时间戳等噪声）
    added   = set(mysql_ddl) - set(chroma_ddl)
    removed = set(chroma_ddl) - set(mysql_ddl)
    changed = {t for t in set(mysql_ddl) & set(chroma_ddl)
               if normalize(mysql_ddl[t]) != normalize(chroma_ddl[t])}

    # 4. 先写后删，保证检索不会拿到空窗
    for t in added | changed:
        new_id = chroma_store.add_ddl(mysql_ddl[t], table_name=t)
        await verify_retrievable(new_id)          # 写后立即验证可读
        if t in chroma_ddl:
            chroma_store.remove("ddl", old_ids[t]) # 验证通过再删旧的

    # 5. 删除只告警不执行（DROP 可能是误操作，或副本视图未就绪）
    if removed:
        logger.warning("Tables missing from MySQL, NOT auto-removed: %s", removed)
        DDL_DRIFT_TOTAL.inc(len(removed))

    # 6. 触发失效检测（见 3.3 第五层）
    await invalidate_stale_examples(changed | removed)
```

关键点：

- **白名单驱动**：只同步业务表，`v_*` 视图和 `app_users/conversations/query_audit_log` 永远不进知识库（安全考虑）
- **先写后删**：ChromaDB 不支持 update document，必须用 `add` + `delete`。若先删后写，期间检索会丢失该表信息
- **删除只告警**：自动删表风险远大于收益
- **规范化比对**：`SHOW CREATE TABLE` 每次输出都带 `AUTO_INCREMENT=123` 这类易变值，不规范化会产生永久假漂移

### 3.2 Job 2：`harvest_sql` —— SQL 示例回写

数据源：`query_audit_log`（已有 307 行，字段含 `question / generated_sql / execution_status / error_message / row_count / execution_time_ms / security_blocked / created_at`）

**质量门（全部满足才入库）**：

| 条件 | 理由 |
|---|---|
| `execution_status = 'success'` | 只收真的执行成功的 |
| `security_blocked = 0` | 被安全网关拦的绝不能进知识库 |
| `row_count > 0` | 空结果通常意味着 SQL 逻辑错误（条件过严/表选错） |
| `execution_time_ms < P95 * 1.5` | 排除慢查询，避免教会模型生成低效 SQL |
| sqlglot 解析通过 + 表全在白名单 | 语法/安全双重校验 |
| 与已有示例 embedding 相似度 < 0.95 | 去重复 |
| 同一 question 24h 内已被采集过则跳过 | 靠 `last_harvested_id` 游标幂等 |

**入库格式**（关键：不能只存原问题）：

```python
content = f"""问题: {question}
SQL: {sql}
涉及表: {tables}
查询模式: {pattern}          # 聚合/分组/时间序列/多表 JOIN/子查询
"""
metadata = {
    "question": question,
    "sql": sql,
    "tables": json.dumps(tables),        # 用于表覆盖保底
    "sql_template": templatize(sql),     # 用于模板去重
    "hit_count": 1,
    "source": "auto",                    # auto | manual | seed
    "quality_score": 0.0,                # 由 prune job 计算
    "created_at": now,
    "last_hit_at": now,
}
```

**为什么必须存 `sql_template` 和 `tables`**：这是后面精简策略的索引基础，不在这里存好，精简时需要对全库重新 sqlglot 解析，成本极高。

**可选的更高质量方案**：只在用户点"结果正确"时回写。质量最高但数据量少（用户通常不点）。建议**两者并行**：自动回写入"候选池"（`source=auto`，低权重），用户点赞的直接入"可信池"（`source=verified`，永不淘汰）。

### 3.3 Job 3：`prune_examples` —— sql_examples 精简

> 这是本设计的重点，单独展开，见第 4 节。

---

## 4. sql_examples 精简策略

### 4.1 为什么要精简

规模膨胀后会同时出现三个问题：

| 问题 | 表现 |
|---|---|
| **检索噪声** | `retrieve_context` 取 top-K（当前 max_items=5）。示例越多，相似但劣质的样本越容易挤占 top-5，反而把好的示例挤出去 |
| **检索变慢** | HNSW 索引随规模增长召回率下降、延迟上升 |
| **上下文膨胀** | 每条示例约 100-200 字符，top-5 就是 1KB。示例总数不影响单次上下文长度，但**相似度分布变密**会让 top-5 的区分度下降，LLM 注意力被稀释 |

注意一个常见误解：**示例总数本身不直接增加 prompt 长度**（因为只取 top-K），主要危害是**检索质量退化**和**存储/索引成本**。

### 4.2 五层精简策略

#### 第一层：写入前去重（防患于未然）

```
新示例 embedding → 在 sql_examples 中查 top-1
    相似度 >= 0.95  → 判为重复：不写入新记录，仅 UPDATE 旧记录的 hit_count += 1, last_hit_at = now()
    相似度 <  0.95  → 正常写入
```

这一层保证"同一个问题被问一万次"只占一条记录，是**成本最低、收益最大**的一层。

#### 第二层：SQL 模板去重（砍掉 70% 冗余）

用户的长尾查询高度集中在少数 SQL 模板上。用 sqlglot 把字面量替换为占位符：

```sql
-- 原始（3 条不同的记录）
SELECT COUNT(*) FROM orders WHERE mvno_id = 3 AND status = 'paid';
SELECT COUNT(*) FROM orders WHERE mvno_id = 7 AND status = 'paid';
SELECT COUNT(*) FROM orders WHERE mvno_id = 3 AND status = 'pending';

-- 模板化后（同一个模板）
SELECT COUNT(*) FROM orders WHERE mvno_id = ? AND status = ?;
```

策略：**每个模板最多保留 3 条**，优先保留 `hit_count` 最高的，其次保留字面量**多样性最大**的（覆盖不同枚举值，帮助 LLM 理解列的取值范围）。

预期效果：10 万条原始示例 → 模板化后通常只剩 1-3 万个模板 → 保留 3 条/模板 → **降到 3-9 万条**。但配合第三层评分还能再砍一个数量级。

#### 第三层：价值评分淘汰

每条示例计算综合得分：

```
score = w1 * log(1 + hit_count)          # 热度：被问得多的更有价值
      + w2 * recency_decay(last_hit_at)  # 时效：最近被问的说明贴合当前 schema
      + w3 * coverage_bonus              # 覆盖：涉及冷门表/罕见 JOIN 的加分
      + w4 * quality_score               # 质量：执行耗时、结果行数、人工点赞
      - w5 * redundancy_penalty          # 冗余：同模板内排名靠后递减
```

建议权重：`w1=0.35, w2=0.20, w3=0.20, w4=0.15, w5=0.10`（可用评估集回归调参）。

保留 top-N（建议 N=300~500，按 `MAX_QUERY_ROWS` 和评估集表现调优），其余进冷层。

> **不要纯按分数截断**。必须加**表覆盖保底约束**：每张业务表至少保留 K 条（建议 K=5）涉及它的示例，否则冷门表会彻底失去 few-shot 支持，导致相关查询准确率断崖下跌。实现上：先按表分组各取 K 条进保底集，剩余名额再按 score 全局排序补齐。

#### 第四层：冷热分层

```
热层 Hot  ：top 300 条 → 留在 ChromaDB sql_examples，参与每次向量检索
冷层 Cold ：其余全部 → 归档到 MySQL 表 sql_examples_archive
            仅当热层召回不足（top-1 相似度 < 0.7）时，做关键词兜底召回
```

好处：向量库规模恒定（检索延迟可控），数据不丢失（可随时回热），且冷层查询用 MySQL 索引很快。

#### 第五层：失效检测（最容易被忽略，但最致命）

**schema 变更后，引用了已删除列的示例会变成毒样本** —— 它会教 LLM 生成引用不存在列的 SQL，而且这种错误会自我强化（错误 SQL 执行失败 → 但示例还在 → 下次还这么生成）。

每次 `sync_ddl` 检测到 `changed` 或 `removed` 表后，立即执行：

```python
async def invalidate_stale_examples(changed_tables: set[str]):
    affected = query_examples_by_tables(changed_tables)   # 靠 metadata.tables 索引
    for ex in affected:
        parsed = sqlglot.parse_one(ex["sql"])
        refs = extract_table_column_refs(parsed)          # [(table, column), ...]
        if not all(is_valid_ref(t, c) for t, c in refs):
            archive_or_delete(ex)                          # 引用失效 → 淘汰
            STALE_EXAMPLES_TOTAL.inc()
```

**这一层是自动同步能长期稳定运行的前提** —— 没有它，知识库会随时间积累越来越多的毒样本。

### 4.3 精简效果预估

| 阶段 | 条数 | 说明 |
|---|---|---|
| 积累期（10 万次查询） | ~100,000 | 未精简 |
| 第一层去重后 | ~30,000 | 重复问题合并 |
| 第二层模板去重后 | ~8,000 | 每模板留 3 条 |
| 第三层评分淘汰 + 保底 | ~500 | 热层目标 |
| 第四层冷热分层 | 热 500 / 冷 7,500 | 归档不丢 |
| 第五层持续失效清理 | 动态 | 每次 schema 变更触发 |

---

## 5. 需要注意的问题

### 5.1 多副本重复执行 🔴

**问题**：2 副本各跑一份调度器，同一时刻触发会重复写入。
**解决**：Redis 分布式锁（`SET NX PX` + Lua 释放）。锁 TTL > 任务最长耗时 × 3。

### 5.2 ChromaDB 数据不共享 🔴

**问题**：见 0.3，当前 2 副本各持独立知识库，Pod 重启即丢。
**解决**：改 ChromaDB Server 模式（HttpClient），且**必须在定时更新模块之前完成**，否则两个副本各同步各的，数据永久分叉。

### 5.3 任务与在线查询争抢资源 🟡

**问题**：DDL diff 和示例回写是 CPU/IO 密集，会拖慢在线查询（这是同一个 Python 进程，受 GIL 影响）。
**解决**：
- 批量处理中插入 `await asyncio.sleep(0)` 让出事件循环
- 每批之间 `await asyncio.sleep(0.05)` 限速
- 大批量操作放在凌晨低峰
- 若仍然影响 P99，把 `prune_examples` 迁到 K8s CronJob（代码复用，只换触发方式）

### 5.4 ChromaDB 并发写 🟡

**问题**：`PersistentClient` 用 SQLite 后端，多进程写会锁冲突。
**解决**：由 5.2 的 Server 模式一并解决。若短期不改，则定时任务必须只在一个副本上跑（通过 `POD_NAME` 环境变量判断是否为 `esim-app-0`），且应用本身不并发写。

### 5.5 重建期间的检索一致性 🟡

**问题**：DDL 更新是 `add` + `delete` 两步，中间窗口检索可能拿到两份（新旧并存）或零份（先删后写）。
**解决**：**严格先写后删**，写入后立即 `verify_retrievable()` 验证，通过再删旧的。

### 5.6 自动回写的安全风险 🔴

**问题**：如果把用户触发的 SQL 直接写进知识库，恶意用户可以通过构造查询**污染知识库**（一种 prompt injection）。后续其他用户问相似问题时，会检索到被投毒的 SQL。
**解决**：
- 强制过 `sql_security` 安全网关 + 表白名单校验
- `source=auto` 的示例权重低于 `source=manual/verified`
- 人工审核入口：管理面板能看到所有 `source=auto` 的示例，支持批量删除
- 记录 `user_id`，出问题可追溯来源

### 5.7 LLM 成本失控 🟡

**问题**：如果用 LLM 给每条示例打质量分，10 万条就是 10 万次调用。
**解决**：质量分优先用**规则 + 统计量**（执行耗时、结果行数、hit_count、是否人工点赞），LLM 只用于极少数边界 case 或最终抽样审计。

### 5.8 失败重试与可观测性 🟡

**问题**：定时任务静默失败，知识库悄悄停止更新，等发现时已经漂移很久。
**解决**：
- 任务内指数退避重试（复用 `app/core/llm.py` 里已有的 `compute_backoff_delay` 思路）
- 暴露 Prometheus 指标（见第 6 节）
- 关键告警：`time() - last_success_timestamp > 2 * interval`

### 5.9 灰度与回滚 🔴

**问题**：自动更新写坏了知识库，SQL 生成准确率暴跌，无法恢复。
**解决**：
- 每次更新前 dump 全量知识库到 MySQL 表 `training_snapshot`（`id, snapshot_at, payload_json`）
- 保留最近 10 个快照
- 管理面板提供"一键回滚到某快照"
- 灰度：自动回写的示例先标记 `source=auto` 且 `enabled=false`，观察 N 天无异常再批量启用

### 5.10 时区混乱 🟡

**问题**：K8s CronJob 用 UTC，APScheduler 默认本地时区，混用会导致任务在预期外的时间执行。
**解决**：统一用 UTC 存储所有时间戳，APScheduler 显式传 `timezone=timezone.utc`，展示层再转本地时区。

### 5.11 多租户 🟡

**问题**：若不同 MVNO 的 schema 或业务口径不同，跨租户的示例会互相干扰。
**解决**：示例 metadata 加 `mvno_id`，检索时通过 ChromaDB 的 `where` 过滤。当前 RLS 已有 `mvno_id` 隔离，知识库层应对齐。

### 5.12 冷启动 🟡

**问题**：系统刚上线时 `query_audit_log` 是空的，没有可回写的示例。
**解决**：`scripts/init_training.py` 里的 24 条种子示例标记 `source=seed` 且**永不淘汰**，作为冷启动基线。

---

## 6. 监控与告警

### 6.1 Prometheus 指标

```python
TRAINING_JOB_DURATION   = Histogram("training_job_duration_seconds", "任务耗时", ["job"])
TRAINING_JOB_LAST_OK    = Gauge("training_job_last_success_timestamp", "上次成功时间", ["job"])
TRAINING_JOB_FAILURES   = Counter("training_job_failures_total", "失败次数", ["job"])
TRAINING_DDL_DRIFT      = Gauge("training_ddl_drift_total", "DDL 漂移表数量")
TRAINING_EXAMPLES       = Gauge("training_examples_total", "示例总数", ["type", "tier"])
TRAINING_PRUNED         = Counter("training_examples_pruned_total", "已精简条数", ["reason"])
TRAINING_STALE          = Counter("training_examples_stale_total", "失效清理条数")
```

### 6.2 告警规则

```yaml
- alert: TrainingJobStuck
  expr: time() - training_job_last_success_timestamp > 2 * 3600
  for: 10m
  annotations:
    summary: "训练同步任务 {{ $labels.job }} 超过 2 小时未成功"

- alert: DDLDriftDetected
  expr: training_ddl_drift_total > 0
  for: 30m
  annotations:
    summary: "检测到 {{ $value }} 张表的 DDL 与知识库不一致"

- alert: TrainingJobFailed
  expr: increase(training_job_failures_total[1h]) > 3
  annotations:
    summary: "训练同步任务 {{ $labels.job }} 1 小时内失败超过 3 次"
```

### 6.3 健康检查

`/healthz` 增加知识库状态字段：

```json
{
  "status": "ok",
  "training": {
    "chroma_mode": "http",
    "counts": {"ddl": 7, "documentation": 15, "sql_examples": 512},
    "last_sync": {"ddl": "2026-08-30T12:00:00Z", "harvest": "...", "prune": "..."},
    "drift_tables": ["users", "orders"]
  }
}
```

---

## 7. 实施路线图

| 阶段 | 内容 | 依赖 | 风险 |
|---|---|---|---|
| **P0** | ChromaDB 改 Server 模式 + PVC | 无 | 中（需回归全部 RAG 测试） |
| **P0** | Redis 分布式锁工具（`app/core/dist_lock.py`）+ 单测 | Redis 已就绪 | 低 |
| **P1** | `sync_ddl` + 漂移检测报告 | P0 | 低 |
| **P1** | 快照表 `training_snapshot` + 回滚 API | 无 | 低 |
| **P2** | `harvest_sql`（质量门 + 去重 + 安全校验） | P1 | 中（污染风险，需灰度） |
| **P2** | 第一层（写入去重）+ 第五层（失效检测） | P1 | 低 |
| **P3** | `prune_examples`：第二层模板去重 + 第三层评分 | P2 | 中（需评估集验证效果） |
| **P3** | 第四层冷热分层 | P3 | 低 |
| **P3** | Prometheus 指标 + 告警规则 | P1 | 低 |

**建议先做 P0 + P1**：这两个阶段解决"知识库与数据库脱钩"的核心问题，风险可控，且能立刻消掉当前已存在的 6 处索引漂移。P2/P3 涉及知识库写入，建议配合 54 题评估集做 A/B 验证（`scripts/eval/compare_eval3.py --trials 3`）确认准确率不降再全量。

---

## 8. 待决策项

1. **ChromaDB Server 模式是否可接受独立 Deployment + PVC**？（会增加一个 K8s 组件）
2. **自动回写是否需要人工审核**？（全自动 vs 半自动，影响数据量和污染风险）
3. **热层 N 值取多少**？建议先用评估集试 300 / 500 / 1000 三档
4. **向量模型是否换成多语言版**？（影响是否需要重建全部 embedding）
5. **任务调度是否接受"进程内"**？若线上确实有资源竞争，可把 `prune_examples` 单独迁到 K8s CronJob
