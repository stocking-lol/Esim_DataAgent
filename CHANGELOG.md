# 更新日志 (Changelog)

本文档记录 eSIM NL2SQL Platform 的版本演进，遵循 [Keep a Changelog](https://keepachangelog.com/) 约定，版本号采用语义化版本 (SemVer)。

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
