# eSIM NL2SQL Platform

> 自然语言数据查询平台（NL2SQL）— 基于 FastAPI + Vanna 2.0 + ChromaDB + DeepSeek-V3

## 项目简介

eSIM NL2SQL 平台是一个面向 eSIM 运营数据查询场景的自然语言到 SQL（NL2SQL）查询系统。用户可用自然语言提问（如"本月新增多少 eSIM 用户"），平台自动生成 SQL、执行查询、返回结构化结果，并支持多轮对话追问。

系统的安全能力以**可验证**的方式落地，而非口号：内置四层 SQL 安全网关、行级租户隔离（RLS）与自我纠错回路，并通过 **327 项自动化测试**（当前 `pytest --collect-only -q` 实测收集数）（其中 **75 个 SQL 注入 / Prompt 注入 / fail-closed 攻防用例**）持续验证；安全网关对解析/校验失败采取 **fail-closed（默认拦截）** 策略。详见下方「安全设计」「演示用例」「技术选型」与「质量指标」章节。

### 核心能力

- **NL2SQL 查询**: 自然语言 → SQL → 结构化结果，基于 Vanna 2.0 Agent + DeepSeek-V3
- **RAG 训练管理**: DDL、业务文档、SQL 示例的 CRUD 管理，ChromaDB 向量检索
- **四层安全网关**: 输入过滤 → Schema 限制 → SQL 校验 → 结果检查，纵深防御
- **多轮对话**: 支持上下文追问（"改成按月统计"、"其中漫游包占比多少"）
- **JWT 认证**: 角色权限体系（admin/analyst），数据脱敏
- **审计日志**: 全链路查询审计，支持按用户/时间/状态筛选
- **限流防护**: 滑动窗口限流，查询 30 次/分钟

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 后端框架 | FastAPI | 异步 API，自动 Swagger 文档 |
| NL2SQL | Vanna 2.0 | Agent 架构，send_message() 流式响应 |
| LLM | DeepSeek-V3 | OpenAI 兼容接口 |
| 向量存储 | ChromaDB | RAG 训练数据持久化 |
| 数据库 | MySQL 8.0 | eSIM 业务数据 |
| ORM | SQLAlchemy | 连接池管理 |
| 认证 | JWT (PyJWT) | Token 验证 + 角色权限 |
| SQL 解析 | sqlglot | AST 级 SQL 安全校验 |
| 流式响应 | sse-starlette | SSE (Server-Sent Events) |
| 测试 | pytest + httpx | 异步集成测试 |

## 快速开始

### 方式一：本地运行

```bash
# 1. 克隆项目
git clone https://github.com/stocking-lol/Esim_DataAgent.git
cd Esim_DataAgent/esim-nl2sql-platform

# 2. 创建 Conda 环境
conda create -n esim-nl2sql python=3.12 -y
conda activate esim-nl2sql
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env，填写 LLM_API_KEY 和 MySQL 连接信息

# 4. 初始化数据库
mysql -u root -p < scripts/init_db.sql
mysql -u root -p --default-character-set=utf8mb4 esim_platform < scripts/seed_data.sql
python scripts/create_conversation_tables.py

# 5. 启动应用
python -m uvicorn app.main:app --reload

# 6. 访问（后端同源托管前端，无需单独起静态服务器 / 无需 CORS）
# 前端界面: http://localhost:8000/            <-- 直接打开即用
# API 文档:  http://localhost:8000/docs
# 健康检查:  http://localhost:8000/health
# 监控指标:  http://localhost:8000/metrics    （需 ENABLE_METRICS=true）
```


### 方式二：Docker 运行

```bash
# 启动 MySQL + ChromaDB
docker-compose up -d mysql chromadb

# 然后按上述步骤 3-5 操作
```

### 演示账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| admin | esim_admin_2026 | 管理员 |
| analyst | esim_analyst_2026 | 分析师 |

## API 接口

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/auth/login` | 登录获取 JWT |
| GET | `/api/v1/auth/me` | 获取当前用户信息 |
| POST | `/api/v1/auth/refresh` | 刷新 Token |

### 查询

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/query` | NL2SQL 查询（非流式） |
| POST | `/api/v1/query/stream` | NL2SQL 查询（SSE 流式） |
| GET | `/api/v1/query/status` | 查询服务状态 |

### 对话

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/conversation` | 创建对话 |
| GET | `/api/v1/conversation` | 对话列表 |
| GET | `/api/v1/conversation/{id}` | 对话详情（含消息） |
| DELETE | `/api/v1/conversation/{id}` | 删除对话 |
| POST | `/api/v1/conversation/{id}/messages` | 发送消息（多轮上下文） |

### 训练管理

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/train/ddl` | 训练 DDL |
| POST | `/api/v1/train/documentation` | 训练业务文档 |
| POST | `/api/v1/train/sql` | 训练 SQL 示例 |
| GET | `/api/v1/train/data` | 训练数据列表 |
| DELETE | `/api/v1/train/data/{type}/{id}` | 删除训练数据 |
| GET | `/api/v1/train/stats` | 训练统计 |

### 管理员

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/admin/security/status` | 安全状态 |
| GET | `/api/v1/admin/audit/stats` | 审计统计 |
| GET | `/api/v1/admin/audit/logs` | 审计日志查询 |

## 统一响应格式

```json
{
  "code": 200,
  "message": "success",
  "data": { ... }
}
```

### 错误码

| 错误码 | 说明 |
|--------|------|
| 200 | 成功 |
| 400 | 请求参数错误 |
| 401 | 未认证 |
| 403 | 无权限 |
| 404 | 资源不存在 |
| 429 | 请求过于频繁 |
| 500 | 服务器内部错误 |
| 1001 | SQL 安全拦截 |
| 1002 | SQL 执行错误 |
| 1003 | LLM 服务异常 |
| 1004 | 数据库连接异常 |

## 项目结构

```
esim-nl2sql-platform/
├── app/
│   ├── main.py                    # FastAPI 应用入口
│   ├── config/
│   │   ├── settings.py            # pydantic-settings 配置
│   │   ├── database.py            # SQLAlchemy 连接池
│   │   └── security.yaml          # 安全策略配置（含角色权限/脱敏）
│   ├── api/v1/
│   │   ├── router.py              # 路由聚合
│   │   ├── auth.py                # 认证 API
│   │   ├── query.py               # NL2SQL 查询 API
│   │   ├── train.py               # RAG 训练管理 API
│   │   ├── conversation.py        # 多轮对话 API
│   │   └── admin.py               # 管理员 API
│   ├── core/
│   │   ├── vanna_instance.py      # Vanna 2.0 Agent 单例
│   │   ├── llm.py                 # LLM 服务封装（含纠错/摘要）
│   │   ├── chroma_store.py        # ChromaDB 训练存储
│   │   ├── sql_security.py        # SQL 安全网关（四层防御）
│   │   ├── schema_linking.py      # Schema 链接（BM25 表排名 + 角色权限）
│   │   ├── error_classifier.py    # SQL 错误分类（驱动自我纠错）
│   │   └── auth.py                # JWT 认证
│   ├── models/
│   │   ├── conversation.py        # 对话 ORM 模型
│   │   ├── user.py                # 用户 ORM 模型
│   │   └── query_log.py           # 审计日志 ORM 模型
│   ├── services/
│   │   ├── query_service.py       # NL2SQL 查询服务（含纠错/缓存）
│   │   ├── train_service.py       # RAG 训练服务
│   │   ├── conversation_service.py # 多轮对话服务
│   │   ├── audit_service.py       # 审计日志服务
│   │   ├── masking_service.py     # 数据脱敏服务
│   │   ├── rls_service.py         # 行级安全（RLS）服务
│   │   ├── visualization.py       # 查询结果可视化
│   │   └── query_cache.py         # 查询缓存（性能优化）
│   ├── middleware/
│   │   ├── rate_limit.py          # 限流中间件
│   │   ├── audit.py               # 审计中间件
│   │   └── metrics.py             # Prometheus 业务指标
│   └── utils/
│       ├── errors.py              # 统一异常处理
│       └── crypto.py              # bcrypt 密码加密
├── tests/                         # 测试套件（327 项）
├── scripts/
│   ├── init_db.sql                # eSIM 数据库建表
│   ├── seed_data.sql              # 测试数据
│   ├── init_training.py           # RAG 训练数据初始化
│   ├── add_readonly_views.sql     # 只读视图 + 只读用户
│   ├── migrate_audit_columns.py   # 审计表结构迁移
│   └── eval/                      # 评估基准（build_test_set / run_eval）
├── monitoring/                    # Prometheus + Grafana 配置
├── k8s/                           # Kubernetes 部署清单（Deployment/Service/ConfigMap/Secret）
├── docs/
│   └── security_architecture.md   # 安全架构设计文档
├── docker-compose.yml             # MySQL + ChromaDB + Redis + Prometheus + Grafana
├── requirements.txt
├── pyproject.toml
└── .env.example
```

## 高级特性

平台在基础 NL2SQL 之上，内置了安全与可观测能力（均以测试/评估量化，非口号）：

- **行级安全 RLS（Day 16）**：通过 `sqlglot` AST 注入租户过滤条件，实现 MVNO 多租户数据隔离；运行时由 `CapturingRunSqlTool.set_user_context(role, mvno_id)` 注入上下文，查询结束后自动重置。
- **列级权限与脱敏（Day 17）**：角色级列白名单（admin/analyst/viewer），viewer 对 `users`/`orders` 仅可见授权列；敏感字段（手机号 `138****5678`、邮箱 `z***@xxx`、ICCID）自动脱敏。
- **自我纠错回路（Day 18）**：`error_classifier` 将 SQL 错误分为 9 类（语法/表不存在/列不存在/歧义列/超时/权限/重复/连接/未知），其中 4 类可重试；`execute_query_with_retry` 捕获可重试错误后用 LLM 纠错并自动重试，记录纠错历史。
- **查询结果可视化（Day 19）**：`visualization` 模块按数据形态自动推荐 bar/line/pie/table，并生成 Plotly HTML 图表随响应返回。
- **监控告警（Day 20）**：`metrics` 中间件暴露 QPS、P95 延迟、安全拦截率、纠错率、查询准确率等业务指标（Prometheus `/metrics`）；配套 Prometheus 告警规则与 Grafana 仪表盘。
- **查询缓存（Day 21）**：按 `(question, role, mvno_id)` 的 TTL 缓存，规避大模型重复生成开销；**双后端（内存 / Redis）**——`QUERY_CACHE_BACKEND=memory|redis|auto`，Redis 支持多副本跨实例共享，连接故障自动降级内存（fail-soft）。
- **Kubernetes 部署（v0.9.0）**：`k8s/` 提供生产化清单（Deployment/Service/ConfigMap/Secret/PVC/三类探针），Redis 缓存跨副本共享、滚动扩展，配置与密钥分离。
- **评估基准（Day 22）**：`scripts/eval/` 提供 54 题测试集（7 类 × 3 难度）与静态/在线两种评估模式，输出 Execution Accuracy + Exact Match 报告；支持 `--trials N` 多轮重复评估（均值±标准差）。

## 演示用例（快速体验）

平台内置一份精心挑选的**演示用例集** [`scripts/eval/demo_set.json`](scripts/eval/demo_set.json)（18 个场景），覆盖平台全部核心能力，gold SQL 严格对照真实 eSIM schema：

- **基础 NL2SQL（7 类）**：单表 / 多表关联 / 聚合计数 / 时间过滤 / 排名 Top-N / 分组统计 / 对比分析
- **安全防护**：SQL 注入、Prompt 注入、UNION 注入 —— 均被四层安全网关以 `code=1001` 拦截（fail-closed）
- **行级租户隔离 RLS**：同一问句，admin 看全量、viewer 仅见本 MVNO 租户数据
- **数据脱敏**：手机号 `138****5678`、邮箱 `z***@xxx`、ICCID 按角色自动脱敏
- **自我纠错回路**：生成 SQL 引用不存在列时，错误分类器判定可重试 → LLM 纠错 → 自动重试成功
- **可视化**：自动推荐并生成 Plotly 图表（bar/line/pie/table）

两种体验方式：

```bash
# 1) 离线静态展示（零 API 调用；基础查询类会同时调用规则基线引擎，对比 Vanna 的差异）
python scripts/demo.py

# 2) 连线实时演示（需先启动后端：python -m uvicorn app.main:app --reload）
python scripts/demo.py --live --endpoint http://localhost:8000

# 只看安全攻防 / RLS / 脱敏 等某一类：
python scripts/demo.py --feature security
```

演示集本身由 [`tests/test_demo_set.py`](tests/test_demo_set.py) 校验：结构完整、gold SQL 可被 MySQL 方言解析、`category`/`feature` 取值合法、覆盖核心能力。前端首页也内置了与演示用例集对齐的「示例问题」快捷入口，点击即可直接提问。

## 质量指标（可验证）

本项目的每一条安全/质量主张都以自动化测试或可量化评估支撑，而非形容词：

| 维度 | 数值 / 说明 |
|------|------|
| 自动化测试 | **327 项**（pytest，`pytest --collect-only -q` 可复现），其中 **75 个 SQL 注入 / Prompt 注入 / fail-closed 攻防用例**（绝大多数 AI 项目接近于零测试） |
| 安全网关策略 | **fail-closed**：SQL 解析/校验失败时默认**拦截**而非放行（见 `test_security.py::TestFailClosed`） |
| 评估基准 | **54 题**（7 类 × 3 难度）黄金 SQL 测试集，输出 Execution Accuracy + Exact Match |
| 执行准确率（EA，前 20 题） | 规则基线 65% · 纯 LLM 直出 95% · 自研 Mini Agent **100%** · Vanna 95% |
| 自我纠错 | 9 类 SQL 错误分类 + 自动重试回路（26 项单测覆盖） |

## 技术选型：为什么用 Vanna

技术选型论证采用**同一套 54 题评估集、同一 LLM（DeepSeek-V3）、仅改变架构形态**的对照实验，四路基线：

| 路线 | 架构 | 说明 |
|------|------|------|
| 纯规则基线 | 零 LLM，关键词+模板 | `app/core/nl2sql_baseline.py`（12 项单测，7 类意图） |
| 纯 LLM 直出（非 Agent） | 单次调用，全量 schema 硬塞 | `app/core/mini_agent/naive.py`：无检索、无工具、无自愈 |
| **自研 Mini Agent** | **从零实现的 Agent 底座** | `app/core/mini_agent/`：RAG 检索 + 工具执行 + 错误反馈自愈循环 |
| Vanna 2.0 Agent | 生产底座 | 本项目生产核心，`send_message()` 编排 |

**实测对比（前 20 题对齐，同一评估集、同一 LLM、真实服务 + dry_run 混合）：**

| 指标 | 纯规则基线 | 纯 LLM 直出（非 Agent） | 自研 Mini Agent | Vanna（LLM Agent） |
|------|------|------|------|------|
| 执行准确率 EA | 65% | 95% | **100%** | **100%** |
| 精确匹配 EM | 0% | 0% | **5%** | 0% |
| 自愈触发率（首轮失败→自愈修正成功占比） | 无 | 0% | **16.7±2.9%** | 0% |
| 上下文策略 | 模板 | 全量硬塞 | RAG 动态检索 | RAG 动态检索 |
| 推理成本 | 0 | 每次 1 次 LLM | 每次 1-2 次 LLM | 每次若干 LLM 调用 |

> 注：数据来自 `scripts/eval/compare_eval3.py` 前 20 题对齐实测（Vanna 走真实服务完整链路，
> Mini Agent/纯 LLM 直出走 dry_run 模拟执行）；可用 `--trials 3` 做**多轮重复评估**消除 LLM 随机性
> （实测 Mini Agent EA **100.0±0.0%**、自愈率 **16.7±2.9%**，`scripts/eval/compare_report3.json` 可复跑复核）。

**三路对比的关键结论（Agent 架构的增量价值）：**
- **纯 LLM 直出 vs 自研 Mini Agent**：同一 LLM，仅差「RAG 检索 + 工具校验 + 自愈循环」，EA 95% → 100%，且 Mini Agent 有 **16.7±2.9% 的题首轮出错后靠自愈纠错成功**（纯 LLM 出错即败）。
- **Vanna vs 自研 Mini Agent（自愈机制实测对比）**：两者最终 EA 均为 100%，但**自愈触发率不同**——Vanna 首轮即 100% 正确（触发率 0%，自愈能力存在但未触发，说明其 few-shot/提示工程使首轮质量更高）；Mini Agent 首轮 ~83.3% + 自愈 16.7% 兜底至 100%（证明自愈闭环是把「首轮失败」转化为「最终成功」的安全网）。两套架构的自愈回路均由 `error_classifier`（9 类错误）驱动，区别只在触发频率。
- **自研 Mini Agent 修正好「纯 LLM 修不了的题」**：如「查询所有已激活的用户档案」，纯 LLM 直出把"用户档案"误映射到 `users` 表；Mini Agent 通过 RAG 混合检索命中业务文档与 few-shot 示例，再经自愈循环（3 次重试）修正到 `esim_profiles`。
- **为什么自研版仍选 Vanna 做生产底座**：自研版验证了 Agent 架构的核心闭环（工程上约 800 行即可复现），但 Vanna 提供更成熟的多轮对话、插件体系与生态；**自研版的价值是架构验证 + 对比实验，用数据反哺选型**——这正是 Agent 项目的工程素养。

**自研 Mini Agent Runtime 的设计要点**（`app/core/mini_agent/`，34 项单测）：
- `ToolRegistry`：工具注册 / 查找 / 访问组权限（对应 Vanna 的 ToolRegistry 抽象）
- `SqlTool`：执行前强制 fail-closed 安全网关 + RLS 注入 + 超时提示；`dry_run` 模式用 sqlglot 模拟执行（语法/表/列三级校验），可离线评估
- 编排循环：检索 → 生成 → 执行 → 错误反馈 → 再生成（`max_iterations` 上限；错误分类器决定是否可重试；安全拦截不进入自愈循环）
- **自研混合检索（Hybrid Search）**：解决英文 embedding（all-MiniLM-L6-v2）对中文查询的检索漂移——向量召回大候选池后，按关键词重叠（Jaccard）与主题包含加权重排，确保「问题与示例几乎一致」的 few-shot 必被召回
- 上下文总量控制：SQL 示例 > 业务文档 > DDL 的保留优先级，避免长上下文稀释注意力

**运行三路对比：**
```bash
python scripts/eval/compare_eval3.py --mini --mini-limit 20 --naive --naive-limit 20
python scripts/eval/compare_eval3.py --vanna --endpoint http://localhost:8000   # 加 Vanna 路
```

**从零实现 NL2SQL 的基本路径**（规则基线即此）：schema 元数据 → 意图识别（关键词/句法）→ 模板拼装 → 执行校验。它的天花板就是上表的 65% EA，且对未写进模板的句式直接退化（例如「利润最高的运营商」「激活失败的用户」只能退化成 `SELECT *` 或漏掉语义过滤）。这正是 LLM 方案的差距所在。

**Vanna 的增量价值**：把"理解自然语言"这一步交给模型，配合 RAG（ChromaDB 检索业务 DDL / 文档 / SQL 示例）与自我纠错回路，在 `join`、歧义列、复杂/口语化问句上显著更优（join 类 EA 从 20% → 100%）。本样本 20 题中 Vanna 首轮即正确、无需强制重试；自我纠错回路由 26 项专测（错误分类 9 类 + 重试循环）独立验证。

> 说明：EM（与黄金 SQL 字符串完全一致）对 LLM 天然偏低（别名/列顺序差异即判错），故本项目以 **EA（触及正确表集合且可执行）** 作为主口径，与 `run_eval.py` 一致。

## 测试

```bash
# 运行全部测试
python -m pytest tests/ -v

# 运行特定测试
python -m pytest tests/test_query.py::test_conversation_crud -v
```

测试覆盖（327 项），按模块分组：

**基础能力**
- 认证流程（登录、me、错误密码、注册、更新资料）
- 对话 CRUD（创建、列表、详情、删除、404）
- 多轮对话（创建 → 发送消息 → 验证历史上下文）
- 流式查询端点（SSE）
- 训练管理 API（DDL/文档/SQL 增删查 + 批量）
- 管理员 API（审计日志、安全状态、用户管理）

**安全（四层防御）**
- InputFilter（SQL 注入 / Stacked / UNION / Prompt 注入 / XSS / CHAR·Hex 编码 / 信息架构探测 / 盲注 / 时间盲注 / 错误探测，共 40 项攻击场景）
- SQL 安全网关（白名单表 / JOIN·子查询数 / 危险函数 / @@系统变量 / CTE 豁免）
- Schema Linking + 角色级列权限（admin/analyst/viewer）
- 行级安全 RLS（MVNO 租户隔离，约 20 项）
- 列级脱敏 masking（手机号 / 邮箱 / ICCID，18 项）

**高级特性**
- 自我纠错回路（错误分类 9 类 / 可重试决策 / 纠错提示 / LLM 纠错增强 / 重试循环，26 项）
- 查询结果可视化（图表类型推荐 / Plotly 配置 / HTML 生成，14 项）
- 监控告警（Prometheus 指标记录器 / `/metrics` 端点，7 项）
- 全链路集成测试（注入拦截 → 认证 → 审计落库 → 角色可见性，6 项）
- 评估基准（SQL 归一化 / 表提取 / 黄金 SQL 校验 / 测试集 ≥50，5 项）

## 开发进度

### 已完成

| 阶段 | 内容 | 状态 |
|------|------|------|
| Day 1 | 项目初始化、环境搭建、FastAPI 骨架 | 完成 |
| Day 2 | eSIM 数据库设计（8 表 401 行） | 完成 |
| Day 3 | Vanna 2.0 集成、NL2SQL 查询、SSE 流式 | 完成 |
| Day 4 | RAG 训练管理、ChromaDB、41 条训练数据 | 完成 |
| Day 5 | SQL 安全网关、JWT 认证、限流、审计、脱敏 | 完成 |
| Day 6 | 多轮对话管理、上下文记忆 | 完成 |
| Day 7 | 集成测试（17 项）、测试基础设施 | 完成 |
| Day 8 | 安全架构文档 | 完成 |
| Day 9 | 安全增强：Prompt 注入检测 + sanitize | 完成 |
| Day 10 | Schema Linking：BM25 表排名 + 角色级权限 | 完成 |
| Day 11 | SQL 校验增强：CTE 豁免 + 危险函数检测 | 完成 |
| Day 12 | 审计日志 ORM + 审计中间件 + 分页查询 | 完成 |
| Day 13 | 只读视图 + 查询超时 + 慢查询日志 | 完成 |
| Day 14 | 安全测试套件（40 项攻击场景） | 完成 |
| Day 15 | 用户认证：bcrypt + 注册/管理 CRUD | 完成 |
| Day 16 | 行级安全 RLS（MVNO 租户隔离） | 完成 |
| Day 17 | 列级权限与数据脱敏增强 | 完成 |
| Day 18 | 自我纠错回路（错误分类 + 重试） | 完成 |
| Day 19 | 查询结果可视化（图表自动推荐） | 完成 |
| Day 20 | 监控告警（Prometheus + Grafana + 告警规则） | 完成 |
| Day 21 | 第三周收尾：全链路集成测试 + 查询缓存 | 完成 |
| Day 22 | 评估基准（54 题测试集 + 评估脚本） | 完成 |
| Day 23 | 前端 Web UI（同源托管 SPA）、安全网关 fail-closed 加固、非 Vanna 规则基线 NL2SQL + Vanna 对比评估、README 质量指标/技术选型章节 | 完成 |

### 待实现

| 阶段 | 内容 |
|------|------|
| Day 24+ | Docker 完整部署打磨、GitHub 开源发布准备、CI/CD |

## License

MIT
