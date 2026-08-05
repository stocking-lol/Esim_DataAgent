# Changelog

All notable changes to the eSIM NL2SQL Platform will be documented in this file.

## [0.5.0] - 2026-08-06

### Day 5 — SQL 安全网关 + 审计日志 + 限流 + JWT 认证

#### 新增
- **SQL 安全网关** (`app/core/sql_security.py`)
  - 4 层防御：输入过滤 → Schema 限制 → SQL 语法校验 → 结果检查
  - 基于 sqlglot 解析 SQL AST，拦截 DROP/DELETE/UPDATE/INSERT/TRUNCATE/ALTER 等非只读操作
  - 表白名单校验，阻止访问 mysql 系统表
  - JOIN 数量限制（默认 ≤5），防止性能攻击
  - 自动注入 LIMIT（默认 1000 行），防止全表扫描
  - 危险函数拦截：LOAD_FILE、SLEEP、BENCHMARK 等
  - 集成到 `CapturingRunSqlTool.execute()`，在 SQL 执行前拦截
- **审计日志服务** (`app/services/audit_service.py`)
  - 所有 NL2SQL 查询写入 `query_audit_log` 表
  - 记录用户、问题、生成 SQL、执行时间、行数、状态（success/blocked/error）
  - 审计统计 API：总查询数、成功率、平均耗时、拦截率
- **限流中间件** (`app/middleware/rate_limit.py`)
  - 滑动窗口算法，内存计数
  - query 端点 30 次/分钟，其他端点 60 次/分钟
  - 超限返回 429 + Retry-After 头
- **JWT 认证** (`app/core/auth.py`)
  - 令牌生成与验证（HS256）
  - FastAPI 依赖注入 `get_current_user` / `require_admin`
  - 演示账号：admin / analyst
  - 登录端点 `/api/v1/auth/login`，用户信息 `/api/v1/auth/me`
- **数据脱敏服务** (`app/services/masking_service.py`)
  - 手机号：`138****1001`
  - 邮箱：`z***@example.com`
  - ICCID/IMSI：保留前 4 后 4
  - 可配置字段规则，查询结果自动脱敏
- **管理员 API** (`app/api/v1/admin.py`)
  - `GET /admin/security/status` — 安全配置概览
  - `GET /admin/audit/stats` — 审计统计
  - `GET /admin/audit/logs` — 审计日志查询（分页）
- **测试脚本**
  - `scripts/test_security.py` — SQL 安全网关 9 项单元测试
  - `scripts/test_day5.py` — 端到端集成测试

#### 修改
- `app/core/vanna_instance.py` — `CapturingRunSqlTool.execute()` 增加 SQL 安全校验
- `app/services/query_service.py` — 集成输入过滤、数据脱敏、审计日志
- `app/api/v1/query.py` — 从 Request 提取 client IP 传入审计
- `app/api/v1/auth.py` — 完整登录/用户信息端点
- `app/api/v1/admin.py` — 安全状态 + 审计管理端点
- `app/api/deps.py` — JWT 依赖注入
- `app/main.py` — 注册限流中间件

#### 验证
- SQL 网关 9 项测试全部通过（DROP/DELETE/非白名单表/LOAD_FILE/SQL注入 等）
- JWT 登录 + 管理员端点正常
- 查询自动脱敏 `phone_number → 138****1001`
- SQL 注入 `'; DROP TABLE users; --` 被拦截（code=1001）
- 审计日志记录 success + blocked 两条记录

---

## [0.4.0] - 2026-08-05

### Day 4 — RAG 训练管理与知识注入

#### 新增
- **ChromaDB 训练存储** (`app/core/chroma_store.py`)
  - PersistentClient 持久化，3 个 Collection（DDL/Documentation/SQL）
  - 多级 Embedding fallback：ONNX → SentenceTransformer → 关键词检索
  - `SimpleKeywordEmbedding` 零依赖关键词检索兜底
- **训练管理服务** (`app/services/train_service.py`)
  - DDL/文档/SQL 示例的增删查
  - 训练统计
- **训练管理 API** (`app/api/v1/train.py`)
  - 6 个 REST 端点：POST DDL/DOC/SQL、GET 列表、DELETE 单条、GET 统计
  - 支持 batch 操作
- **eSIM 领域知识初始化** (`scripts/init_training.py`)
  - 7 张表 DDL + 14 条业务文档 + 20 条 SQL 示例 = 41 条训练数据
  - 涵盖用户管理、套餐订购、Profile 激活、流量使用、漫游等核心业务
- **查询上下文增强**
  - `vanna_instance.py` 新增 `retrieve_training_context()` 方法
  - 查询前从 ChromaDB 检索相关 DDL/文档/SQL，注入 Agent 上下文

#### 修改
- `app/core/vanna_instance.py` — 集成 ChromaDB，`initialize()` 改为 async
- `app/services/query_service.py` — 查询前注入训练上下文
- `app/main.py` — 启动时自动初始化训练数据（失败不阻塞）

#### 验证
- 训练数据 CRUD 全正常，41 条数据自动初始化
- 查询"各运营商上月的Profile激活转化率"生成精准 SQL（~4.7s）

---

## [0.3.0] - 2026-08-05

### Day 3 — Vanna 2.0 集成与 NL2SQL 查询

#### 新增
- **Vanna 2.0 Agent 单例** (`app/core/vanna_instance.py`)
  - `VannaAgentManager` 单例管理 Agent 生命周期
  - `OpenAILlmService` 对接 DeepSeek API（`deepseek-chat` 模型）
  - `MySQLRunner` 基于 pymysql 执行 SQL
  - 自定义 `CapturingRunSqlTool(RunSqlTool)` 捕获 LLM 生成的 SQL
  - `DemoAgentMemory` 内存模式（避免 ChromaDB ONNX 下载阻塞）
  - `AgentConfig(max_tool_iterations=30)`
- **LLM 服务封装** (`app/core/llm.py`)
  - `generate_sql` / `explain_sql` / `correct_sql` 备用方法
- **查询服务** (`app/services/query_service.py`)
  - `execute_query` 同步查询，解析 UiComponent（DataFrame/SimpleText）
  - `execute_query_stream` SSE 流式端点
- **查询 API** (`app/api/v1/query.py`)
  - `POST /api/v1/query` — 同步查询
  - `POST /api/v1/query/stream` — SSE 流式查询
  - `GET /api/v1/query/status` — 服务状态

#### 验证
- "本月新增多少eSIM用户" → COUNT 查询，1 行结果
- "哪个套餐订购量最高" → JOIN + GROUP BY，5 行结果
- "各运营商的活跃用户数" → LEFT JOIN + COUNT DISTINCT，3 行结果

---

## [0.2.0] - 2026-08-05

### Day 2 — 数据库设计与测试数据

#### 新增
- **数据库 Schema** (`scripts/init_db.sql`)
  - 8 张表：users, plans, orders, esim_profiles, data_usage, operators, roaming_packages, query_audit_log
  - 完整主键、外键、索引
  - utf8mb4 字符集
- **测试数据** (`scripts/seed_data.sql`)
  - 3 运营商、5 套餐、50 用户、80 订单、60 Profile、200 流量记录、3 漫游包
  - 共 401 行数据
- **SQLAlchemy 连接池** (`app/config/database.py`)
  - QueuePool 连接池配置
  - `get_db` 依赖注入
  - 连接事件监听

#### 修改
- `.env` — 更新数据库密码
- `docker-compose.yml` — MySQL 密码配置

---

## [0.1.0] - 2026-08-05

### Day 1 — 项目脚手架与 FastAPI 骨架

#### 新增
- **项目结构** — `app/` (config, api/v1, core, services, middleware, utils, models), `tests/`, `scripts/`, `frontend/`
- **依赖管理** — `requirements.txt`、`pyproject.toml`（ruff/black/pytest）
- **配置管理** (`app/config/settings.py`)
  - pydantic-settings，支持环境变量
  - 数据库、DeepSeek、ChromaDB、JWT、CORS 等配置项
- **安全策略** (`app/config/security.yaml`)
  - SQL 网关、JWT、限流、审计、脱敏策略定义
- **FastAPI 入口** (`app/main.py`)
  - CORS、全局异常处理、健康检查、lifespan 事件
- **自定义异常** (`app/utils/errors.py`)
  - 6 个异常类 + 4 个全局异常处理器
- **API 路由占位** — query, train, auth, conversation, admin
- **Docker Compose** — MySQL 8.0 + ChromaDB
- **环境配置** — `.env.example` / `.env`
