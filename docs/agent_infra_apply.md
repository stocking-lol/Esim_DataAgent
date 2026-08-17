# Agent Infra 工程师投递材料（基于 Vanna 2.0 拆解 + eSIM NL2SQL 平台实战）

> 定位：**"我把开源 Agent 框架 Vanna 2.0 拆到源码级并在生产里二次开发，同时从零实现过一个轻量 Agent 底座"**  
> ——这正好对招聘方关心的"懂 Agent 框架内核 + AgentOps + 多租户隔离"三件事。

---

## 一、Vanna 2.0 架构拆解（面试可复述的核心理解）

### 1.1 整体：一个"编排内核 + 策略注入 + 全链路可观测"的分层架构

```
┌─ 应用层 ──  I/Flask）+ UiComponent（富/简双通道渲染）
│
├─ 编排内核 ──  core/agent/agent.py：感知→决策→行动→反思 主循环（async 事件流）
│     │        send_message() 驱动，7 大扩展点注入：
│     │        lifecycle_hooks / llm_middlewares / error_recovery_strategy /
│     │        context_enrichers / llm_context_enhancer / conversation_filters /
│     │        observability_provider
│     ▼
├─ 决策引擎 ──  core/llm/（LlmService 厂商无关：request/stream/validate_tools）
│     │        工具调用协议：LlmResponse.is_tool_call() → tool 消息回灌 → 循环
│     ▼
├─ 行动层 ────  core/tool/（Tool 抽象 + ToolRegistry 调度）
│     │        LLM 只见 JSON Schema，不见实现（Pydantic model_json_schema 自动生成）
│     │        执行链：权限校验 → 参数校验 → transform_args（RLS/脱敏钩子）→ 执行 → 审计
│     ▼
├─ 状态层 ────  capabilities/agent_memory/（双路 RAG 记忆：ToolMemory + TextMemory）
│     │        + core/storage.ConversationStore（会话）+ core/lifecycle（hooks）
│     ▼
├─ 韧性层 ────  core/recovery/（ErrorRecoveryStrategy：RETRY/FAIL/FALLBACK/SKIP）
│     │        + 编排层兜底（异常降级为错误 UI，主链路不中断）
│     ▼
└─ 观测层 ────  core/observability/（40+ 埋点，Span 支持链路追踪）
               + core/audit/（合规审计，PII 脱敏，与性能观测分离）
```

### 1.2 关键设计决策（面试亮点）

| 设计                           | 为什么（源码依据）                                                                              |
| ---------------------------- | -------------------------------------------------------------------------------------- |
| **工具调用即对话状态**                | agent.py L668 把含 tool_calls 的 assistant 消息先入会话再执行工具——满足 OpenAI 协议，天然支持多轮上下文            |
| **LLM 只见 schema 不见实现**       | Tool.get_schema() 用 Pydantic 自动生成 JSON Schema（base.py L16-70），实现与协议解耦                  |
| **数据级多租户隔离在 transform_args** | ToolRegistry.execute L113 提供参数变换点——RLS/脱敏在工具边界注入，而非散落业务代码                              |
| **RAG 是增强器而非依赖**             | DefaultLlmContextEnhancer 检索失败降级返回原 prompt（default.py L94-101），主链路韧性                   |
| **韧性靠降级设计**                  | recovery 策略模式默认 FAIL；编排层 try/except 兜底全部异常降级为 UI 错误                                    |
| **可观测与合规分离**                 | observability（性能/追踪）与 audit（审计/脱敏）是两个独立抽象，各司其职                                         |
| **配置即策略**                    | AgentConfig（max_tool_iterations=10 防死循环 / stream_responses / auto_save）集中为 Pydantic 配置 |

### 1.3 Agent 全生命周期 / LLMOps / AgentOps / 多租户 归属（面试直接引用）

| 招聘维度                   | Vanna 2.0 承担模块                                                                                                   | 我的项目对应实践                                                                    |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| **Agent 全生命周期**        | 训练=agent_memory 沉淀；**评测=core/evaluation（EvaluationRunner 并行评测 + compare_agents A/B）**；部署=core/workflow + servers | 自建 **54 题评估集 + 三路对比实验**（纯 LLM 直出/自研 Agent/Vanna），EA 100% vs 95%，量化选型        |
| **LLMOps**             | 模型配置集中管理 + observability 监控                                                                                      | DeepSeek-V3 配置中心化（.env），Full Jitter 重试 + 故障注入演练，Prometheus 指标（LLM 调用耗时/重试率） |
| **AgentOps（编排/自愈/状态）** | agent.py 工具循环 + core/recovery + ConversationStore                                                                | 自研 Mini Agent 编排循环 + **9 类错误分类自愈回路**（15% 首轮失败自动修复兜底至 100%）+ 会话管理            |
| **可观测性**               | observability（Span 链路）+ audit（合规）                                                                                | Prometheus/Grafana（QPS/P95/拦截率/纠错率）+ 审计日志 + 查询全链路 trace                     |
| **多租户隔离**              | user → tool_schemas → transform_args(RLS) → UiFeatures → audit 五层                                                | JWT 认证 + **RLS 行级租户隔离** + 角色级列脱敏 + fail-closed 网关（75 攻防用例）                  |

---

## 二、职位要求逐条映射（✓ 已具备 / ★ 可展示源码级理解 / ○ 缺口）

### 2.1 基础条件

| 要求                | 状态 | 我的证据/补法                                                                      |
| ----------------- | -- | ---------------------------------------------------------------------------- |
| 计算机相关专业优先         | ○  | 如实填写专业背景                                                                     |
| 顶会论文/高影响项目/开源贡献加分 | ★  | 项目 GitHub 开源可查（v0.1-v0.8.0）；**基于开源 Vanna 2.0 二次开发**（改 LLM 服务层、工具层）本身就是开源生态贡献 |

### 2.2 技术基础

| 要求                                      | 状态  | 我的证据                                                                                     |
| --------------------------------------- | --- | ---------------------------------------------------------------------------------------- |
| 扎实计算机基础（OS/网络/数据库）                      | ✓   | Python 全栈；MySQL 8.0 实战（schema 设计/慢查询/事务）；HTTP 协议（REST 全链路）；并发（asyncio）                   |
| 精通一门编程语言                                | ✓   | Python（FastAPI/Vanna 二次开发/自研 Agent 底座/269 项 pytest 工程实践）                                 |
| 后端技术栈（微服务/MQ/缓存）                        | ✓   | FastAPI 服务化、**内存查询缓存（TTL）**、可扩展消息中间件                                                     |
| 云原生（K8s/Serverless）或安全隔离（Docker/gVisor） | ✓/○ | Docker Compose 部署 ✓；K8s/gVisor 为**可标注的学习路径**（见补齐计划）                                      |
| 安全隔离技术                                  | ✓   | **fail-closed 四层 SQL 安全网关（sqlglot AST + 正则兜底）**、75 个注入攻防用例、RLS 行级隔离、列级脱敏——安全隔离是本项目最强卖点之一 |

### 2.3 AI Agent 专精

| 要求                    | 状态 | 我的证据                                                                                                                             |
| --------------------- | -- | -------------------------------------------------------------------------------------------------------------------------------- |
| 深入理解大模型原理             | ✓  | LLM 调用层工程化：temperature/max_tokens/流式协议/函数调用（tool_calls）协议；重试决策矩阵（瞬态 vs API 错误）                                                   |
| 熟悉至少一种 Agent 框架或核心组件  | ✓✓ | **双倍覆盖**：①生产深度使用 Vanna 2.0（源码级拆解）；②**从零实现自研 Mini Agent 底座**（ToolRegistry/编排循环/混合检索/记忆）——相当于理解 LangGraph 的图执行模型 + LangChain 的编排模型 |
| Agent 全生命周期（训练/评测/部署） | ✓  | 训练数据 46 条（DDL/文档/SQL 示例）；54 题评估集 + 三路对比 + **多轮重复评估（--trials N，均值±标准差，消除 LLM 随机性）**；FastAPI + Docker 部署 |
| 强化学习/规划算法背景           | ○  | 标注：可作为学习补充（与自愈策略/规划序列相关）                                                                                                         |

### 2.4 Agent Ops

| 要求                   | 状态 | 我的证据                                                    |
| -------------------- | -- | ------------------------------------------------------- |
| LLMOps（模型版本/监控/部署）   | ✓  | 模型配置中心化；Prometheus 指标；Docker 部署                         |
| AgentOps（编排/自愈/状态管理） | ✓✓ | 自研编排循环 + 9 类错误自愈回路 + 会话状态管理 + **Full Jitter 重试与故障注入演练** |
| 可观测性（链路追踪/日志分析）      | ✓  | Prometheus/Grafana 监控告警、审计日志、查询耗时/纠错率指标                 |

### 2.5 工程与交付

| 要求               | 状态 | 我的证据                                                             |
| ---------------- | -- | ---------------------------------------------------------------- |
| AI Coding 快速原型开发 | ✓  | 4 周 23 天从零到企业级平台（v0.1→v0.8.0）；自研 Agent 底座 800 行两日内完成             |
| 测试与变更管理（CI/静态分析） | ✓  | **269 项 pytest**（75 个攻防用例）、评估报告、GitHub 版本管理（语义化 tag v0.1-v0.8.0） |
| 高并发优化            | ✓  | Full Jitter 防重试风暴、查询缓存、超时控制、行数限制                                 |
| 多租户隔离            | ✓✓ | RLS 行级隔离 + 列级脱敏 + 工具级/UI 级权限 + 审计留痕（对齐 Vanna 五层隔离）               |

### 2.6 加分项

| 要求           | 状态 | 我的证据                                                        |
| ------------ | -- | ----------------------------------------------------------- |
| Agent 优化实战经验 | ✓✓ | 三路对比实验量化 Agent 化收益（EA 95%→100%）；**多轮重复评估（trials=3）：EA 100±0%、自愈率 16.7±2.9%（各轮 15/15/20）** |
| Agent 平台开发   | ✓✓ | 自研 Mini Agent Runtime + 完整平台（FastAPI/前端/监控/评估）              |
| 上下文工程        | ✓✓ | **自研混合检索**（向量 + 关键词加权）解决中文检索漂移；上下文总量控制；few-shot 工程          |
| 多 Agent 协同系统 | ○  | 尚未涉及（可标注为学习方向：多 Agent 规划/协同）                                |

---

## 三、简历 Bullet（Agent Infra 方向，可直接用）

**eSIM NL2SQL 数据智能体平台 · Agent 开发工程师**（[时间]）

- **Agent 框架内核**：生产深度使用开源 Vanna 2.0 Agent（源码级拆解其"编排内核+策略注入+全链路可观测"架构），并**从零实现轻量 Agent 底座**（ToolRegistry 工具调度 / 感知-决策-行动-反思编排循环 / 双路 RAG 混合检索 / 对话记忆），构建"自然语言 → SQL → 可视化"全链路；
- **AgentOps**：自建 54 题评估集 + 三路对比实验量化 Agent 化收益（执行准确率 100% vs 纯 LLM 直出 95%）；**多轮重复评估（--trials 3）输出均值±标准差（EA 100±0%、自愈率 16.7±2.9%），消除 LLM 随机性**；9 类错误分类自愈回路（首轮失败自动修复兜底至 100%）；LLM 调用 Full Jitter 重试 + 故障注入演练（断连自动重连/API 错误快速失败）；
- **多租户安全隔离**：fail-closed 四层 SQL 安全网关 + RLS 行级租户隔离 + 角色级列脱敏，75 个注入攻防用例 + 269 项 pytest 全链路守护；修复注释拆分绕过真实漏洞（DRO/\*\*/P == DROP）；
- **可观测与工程化**：Prometheus/Grafana 监控（QPS/P95/拦截率/纠错率）+ 审计日志 + Docker 部署 + 语义化版本管理（v0.1-v0.8.0）。

---

## 四、面试高频追问应答（口述弹药）

**Q1：你项目里 Agent 框架到底帮你做了什么，你自己做了什么？**

> Vanna 2.0 提供的是"编排内核 + 可插拔扩展点"：工具调度、函数调用协议、记忆、recovery。我做的分三层——① 把它接到生产（DeepSeek/ChromaDB/MySQL，重写工具层注入安全网关+RLS+脱敏）；② 为证明理解，**从零实现了一个约 800 行的 Mini Agent 底座**，覆盖工具注册、编排循环、混合检索、记忆，再与 Vanna 做三路对比；③ 用评估数据反哺选型。

**Q2：Agent 编排循环的核心难点？**

> 状态管理 + 终止条件。Vanna 把工具调用即对话状态（assistant 消息先入会话再执行），用 max_tool_iterations 防死循环；我自研版用 max_iterations + 错误分类决定是否重试 + 安全拦截不进入自愈，避免放大风险。

**Q3：多租户隔离怎么做？**

> 对齐 Vanna 的五层隔离思路：身份（JWT→User）→ 工具可见性（access_groups）→ **数据级 RLS（sqlglot AST 在工具执行边界注入租户过滤条件）** → 列级脱敏（角色白名单）→ 审计留痕。关键是把隔离注入点收敛到工具边界（transform_args），而不是散落业务代码。

**Q4：RAG 检索效果不好你怎么办？**

> 真实案例：英文 embedding（all-MiniLM-L6-v2）对中文查询检索漂移，"问题与示例一字不差"都召回不了。解法是自研混合检索——向量召回大候选池后按关键词重叠（Jaccard）+ 主题包含加权重排，并做上下文总量控制（SQL 示例 > 文档 > DDL）。修复后 20 题 EA 从 95% 提到 100%。

**Q5：高并发下 LLM 抖动你怎么处理？**

> 重试决策矩阵：连接类/限流 → Full Jitter 抖动退避重试（避免重试风暴）；API 错误（401/参数错误）→ 不重试、快速失败、提示人工介入。且做了故障注入演练：模拟断连看到 3 次抖动重连、模拟 Key 失效看到 0 次无效重试。

**Q6：你怎么证明你的评估不是运气？（Agent 评测严谨性）**

> 我的评估是三路对照实验，且用**多轮重复评估**消除 LLM 随机性：`compare_eval3.py --trials 3` 同一评估集跑 3 轮，输出指标均值±标准差。比如自研 Agent 与 Vanna 的 EA 都是 100%，如果只看单轮可能被质疑是抽样误差，多轮取均值后波动区间给出置信边界；对差异不够显著的地方（如自愈率 15-20% 区间），我如实标注样本量限制，不夸大。Vanna 2.0 的 `core/evaluation` 里 `compare_agents` 做的正是这件事，我从工程上复现了它。

---

## 五、缺口补齐计划（投递前 2-4 周）

| 缺口                   | 优先级 | 补法（结合现有项目）                                                                                                                    |
| -------------------- | --- | ----------------------------------------------------------------------------------------------------------------------------- |
| **云原生 K8s/gVisor**   | 高   | 给项目补一个 deployment.yaml + 把 Docker Compose 迁移为单节点 K8s manifest；讲清"沙箱隔离"与 gVisor 的关系（gVisor 是用户态内核，与 fail-closed 网关的"默认拒绝"哲学同源） |
| **LangGraph/其他框架对比** | 高   | 花 1 天读 LangGraph 的 StateGraph/节点边模型，写一页"Vanna vs 自研底座 vs LangGraph"编排模型对比（面试讲"图执行 vs 顺序循环 vs 事件流"）                            |
| **LLMOps 模型版本管理**    | 中   | 给 .env 的 LLM_MODEL 增加可观测的"模型灰度切换"记录；在监控里加"模型版本"标签维度                                                                           |
| **强化学习/规划基础**        | 低   | 了解 Agent 训练中 RLHF/SFT 的基本流程 + 自愈策略与 MDP 的类比（错误状态→动作→奖励），能讲概念即可                                                                |
| **多 Agent 协同**       | 低   | 用自研底座扩展一个"规划 Agent + 执行 Agent"的玩具 demo（一个拆分问题、一个执行查询），证明扩展性                                                                   |

---

## 六、一句话定位（简历顶部/自我介绍用）

> "**深度使用并拆解过 Agent 框架内核、能从零复现 Agent 底座、用评估数据做 Agent 选型、并在工具边界落地多租户安全隔离**的 Agent 平台工程师——我的项目同时覆盖了 Agent 全生命周期（训练/评测/部署）与 AgentOps（编排/自愈/状态管理/可观测）。"
