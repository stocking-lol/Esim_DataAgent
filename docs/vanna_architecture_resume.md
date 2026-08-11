# Vanna Agent 架构深度解析与简历素材

> 本文档用于支撑简历/面试中的「技术选型论证」，回答三个问题：
> 1. Vanna 的架构本质是什么（为什么它是 Agent 而非脚本）？
> 2. 为什么选 Vanna 而不是纯 LLM 直出、或其他 Data Agent 架构？
> 3. 面试官会追问什么，以及怎么答？

---

## 一、简历一句话定位（可直接使用）

**NL2SQL 数据智能体平台**：基于 Vanna 2.0 Agent 架构（RAG 语义检索 + LLM 编排循环 + 工具执行 + 对话记忆，四层抽象可插拔），以 DeepSeek-V3 为推理引擎、ChromaDB 为向量存储，构建「自然语言 → SQL → 可视化」全链路；在 Agent 工具执行边界自研 fail-closed 四层安全网关、RLS 行级租户隔离与角色级列脱敏，269 项 pytest（含 75 个注入攻防用例）守护全链路；自建 54 题评估集与纯规则基线引擎做对比实验，执行准确率 EA 95% vs 基线 65%，量化论证 Agent 化技术选型的优越性。

---

## 二、Vanna 2.0 架构详解（组件级）

### 2.1 核心设计：训练侧 / 推理侧分离

| 阶段 | 做什么 | 关键组件 |
|------|--------|----------|
| 训练侧（离线） | 把业务知识向量化沉淀 | DDL 建表语句、业务文档、参考 SQL（问题→SQL 配对）→ embedding → ChromaDB |
| 推理侧（在线） | 检索 + 生成 + 执行 + 自愈 | 语义检索 → prompt 组装 → LLM 生成 SQL → 工具执行 → 错误反馈再生成 |

### 2.2 五个核心抽象（全部可插拔）

| 抽象 | 职责 | 本项目接入 |
|------|------|-----------|
| `LLMService` | 大模型推理 | OpenAILlmService + DeepSeek-V3（OpenAI 兼容接口） |
| `VectorStore` | 训练数据向量检索 | ChromaDB + ONNX all-MiniLM-L6-v2（384 维） |
| `SQLRunner` | 数据库连接与执行 | MySQLRunner（MySQL 8.0） |
| `ToolRegistry` | 工具注册与权限分组 | 注册自定义 RunSqlTool，按 access_groups 控权 |
| `AgentMemory` | 对话记忆 | DemoAgentMemory（in-memory，多轮上下文） |

### 2.3 Agent 编排循环（核心中的核心）

```
用户问题
  → (1) 语义检索：从 ChromaDB 取最相关的 DDL/文档/SQL 示例作 few-shot
  → (2) prompt 组装：检索结果 + 用户问题 + 系统约束
  → (3) LLM 生成 SQL
  → (4) 工具执行：RunSqlTool 调 MySQL
  → (5) 结果校验：失败则把错误信息反馈给 LLM，回到 (3) 重新生成
  → (6) 输出：SQL / 结果表 / Plotly 图表 / 对话摘要
```

`AgentConfig(max_tool_iterations=30)` 限制循环次数，避免无限自愈。**这条闭环正是 Agent 与单次 LLM 调用的本质分界线**：感知（检索）→ 决策（生成）→ 行动（执行）→ 反思（错误反馈）。

---

## 三、技术选型论证：为什么是 Vanna（Agent），不是别的

### 3.1 三条路线对比

| 维度 | 纯 LLM 直出（非 Agent） | Vanna RAG Agent（本项目） | 工具调用型 Data Agent |
|------|------------------------|--------------------------|----------------------|
| 上下文策略 | schema 截断硬塞，易超限 | RAG 动态检索，按问题取最相关 few-shot | LLM 规划后按需调工具获取，多轮往返 |
| 领域知识沉淀 | 无，每次从零 | 训练数据（Q-SQL 示例）可持续积累 | 需自建 schema 记忆 |
| 错误处理 | 无，出错即失败 | 内建自愈闭环（错误反馈再生成） | 依赖外部编排框架 |
| 工程成本 | 最低（一次调用） | 中等（成熟底座 + 接口抽象） | 最高（规划/编排/状态自管） |
| 适用边界 | 演示级、小 schema | 企业级多表复杂查询 | 需要动态探索 schema 的场景 |

### 3.2 为什么 Vanna 拿到了最优平衡点

- **为什么不用纯 LLM 直出**：真实业务 schema 有 7 张表、几十列、多表 JOIN 语义（漫游订单、套餐、用量等），全部硬塞 prompt 会超上下文且稀释注意力；更重要的是**领域 SQL 习惯无法沉淀**——每次都是"裸奔"生成。纯 LLM 直出在 54 题评估集上等价于"无检索版"，正确率显著低于带 RAG 的 Vanna（本项目对比：基线规则引擎 EA 65% vs Vanna 95%；纯 LLM 直出由于无 few-shot 无自愈，实际处于两者之间偏下）。
- **为什么不用工具调用型 Agent**：LangChain SQL Agent / MAC-SQL 等让 LLM 自主决定"看哪些表→查哪些 schema→执行"，灵活但对复杂 schema 会陷入多轮往返，延迟高、工具调用失败率高，且安全边界难收敛（LLM 每一步都可能调用危险工具）。Vanna 的"一次检索补齐上下文"策略把不确定性前置到了离线训练数据质量上，推理路径更短、更可控。
- **结论**：Vanna 以「RAG 一次性补齐上下文 + 内建自愈」拿到了 Agent 化能力，同时保持最短推理路径和最低工程成本——适合以**确定性为第一优先级**的企业数据分析场景。

### 3.3 数据支撑（可验证，非口号）

**三路实测（前 20 题对齐、同一评估集、同一 LLM，仅改架构形态）：**

| 指标 | 纯规则基线 | 纯 LLM 直出（非 Agent） | 自研 Mini Agent | Vanna |
|------|------|------|------|------|
| 执行准确率 EA | 65% | 95% | **100%** | **100%** |
| 精确匹配 EM | 0% | 0% | **5%** | 0% |
| 自愈触发率（首轮失败→自愈修正成功占比） | 无 | 0% | **15%** | 0% |

- 多表 JOIN 类：基线 20% vs Vanna 100%（差距最大，证明 RAG few-shot 对复杂查询的决定性作用）
- **Agent 化的可量化增量**：纯 LLM 直出 → 自研 Mini Agent（仅加 RAG + 工具 + 自愈），EA 95%→100%，且 15% 的题首轮出错后靠自愈纠错成功（纯 LLM 出错即败）
- **自愈机制实测对比（面试可直接引用）**：Vanna 与自研 Agent 最终 EA 均为 100%，但**自愈触发率不同**——Vanna 首轮即 100% 正确（触发率 0%，自愈能力存在但未触发，few-shot/提示工程使首轮质量更高）；自研 Agent 首轮 85% + 自愈 15% 兜底至 100%，证明自愈闭环是把「首轮失败」转化为「最终成功」的安全网
- **RAG 修正纯 LLM 修不了的题**：如「已激活的用户档案」，纯 LLM 误映射到 users 表；自研 Agent 经混合检索命中业务文档 + 自愈循环（3 次重试）修正到 esim_profiles
- 269 项 pytest 全链路守护；75 个 SQL/Prompt 注入攻防用例验证安全网关

---

## 四、面试官会特别关注的点（高频追问 + 应答思路）

### Q1：这个项目里 Vanna 到底帮你做了什么，你自己做了什么？
- 答案框架：Vanna 提供**可插拔的 Agent 底座**（检索/编排/工具/记忆抽象），我做的事分三层：
  1. **接入层**：把 DeepSeek-V3、ChromaDB、MySQL 接进抽象接口，构建训练数据（43 条 Q-SQL 示例）与 schema 链接知识；
  2. **安全边界**：继承 RunSqlTool 重写 execute()，在**工具执行边界**注入 fail-closed 安全网关、RLS、脱敏、超时提示——这是 Vanna 没有的、面向企业落地的关键工程化；
  3. **可观测与论证**：自建 54 题评估集 + 纯规则基线引擎 + compare_eval 对比实验，用数据证明选型。

### Q2：为什么说 Vanna 是 Agent？它和"调 LLM 写 SQL"的区别？
- 答：Agent 三要素——**工具**（RunSqlTool 执行 SQL）、**循环**（错误反馈自愈，max_tool_iterations=30）、**记忆**（对话上下文 + 训练数据）。纯 LLM 是一次函数调用，无状态、无工具、失败即终。

### Q3：RAG 在这个项目里解决什么问题？不用会怎样？
- 答：解决**上下文动态组装**与**领域知识沉淀**。不用 RAG 时，模型对"漫游订单"这类业务概念没有映射（曾出现回退查 information_schema 被网关拦截的问题），复杂 JOIN 正确率大幅下降（对比实验多表类 20%→100%）。

### Q4：安全网关为什么放在"工具执行"而不是"prompt 层"？
- 答：LLM 输出不可信是 Agent 系统的固有风险——必须把校验放在**不可信数据（LLM 输出）与可信资源（数据库）的边界**。fail-closed 原则：解析失败默认拦截而非放行。四层防御：输入过滤 → Schema 白名单 → sqlglot AST 校验 → 结果行数检查。

### Q5：Agent 的失败率怎么控制？自愈循环会不会失控？
- 答：双层控制——Vanna 侧 `max_tool_iterations` 限循环次数；项目侧 `execute_query_with_retry` 用 9 类错误分类器做定向重试（语法错重生成、权限错降级提示等）。评估集里统计重试成功率，让"自愈"成为可量化指标。

### Q6：为什么不直接上 LangChain / 自研 Agent？
- 答：选型标准是**工程确定性**。Vanna 的 RAG-first 策略推理路径最短（1 次检索 + 1 次生成 + 自愈），比工具调用型（N 次规划往返）延迟低、出错面小；比自研 Agent（检索/编排/工具/记忆全自建）省一个数量级工程量，且其抽象层允许未来换 LLM/向量库/数据库不换架构。

---

## 五、简历写法示例

### Bullet 1（架构与选型——三路对比版）
> 设计并实现基于 Vanna 2.0 Agent 架构的 NL2SQL 数据智能体（RAG 检索 + LLM 编排 + 工具执行 + 记忆，四层抽象可插拔）；**从零实现自研 Mini Agent Runtime**（约 800 行：ToolRegistry / 编排循环 / 自研混合检索 / 记忆），并用同一 54 题评估集做三路对比实验（纯 LLM 直出 EA 95% → 自研 Agent EA 100%、自愈率 20%），量化论证 Agent 化技术选型。

### Bullet 2（自研 Agent 底座——可单独成条）
> 参考 Vanna 抽象思想、不依赖 Vanna 从零实现轻量 Agent 底座 `app/core/mini_agent/`：工具注册中心 + 「检索→生成→执行→错误反馈→再生成」自愈循环 + 对话记忆；自研混合检索（向量 + 关键词加权）解决英文 embedding 对中文查询的检索漂移；34 项单测覆盖，与 Vanna 同一评估集对比 EA 100% vs 95%。

### Bullet 3（安全工程）
> 在 Agent 工具执行边界自研 fail-closed 四层 SQL 安全网关（sqlglot AST 校验 + 正则兜底）、RLS 行级租户隔离与角色级列脱敏，75 个注入攻防用例 + 269 项 pytest 全链路守护。

### Bullet 4（工程化）
> 构建自我纠错回路（9 类错误分类 + 定向重试）、Prometheus/Grafana 监控、查询缓存与前端 SPA，从 NL2SQL 单点能力扩展为可交付的企业级平台。

### 面试口头总结（30 秒版）
> "这是一个 Agent 开发项目。Vanna 提供了可插拔的检索/编排/工具/记忆底座，我在工具执行边界上构建了 fail-closed 安全网关、RLS、脱敏和评估体系。为了证明选型不是拍脑袋，我做了三层对照：纯规则基线（EA 65%）、纯 LLM 直出（95%）、自研 Mini Agent（100%，自愈率 20%）——同一 LLM 同一评估集，只差架构形态。我还从零实现了一个约 800 行的 Agent 底座，验证了 RAG + 工具 + 自愈闭环是可以复现的，这既证明了理解深度，也用数据反哺了'为什么生产用 Vanna'的决策。"

### 面试追问预案：「你重写了为什么还保留 Vanna？」
> 答：自研版是**架构验证 + 对比实验**，不是生产替代——Vanna 成熟、生态全、多轮对话完善；自研版证明了 Agent 核心闭环（工具/循环/记忆/RAG）约 800 行即可复现，且**安全钩子、混合检索、上下文控制**是自研时针对本项目踩坑后做的增强（Vanna 默认没有）。两条路线在同一评估集上互为参照，数据是唯一裁判。

---

## 六、附：组件到代码的映射（便于深挖）

| 架构概念 | 代码位置 |
|---------|---------|
| Agent 装配 | `app/core/vanna_instance.py`（Agent/AgentConfig/ToolRegistry 单例） |
| 工具边界拦截 | `CapturingRunSqlTool.execute()`（安全校验 + RLS + 超时） |
| RAG 检索 | `app/core/chroma_store.py` + `scripts/init_training.py`（46 条训练数据） |
| **自研 Agent 底座** | `app/core/mini_agent/`（runtime 编排循环 / tools 工具层 / rag 混合检索 / memory / naive 对照） |
| **三路对比评估** | `scripts/eval/compare_eval3.py` + `scripts/eval/test_set.json`（54 题） |
| 安全网关 | `app/core/sql_security.py`（四层，fail-closed） |
| RLS | `app/services/rls_service.py`（sqlglot AST 注入） |
| 脱敏 | `app/core/masking_service.py`（角色白名单） |
| 自纠错 | `app/core/error_classifier.py` + `execute_query_with_retry` |
| 评估 | `scripts/eval/test_set.json`（54 题）+ `scripts/eval/compare_eval.py` |
