"""
Mini Agent Runtime - 编排循环（核心）
====================================
自研 Agent 主循环：「感知（检索）→ 决策（生成 SQL）→ 行动（工具执行）
→ 反思（错误反馈重试）」，与 Vanna 的 send_message 循环对齐，但独立实现。

设计决策（面试可讲）：
  1. 循环上限 max_iterations：防止自愈无限递归（对应 Vanna max_tool_iterations）；
  2. 错误分类驱动重试：用 error_classifier 判断"可重试错误"才反馈 LLM，
     权限/拦截类错误直接终止，避免浪费 token 与放大风险；
  3. RAG 可开关：use_rag=False 退化为"全量 schema 硬塞"，量化 RAG 的贡献；
  4. 安全边界：工具执行前强制 fail-closed 网关，拦截结果不进入重试循环。
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.error_classifier import classify_sql_error
from app.core.llm import LLMService, llm_service
from app.core.mini_agent.memory import AgentMemory
from app.core.mini_agent.rag import RAGRetriever
from app.core.mini_agent.tools import SqlTool, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


# ============================================================
# 全量 schema 提示词（降级/对照组用）
# ============================================================

SCHEMA_DDL = """CREATE TABLE operators (
  id INT PRIMARY KEY, name VARCHAR(100), type ENUM('MNO','MVNO'),
  mcc_mnc VARCHAR(6), country VARCHAR(100), status ENUM('active','inactive'),
  contact_info VARCHAR(200), created_at DATETIME, updated_at DATETIME);
CREATE TABLE users (
  id INT PRIMARY KEY, phone_number VARCHAR(20), email VARCHAR(255),
  iccid VARCHAR(22), imsi VARCHAR(15), mvno_id INT, status ENUM('active','inactive','suspended'),
  region VARCHAR(100), created_at DATETIME, updated_at DATETIME);
CREATE TABLE plans (
  id INT PRIMARY KEY, name VARCHAR(200), data_volume_mb INT, voice_minutes INT,
  sms_count INT, price DECIMAL(10,2), currency VARCHAR(10), validity_days INT,
  type ENUM('local','roaming','global'), mvno_id INT,
  status ENUM('active','inactive','discontinued'), description TEXT,
  created_at DATETIME, updated_at DATETIME);
CREATE TABLE orders (
  id INT PRIMARY KEY, user_id INT, plan_id INT, order_no VARCHAR(50),
  status ENUM('pending','paid','activated','cancelled','refunded'),
  amount DECIMAL(10,2), currency VARCHAR(10), payment_method VARCHAR(50),
  mvno_id INT, created_at DATETIME, activated_at DATETIME, cancelled_at DATETIME, updated_at DATETIME);
CREATE TABLE esim_profiles (
  id INT PRIMARY KEY, user_id INT, iccid VARCHAR(22), imsi VARCHAR(15),
  profile_status ENUM('downloaded','installed','active','enabled','disabled','deleted'),
  activation_code VARCHAR(100), mno_id INT, mvno_id INT,
  created_at DATETIME, activated_at DATETIME, updated_at DATETIME);
CREATE TABLE data_usage (
  id INT PRIMARY KEY, user_id INT, iccid VARCHAR(22), used_mb INT,
  country_code VARCHAR(10), roaming_flag TINYINT, started_at DATETIME,
  ended_at DATETIME, created_at DATETIME);
CREATE TABLE roaming_packages (
  id INT PRIMARY KEY, name VARCHAR(200), coverage_countries VARCHAR(500),
  data_volume_mb INT, price DECIMAL(10,2), currency VARCHAR(10),
  validity_days INT, mvno_id INT, status ENUM('active','inactive'),
  created_at DATETIME);"""


# ============================================================
# 配置与响应模型
# ============================================================

@dataclass
class AgentConfig:
    """Agent 配置"""
    max_iterations: int = 5            # 编排循环上限（含首次生成）
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    use_rag: bool = True               # 是否启用 RAG 检索
    rag_max_items: int = 3             # 每 collection 检索数量
    verbose: bool = False              # 打印每轮日志

    def __post_init__(self) -> None:
        self.max_iterations = max(1, self.max_iterations)


@dataclass
class AgentResponse:
    """Agent 响应"""
    question: str
    sql: str = ""
    data: Optional[list[dict]] = None
    columns: Optional[list[str]] = None
    success: bool = False
    iterations: int = 0                # 实际循环轮数
    retries: int = 0                   # 自愈重试次数
    blocked: bool = False
    block_reason: str = ""
    error: str = ""
    context_len: int = 0               # 检索上下文长度（分析 RAG 贡献）
    feedbacks: list[str] = field(default_factory=list)   # 每轮错误反馈
    tool: Optional[ToolResult] = None


# ============================================================
# SQL 提取工具
# ============================================================

_SQL_BLOCK_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_sql_block(text: str) -> str:
    """从 LLM 输出中提取纯 SQL

    处理三种形态：
      1. ```sql ... ``` 代码块
      2. 代码块但语言标记非 sql
      3. 无围栏的裸 SQL（必须以 SELECT/WITH/SHOW 开头，避免把解释文本当 SQL）

    非 SQL 文本（如"抱歉，我无法回答"）返回空字符串。
    """
    if not text:
        return ""
    m = _SQL_BLOCK_RE.search(text)
    if m:
        return m.group(1).strip().rstrip(";")
    # 去除可能残留的围栏与开头的注释行
    cleaned = re.sub(r"^```(?:sql)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    cleaned = re.sub(
        r"^\s*(--[^\n]*\n|#[^\n]*\n|/\*.*?\*/)\s*", "", cleaned, flags=re.DOTALL)
    cleaned = cleaned.strip()
    if re.match(r"(?is)^\s*(select|with|show)\b", cleaned):
        return cleaned.rstrip(";")
    return ""


# ============================================================
# Mini Agent Runtime
# ============================================================

class MiniAgentRuntime:
    """轻量自研 Agent 编排器

    Args:
        llm: LLM 服务（默认 app.core.llm.llm_service）
        registry: 工具注册中心
        retriever: RAG 检索器
        memory: 对话记忆
        config: Agent 配置
    """

    def __init__(
        self,
        llm: Optional[LLMService] = None,
        registry: Optional[ToolRegistry] = None,
        retriever: Optional[RAGRetriever] = None,
        memory: Optional[AgentMemory] = None,
        config: Optional[AgentConfig] = None,
    ) -> None:
        self.llm = llm or llm_service
        self.registry = registry or ToolRegistry()
        self.retriever = retriever or RAGRetriever()
        self.memory = memory or AgentMemory()
        self.config = config or AgentConfig()
        self._sql_tool: Optional[SqlTool] = None

    # --- 工具管理 ---

    def set_sql_tool(self, tool: SqlTool) -> None:
        """注入 SQL 执行工具（测试/评估时可换 dry_run 实例）"""
        self._sql_tool = tool

    @property
    def sql_tool(self) -> SqlTool:
        if self._sql_tool is None:
            self._sql_tool = SqlTool(dry_run=False)
        return self._sql_tool

    # --- 主入口 ---

    async def ask(
        self,
        question: str,
        role: str = "admin",
        mvno_id: Optional[int] = None,
        use_rag: Optional[bool] = None,
    ) -> AgentResponse:
        """处理一个自然语言问题，返回最终 SQL 与结果

        Args:
            question: 用户问题
            role: 用户角色（RLS 用）
            mvno_id: MVNO ID（RLS 用）
            use_rag: 覆盖配置的 RAG 开关
        """
        use_rag = self.config.use_rag if use_rag is None else use_rag
        resp = AgentResponse(question=question)

        # 1. 感知：检索上下文（RAG）
        context = ""
        if use_rag:
            context = self.retriever.retrieve(question, max_items=self.config.rag_max_items)
            resp.context_len = len(context)
            if self.config.verbose and context:
                print(f"[MiniAgent] RAG context: {len(context)} chars")

        # 2-4. 决策-行动-反思循环
        sql, last_error = "", ""
        for i in range(self.config.max_iterations):
            resp.iterations = i + 1
            try:
                if i == 0:
                    # 首次生成：检索上下文优先，空则用全量 schema 兜底
                    sql = await self.llm.generate_sql(
                        question,
                        ddl=context or SCHEMA_DDL,
                        documentation="",
                        sql_examples="",
                    )
                else:
                    # 反思：根据错误反馈修正
                    cls = classify_sql_error(last_error)
                    sql = await self.llm.correct_sql(
                        sql=sql,
                        error_message=last_error,
                        correction_hint=f"错误类别: {cls.category.value}",
                        ddl=context or SCHEMA_DDL,
                    )
            except Exception as e:
                logger.error("LLM call failed at iteration %d: %s", i + 1, e)
                resp.error = f"LLM 调用失败: {e}"
                self.memory.add_tool(resp.error)
                return resp

            sql = extract_sql_block(sql)
            resp.sql = sql
            if not sql:
                resp.error = "LLM 未返回有效 SQL"
                break
            if self.config.verbose:
                print(f"[MiniAgent] iter={i + 1} SQL: {sql[:120]}")

            # 3. 行动：工具执行（安全网关 + RLS 在工具内部）
            result = await self.sql_tool.execute(sql, role=role, mvno_id=mvno_id)
            resp.tool = result

            # 安全拦截：不进入自愈循环
            if result.blocked:
                resp.blocked = True
                resp.block_reason = result.block_reason
                resp.error = result.error
                resp.retries = i
                self.memory.add_tool(f"BLOCKED: {result.block_reason}")
                return resp

            if result.success:
                resp.success = True
                resp.data = result.data
                resp.columns = result.columns
                resp.retries = i
                self.memory.add_assistant(sql, metadata={"success": True})
                return resp

            # 4. 反思：错误是否可重试
            last_error = result.error
            resp.feedbacks.append(last_error)
            cls = classify_sql_error(last_error)
            if not cls.retryable:
                resp.error = f"不可重试错误: {last_error}"
                resp.retries = i
                return resp
            if self.config.verbose:
                print(f"[MiniAgent] error ({cls.category.value}), retrying...")

        # 达到循环上限仍未成功
        resp.error = resp.error or f"达到最大循环次数 {self.config.max_iterations}"
        resp.retries = self.config.max_iterations - 1
        self.memory.add_tool(resp.error)
        return resp

    # --- 会话辅助 ---

    def reset(self) -> None:
        """清空对话记忆（新会话）"""
        self.memory.clear()

    def get_trace(self) -> dict[str, Any]:
        """导出本次会话的 trace（评估/监控用）"""
        return {
            "config": {
                "max_iterations": self.config.max_iterations,
                "use_rag": self.config.use_rag,
                "rag_max_items": self.config.rag_max_items,
            },
            "memory_size": self.memory.size,
            "history": self.memory.get_history(),
        }


def build_default_runtime(
    dry_run: bool = True,
    use_rag: bool = True,
    max_iterations: int = 5,
    verbose: bool = False,
) -> MiniAgentRuntime:
    """默认装配一个 Mini Agent Runtime

    Args:
        dry_run: SqlTool 是否用 dry_run（离线评估/测试）
        use_rag: 是否启用 RAG
        max_iterations: 循环上限
        verbose: 打印每轮日志
    """
    tool = SqlTool(dry_run=dry_run)
    runtime = MiniAgentRuntime(
        retriever=RAGRetriever(),
        memory=AgentMemory(),
        config=AgentConfig(
            max_iterations=max_iterations,
            use_rag=use_rag,
            verbose=verbose,
        ),
    )
    runtime.set_sql_tool(tool)
    return runtime
