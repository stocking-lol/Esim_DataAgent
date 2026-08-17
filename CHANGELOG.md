# 更新日志 (Changelog)

本文档记录 eSIM NL2SQL Platform 的版本演进，遵循 [Keep a Changelog](https://keepachangelog.com/) 约定，版本号采用语义化版本 (SemVer)。

## [Unreleased] - 2026-08-12

### 新增 (Added)
- **自研 Mini Agent Runtime** — `app/core/mini_agent/`（约 800 行，34 项单测）：参考 Vanna 抽象思想、不依赖 Vanna 从零实现的轻量 Agent 底座，含 `ToolRegistry`（工具注册/访问组权限）、`SqlTool`（fail-closed 安全网关 + RLS + 超时；`dry_run` 用 sqlglot 模拟执行三级校验，可离线评估）、编排循环（检索→生成→执行→错误反馈→再生成，`max_iterations` 上限 + 错误分类决定是否重试 + 安全拦截不进入自愈）、对话记忆。
- **自研混合检索（Hybrid Search）** — `app/core/mini_agent/rag.py`：解决英文 embedding（all-MiniLM-L6-v2）对中文查询的检索漂移——向量召回大候选池后按关键词重叠（Jaccard）与主题包含加权重排，并做上下文总量控制（SQL 示例 > 文档 > DDL 的保留优先级）。
- **纯 LLM 直出对照组** — `app/core/mini_agent/naive.py`：单次调用、全量 schema 硬塞、无检索/无工具/无自愈，作为「非 Agent」对照基线。
- **三路对比评估** — `scripts/eval/compare_eval3.py`：同一 54 题评估集、同一 LLM（DeepSeek-V3）、仅改架构形态，对比「纯 LLM 直出 / 自研 Mini Agent / Vanna」。
- **训练数据扩充** — `scripts/init_training.py`：新增「用户档案→esim_profiles」「漫游套餐包→roaming_packages」映射文档与 SQL 示例（43 → 46 条）。

### 变更 (Changed)
- 测试套件从 **191 项**扩展至 **225 项**（新增 mini_agent 34 项），全部通过。
- README「技术选型」章节升级为四路对照（纯规则基线 / 纯 LLM 直出 / 自研 Mini Agent / Vanna），前 20 题对齐实测：EA 65% → 95% → **100%** → **100%**；自愈触发率 无 → 0% → **15%** → 0%（Vanna 首轮全对未触发，自愈能力存在；Mini Agent 首轮 85% + 自愈 15% 兜底至 100%）。数据见 `scripts/eval/compare_report3.json`。
- **LLM 重试退避加抖动（Full Jitter）** — `app/core/llm.py`：原纯指数退避（2s/4s 固定）在高并发下会导致所有超时请求**同一时刻**同步重试，形成重试风暴（thundering herd）冲击下游 API。改为 `delay = uniform(0, min(cap, base·2^(attempt-1)))`（AWS 推荐 Full Jitter），并设上限 `RETRY_MAX_DELAY=60s`、随机源可注入。模拟验证：100 个并发失败请求，最拥挤时刻从 100 个降到 3 个。新增 `tests/test_llm_backoff.py`（8 项，233 项总计）。
- **生产路径 LLM 重连（ResilientOpenAILlmService）** — `app/core/vanna_instance.py`：系统级断连演练发现核心 SQL 生成路径（Vanna Agent 流式调用）的 LLM 调用**无任何重试**，连接错误一次即败。新增 `ResilientOpenAILlmService(OpenAILlmService)` 包装 Vanna 的 LLM 服务，在建立流/非流式调用处复用 jittered 退避重连（APIConnectionError/RateLimitError 重试，其余错误不重试），流处理逻辑与父类一致。实测断连日志：`attempt 1/3 → retry in 0.09s (jittered) → attempt 2/3 → retry in 3.11s (jittered) → failed after 3 attempts`。新增 `tests/test_vanna_llm_resilience.py`（4 项，237 项总计）。
- **重试决策矩阵明确化（可重试 vs 需人工介入）** — `app/core/llm.py`：补上"非超时连接错误（连接被拒绝/DNS 失败）也纳入抖动重试"的漏网点（此前被误归为 API 错误不重试）；API 错误（4xx/5xx，如 API Key 无效、参数错误、配额不足）**一律不重试**，带完整错误信息抛出并提示人工检查配置。系统级验证：改坏 API Key（401）→ 0 次重试、2.07s 快速失败、日志完整留痕；断连（ConnectError）→ 3 次 jittered 重试、9.8s。新增 `tests/test_llm_backoff.py` 2 项（连接拒绝重试 + API 错误信息引导人工介入），239 项总计。
- **安全攻防用例扩充至 75 个** — `app/core/sql_security.py`：修复**注释拆分关键字绕过**漏洞（DRO/**/P == DROP，MySQL 会拼接执行——正则兜底按 MySQL 词法剥离注释拼接后命中危险词）；Prompt 注入补强（方括号 [SYSTEM] 指令前缀、角色扮演"数据库管理员"、"执行+危险动词"伪装指令）。`tests/test_security.py` 新增 30 项（危险操作全覆盖 / 库前缀与反引号绕过 / 编码与注释拆分 / CTE 与子查询隐藏 / Prompt 变体 / fail-closed 组合 / 结果检查层），239 → 269 项总计。
- **多轮重复评估（严格 A/B 支持）** — `scripts/eval/compare_eval3.py` 新增 `--trials N`：同一评估集重复跑 N 轮，输出 EA/EM/自愈率**均值±标准差**，消除 LLM 随机性对单次评估的影响。实测（20 题 × 3 轮）：Mini Agent EA **100.0±0.0%**、自愈率 **16.7±2.9%**（各轮 15/15/20——单轮值存在抽样波动）；naive EA 95.0%、自愈 0%。报告新增 `trials` 字段（各轮明细 + stats）。

## [0.7.0] - 2026-08-06

### 新增 (Added)
- **演示用例集** — `scripts/eval/demo_set.json`（18 个场景），覆盖基础 NL2SQL（7 类）、安全防护、RLS 租户隔离、数据脱敏、自我纠错、可视化；gold SQL 严格对照真实 eSIM schema。
- **演示运行器** — `scripts/demo.py`：支持离线静态展示（零 API 调用，并对基础查询对比「规则基线」与 Vanna 差异）与 `--live` 实时连线演示（POST `/api/v1/query`），可按 `--feature` 过滤。
- **演示集校验测试** — `tests/test_demo_set.py`（7 项）：校验演示集结构完整、gold SQL 可被 MySQL 方言解析、`category`/`feature` 合法、覆盖核心能力。
- **前端示例问题入口** — `frontend/app.js` 新增 `DEMO_QUESTIONS`，首页「示例问题」快捷入口由演示用例集动态填充（单一数据源）。
- **非 Vanna 的纯规则 NL2SQL 基线** — `app/core/nl2sql_baseline.py`（12 项单测）与 `scripts/eval/compare_eval.py`，用同一套评估集量化 Vanna 相对朴素方案的优越性（选型支撑）。

### 修复 (Fixed)
- **安全网关 fail-closed 加固** — `app/core/sql_security.py`：解析/校验失败（`ParseError`/`TokenError`）由原先的 fail-open（放行）改为 fail-closed（默认拦截），并增加基于正则的兜底校验（FROM/JOIN 表提取 + 白名单 + 危险函数 + `@@` 系统变量）；`vanna_instance.py` 安全校验异常同样 fail-closed。新增 `tests/test_security.py::TestFailClosed`（5 项）。

### 变更 (Changed)
- 测试套件从 **167 项**扩展至 **191 项**（新增 baseline 12 + demo 7 + fail-closed 5），全部通过；安全攻防用例从 40 增加到 45。
- README 新增「演示用例（快速体验）」章节与「技术选型：为什么用 Vanna」章节，并以可验证指标替换主观自夸表述。

### 安全 (Security)
- 安全网关对解析/校验失败采取 **fail-closed（默认拦截）** 策略，新增 5 项 fail-closed 攻防用例固化该行为。

---

## [0.6.0] - 2026-08-06

### 新增 (Added)
- **行级安全 (RLS)** — `app/core/rls_service.py`：基于 sqlglot AST 注入租户过滤条件，实现 MVNO 多租户数据隔离；运行时通过 `CapturingRunSqlTool.set_user_context(role, mvno_id)` 注入上下文，查询结束自动重置。
- **列级权限与脱敏**（Day 17）— 角色级列白名单（admin/analyst/viewer），viewer 对 `users`/`orders` 仅可见授权列；手机号、邮箱、ICCID 自动脱敏，并入查询全链路。
- **自我纠错回路**（Day 18）— `app/core/error_classifier.py` 将 SQL 错误分为 9 类（语法/表不存在/列不存在/歧义列/超时/权限/重复/连接/未知），其中 4 类可重试；`execute_query_with_retry()` 捕获可重试错误后用 LLM 纠错并自动重试，记录纠错历史。
- **查询结果可视化**（Day 19）— `app/services/visualization.py` 按数据形态自动推荐 bar/line/pie/table，并生成 Plotly HTML 图表随响应返回。
- **监控告警**（Day 20）— `app/middleware/metrics.py` 暴露 QPS、P95 延迟、安全拦截率、纠错率、查询准确率等业务指标（Prometheus `/metrics`，由 `ENABLE_METRICS` 开关控制）；新增 `monitoring/` 目录：Prometheus 抓取配置、5 条告警规则、Grafana 数据源与 5 面板仪表盘；`docker-compose.yml` 增加 prometheus + grafana 服务。
- **查询缓存**（Day 21）— `app/services/query_cache.py`：按 `(question, role, mvno_id)` 的 TTL 内存缓存（默认 60s，上限 200 条），规避大模型重复生成开销；`settings.py` 增加 `QUERY_CACHE_ENABLED` / `QUERY_CACHE_TTL_SECONDS` / `QUERY_CACHE_MAX_SIZE`。
- **评估基准**（Day 22）— `scripts/eval/`：54 题测试集（7 类 × 3 难度）+ 静态/在线两种评估模式，输出 Execution Accuracy + Exact Match 报告。
- `docker-compose.yml` 增加 prometheus、grafana 服务及数据卷。

### 修复 (Fixed)
- **审计表 schema drift（生产级隐患）**— 线上 `query_audit_log` 缺 `conversation_id` / `security_blocked` 列，导致 Day 12+ 审计静默失败；新增 `scripts/migrate_audit_columns.py` 执行 ALTER 修复，并同步 `scripts/init_db.sql`。
- **输入过滤前置** — 将注入检测移到 agent-init 检查之前，服务降级态也能拦截 Prompt/SQL 注入（防御纵深）。
- 纠错历史误记录成功结果（恒为 None）→ 引入 `last_failed_error` 变量。
- 表名不存在正则未处理引号（`'esim.usersss' doesn't exist` 误判为 UNKNOWN）→ 正则增加闭合引号匹配。
- 重试测试缓存 HIT 短路 → 增加 autouse `_clear_cache` fixture。
- `normalize_sql` 尾随空格 → 末尾 `.strip()`。
- 迁移脚本 `Engine.connect()` 缺 self、ModuleNotFoundError `app` → 改用 `db_manager._engine` 并补全 `sys.path`。

### 变更 (Changed)
- `app/services/query_service.py`：集成 RLS 上下文、缓存 HIT 短路、纠错重试、图表生成、脱敏后审计。
- `app/api/v1/query.py`：输入过滤前置；改用 `execute_query_with_retry()`；响应新增 `retry_count` / `corrections` / `chart`；拦截时标记 `http_request.state._security_blocked` 供指标中间件。
- `app/core/llm.py`：`correct_sql()` 扩展签名（错误信息 / 纠错提示 / DDL 参考）；新增 `generate_summary()`。
- `requirements.txt`：新增 `pytest-mock>=3.14.0`。
- 测试套件从 71 项扩展至 **167 项**（新增 RLS 20、masking 18、self_correction 26、visualization 14、monitoring 7、integration 6、eval 5），全部通过。

### 安全 (Security)
- 防御纵深加固：Prompt/SQL 注入检测在 agent 初始化检查之前执行，降级态仍拦截。
- 角色级列权限与 RLS 租户隔离共同约束数据可见范围。

---

## [0.5.0] - 2026-08-05

### 新增 (Added)
- 项目骨架：FastAPI 入口、配置管理、安全策略、统一异常处理、CORS。
- eSIM 数据库设计与种子数据（7 业务表 + 1 审计表）。
- Vanna 2.0 Agent 集成 + NL2SQL 查询 + SSE 流式。
- RAG 训练管理（ChromaDB + ONNX 语义检索，41 条领域知识）。
- SQL 安全网关（四层防御）、JWT 认证、限流、审计、数据脱敏。
- 多轮对话管理 + 上下文记忆。
- Prompt 注入检测、Schema Linking（BM25 + 角色权限）、SQL 校验增强（CTE 豁免 / 危险函数 / @@变量）。
- 审计日志 ORM + 审计中间件 + 分页查询；只读视图 + 查询超时 + 慢查询日志。
- 安全测试套件（40 项攻击场景）；完整用户认证体系（bcrypt + 注册/管理 CRUD）。
- 安全架构设计文档 `docs/security_architecture.md`。
- 测试基础设施与 71 项集成测试（test_query 17 + test_security 40 + test_schema_linking 14）。

---

## 版本说明
- `0.5.0`：基础平台 + 第一、二周功能（Day 1-15）。
- `0.6.0`：企业级安全与可观测性（Day 16-22），含 RLS、列级脱敏、自我纠错、可视化、监控告警、查询缓存、评估基准。
- `0.7.0`：安全网关 fail-closed 加固、非 Vanna 规则基线 + 对比评估（技术选型支撑）、演示用例集与运行器、前端示例入口，测试 167→191。
