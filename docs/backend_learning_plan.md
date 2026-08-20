# 后端工程师补充学习计划（基于 eSIM NL2SQL 平台特性）

> 目标：把项目里**用到的每个特性**从"会用"升级到"懂原理 + 能讲透 + 经得起追问"。
> 原则：每学一个知识点，都回到项目代码里找到对应实现/场景验证——学以致用，面试可指。
> 状态说明：✅ 已具备（可讲）· 🟡 需补强（会用在说不透）· 🔵 新学（项目中无直接对应）

---

## 总览

| 模块 | 状态 | 优先级 | 建议投入 | 与项目对应点 |
|---|---|---|---|---|
| MySQL | 🟡 | 高 | 2-3 天 | 表设计/RLS 注入/慢查询 |
| Redis | 🟡 | 高 | 2 天 | 查询缓存（刚集成） |
| Docker/K8s | 🟡 | 高 | 2-3 天 | compose 全栈 + k8s 清单（刚补齐） |
| FastAPI/异步 | 🟡 | 中高 | 2 天 | 全项目 |
| 网络/HTTP/认证 | 🟡 | 中高 | 1-2 天 | REST/JWT/限流 |
| 安全工程 | ✅→🟡 | 中 | 1 天 | 安全网关/RLS/脱敏（已很强，成体系化） |
| 可观测性 | 🟡 | 中 | 1-2 天 | Prometheus/Grafana/审计 |
| 消息队列 | 🔵 | 低（可选） | 2 天 | 暂无——可加审计事件异步化场景 |
| AI 栈（LLM/RAG） | ✅ | 持续 | 持续 | 已有深度，补充"重排/混合检索"进阶 |
| 编程语言 | ✅Python | 持续 | 1 天/周 | Python 进阶 + Go 入门（可选） |

---

## 1. MySQL（🟡 需补强）

**项目里用到**：7 表 schema 设计（users/plans/orders/esim_profiles/data_usage/roaming_packages/operators）、RLS 通过 sqlglot AST 注入 WHERE 条件、查询带 `MAX_EXECUTION_TIME` 与 LIMIT 控制、审计日志表。

**要补的知识点**：
- [ ] **索引原理**：B+ 树结构、聚簇索引 vs 二级索引、回表、覆盖索引、最左前缀原则
  - → 验证：给 `orders(created_at)`、`data_usage(user_id, used_at)` 建索引，用 `EXPLAIN` 对比查询计划；在面试里讲"为什么要给 RLS 的 mvno_id 列建联合索引"
- [ ] **事务隔离级别**：RC/RR 区别、MVCC 原理、当前读 vs 快照读、幻读
  - → 验证：讲清审计日志为什么用默认 RR 也安全（单条插入）
- [ ] **慢查询优化**：`EXPLAIN` 关键字段（type/rows/Extra）、using filesort/temporary 危害
  - → 验证：写一个全表扫描查询（如 `WHERE phone_number LIKE '%138%'`）对比加索引前后
- [ ] **锁与死锁**：行锁/间隙锁/意向锁、死锁检测
- [ ] **性能参数**：连接池（SQLAlchemy pool_size）、`slow_query_log` 开启

**面试经典题**：索引为什么用 B+ 树不用红黑树？最左前缀怎么匹配？`LIKE '%xx%'` 为什么失效？

---

## 2. Redis（🟡 已集成，补原理）

**项目里用到**：查询缓存 RedisCacheBackend（TTL、JSON 序列化、fail-soft 降级内存）。

**要补的知识点**：
- [ ] **数据结构**：String/List/Hash/Set/ZSet 各自适用场景（不止 KV）
  - → 验证：设想如何用 ZSet 做"热门查询 TOP-N"、用 Hash 存用户画像
- [ ] **缓存三大问题**：穿透（查不存在→击穿 DB）、击穿（热点 key 过期→并发打 DB）、雪崩（大批同时过期）
  - → 验证：本项目缓存 key = question+role+mvno，命中率如何？热点问题怎么防击穿？（答案：缓存永不过期+后台刷新 / 互斥锁重建）
- [ ] **淘汰策略与持久化**：maxmemory-policy（LRU/LFU）、RDB vs AOF
- [ ] **过期删除**：惰性删除 + 定期删除
- [ ] **分布式锁**（可选）：SETNX + 过期时间 + 续期（Redisson 思路）
- [ ] **与内存缓存对比**：为什么多副本要 Redis？你项目 auto 降级的价值？

**面试经典题**：缓存击穿和穿透的区别与解法？Redis 为什么快（单线程/IO 多路复用）？

---

## 3. Docker / K8s（🟡 已补齐清单，补原理）

**项目里用到**：docker-compose 全栈（mysql/chromadb/redis/prometheus/grafana）、`k8s/` 清单（Deployment/Service/ConfigMap/Secret/PVC/三类探针）。

**要补的知识点**：
- [ ] **镜像与容器**：镜像分层（Layer）原理、COPY/ADD 缓存、多阶段构建
  - → 验证：优化 Dockerfile（合并 RUN、多阶段）看构建体积下降
- [ ] **容器网络**：bridge/host 网络、端口映射原理、跨容器通信
- [ ] **K8s 核心对象**：Pod/Deployment/Service/ConfigMap/Secret/PVC/Ingress 各自的职责
- [ ] **探针三兄弟**：liveness/readiness/startup 区别与配置场景（你清单里已用，讲透为什么）
- [ ] **调度与扩缩容**：replicas 水平扩展、resource requests/limits、HPA（可选）
- [ ] **滚动更新**：maxSurge/maxUnavailable、零停机发布
- [ ] **网络模型**（可选）：Service 的 ClusterIP/NodePort/LoadBalancer 三种类型差异

**面试经典题**：Docker 镜像为什么比 VM 轻？readiness vs liveness 用错会怎样？

---

## 4. FastAPI / 异步（🟡）

**项目里用到**：FastAPI 全站、async/await、asyncio.Lock（旧缓存）、async 评估、uvicorn。

**要补的知识点**：
- [ ] **异步原理**：事件循环、协程、await 的调度、I/O 密集 vs CPU 密集
- [ ] **asyncio 底层**：Future/Task、`await` 挂起点、`gather/create_task` 并发控制
- [ ] **阻塞陷阱**：async 函数里调同步 I/O（如 requests、time.sleep）会卡事件循环
  - → 验证：检查项目里有没有同步调用混入 async 路径（openai 同步客户端在 stream_request 里的处理——你包装过的 ResilientOpenAILlmService 就在处理这个）
- [ ] **uvicorn worker 模型**：多进程 vs 事件循环、`--workers` 与 GIL
- [ ] **FastAPI 特性**：依赖注入、Pydantic 校验、中间件、BackgroundTasks
- [ ] **并发安全**：共享状态（内存缓存）在 async 下的竞态

**面试经典题**：一个 `async def` 里调用 `requests.get()` 会怎样？怎么修（httpx.AsyncClient / 线程池）？

---

## 5. 网络 / HTTP / 认证（🟡）

**项目里用到**：RESTful API 全链路、JWT 认证（access_token）、限流中间件、CORS。

**要补的知识点**：
- [ ] **TCP 三次握手/四次挥手**、TIME_WAIT 为什么存在
- [ ] **HTTP/1.1 vs HTTP/2**：队头阻塞、多路复用、二进制分帧
- [ ] **REST 设计**：资源命名、状态码语义、幂等性
- [ ] **JWT 原理**：三段式（header/payload/signature）、无状态认证、token 过期与续期、与 session 对比
  - → 验证：讲清你的 `get_current_user` 解析流程、`get_optional_user` 匿名降级设计
- [ ] **限流算法**：固定窗口/滑动窗口/令牌桶/漏桶——你的 rate_limit 用了哪种？
- [ ] **HTTPS/TLS**（可选）：握手流程、证书链

**面试经典题**：JWT 被偷了怎么办？限流为什么用令牌桶不用固定窗口？

---

## 6. 安全工程（✅ 已很强 → 成体系化表达）

**项目里用到**：fail-closed 四层网关、75 攻防用例、RLS、列脱敏、注释拆分绕过修复。

**要做的事**：不是补知识，而是**把已做的串成体系**（面试可讲）：
- [ ] 画一遍"四层防线"执行顺序：输入过滤 → schema 限制 → AST 校验 → 结果检查
- [ ] 用 OWASP Top 10 给项目"贴标签"：SQL 注入（A03）/ 越权（A01）→ 你是怎么防的
- [ ] 把"注释拆分绕过"的发现-修复过程讲成故事（探针扫描 → 发现 DRO/**/P → MySQL 词法拼接 → 修复）

---

## 7. 可观测性（🟡）

**项目里用到**：Prometheus 指标（QPS/P95/拦截率/纠错率）、Grafana 面板、审计日志、Gunicorn 访问日志。

**要补的知识点**：
- [ ] **Prometheus 四种指标**：Counter/Gauge/Histogram/Summary 差异与适用
  - → 验证：看 `app/middleware/metrics.py` 里各指标用的哪种类型、为什么
- [ ] **P95/P99 计算**：Histogram bucket 原理
- [ ] **链路追踪**（可选）：OpenTelemetry 概念（trace/span/parent_id）——Vanna 2.0 observability 用的就是这套，你拆解过
- [ ] **告警规则**：rate 语法、阈值设置（`alerts.yml` 里已写）

**面试经典题**：指标和日志的区别？一个查询慢了怎么排查（用你项目的 trace/指标/日志三层）？

---

## 8. 消息队列（🔵 可选，建议加一个场景）

**项目里用到**：暂无。可低成本加入审计事件异步化。

**要补的知识点**：MQ 解决什么问题（解耦/削峰/异步）、Kafka vs RabbitMQ 对比、消费组、顺序性/幂等。

**落地建议**：把查询审计日志改为"生产者发事件 → 消费者落库"，用本地内存队列模拟（不引依赖），面试讲"MVP 版事件驱动"。

---

## 9. AI 栈进阶（✅ 已有基础，持续深化）

**项目里用到**：RAG 全链路（文档解析/分块/向量化/混合检索）、LLM API、Agent 编排、评估闭环。

**要补的知识点**：
- [ ] **重排（Rerank）**：向量召回 → 精排两阶段；本项目混合检索就是轻量版重排，讲清楚异同
- [ ] **Embedding 模型选型**：为什么 MiniLM 对中文弱？中文向量模型（BGE 等）的改进点
- [ ] **上下文工程**：few-shot 选择策略、token 预算管理（你已有总量控制）
- [ ] **函数调用（function calling）**协议细节：tool_calls 消息流、参数校验失败处理
- [ ] **评估方法论**：为什么单轮评估不可靠（你的 trials 数据就是证据）、显著性概念

---

## 10. 编程语言（✅ Python / 🟡 建议）

- [ ] **Python 进阶**：装饰器/生成器/GIL/内存管理/类型注解（你项目大量使用）
- [ ] **Go 入门（可选，拓宽投递面）**：goroutine/channel 并发模型、与 Python asyncio 对比、用 Go 重写一个 mini HTTP 服务

---

## 两周冲刺节奏（建议）

| 天 | 主题 | 产出/验证 |
|---|---|---|
| 1-2 | MySQL 索引 + 事务 | 给项目表加索引 + EXPLAIN 记录 |
| 3-4 | Redis 原理 + 缓存三大问题 | 项目缓存改造笔记 + 防击穿设计 |
| 5-6 | Docker 镜像优化 + K8s 探针/滚动更新 | 优化 Dockerfile + 讲清单 |
| 7 | FastAPI 异步 + 阻塞陷阱 | 检查项目异步路径 |
| 8-9 | 网络/HTTP/JWT/限流 | 画出请求全链路图 |
| 10 | 安全体系化 + OWASP 映射 | 四层防线讲解稿 |
| 11 | 可观测（指标类型 + 告警） | 讲清 metrics.py 每个指标 |
| 12 | MQ 概念 + 审计异步化 MVP | 事件驱动 demo |
| 13 | AI 栈进阶（重排/评估） | RAG 进阶笔记 |
| 14 | Go 入门 / 总复习 | 面试自问自答一轮 |

---

## 面试串讲模板（每个知识点 3 句话）

> **讲项目**：我在 `xxx.py` 里做了 YYY，用来解决 ZZZ。
> **讲原理**：底层原理是……（B+树/事件循环/令牌桶……）
> **讲取舍**：为什么这样选而不是那样？（对比方案 + 数据/经验佐证）
