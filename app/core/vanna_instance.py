"""
Vanna 2.0 Agent 配置与初始化
--------------------------
使用 Vanna 2.0 Agent 架构，配置 LLM + MySQL + 工具，提供单例模式。
Vanna 2.0 采用 Agent-based 架构，通过 send_message() 处理自然语言查询。
"""

import logging
from typing import Optional

from typing import Any

from vanna import Agent, AgentConfig, ToolRegistry
from vanna.core.tool import ToolContext, ToolResult
from vanna.core.user import RequestContext, User, UserResolver
from vanna.integrations.openai import OpenAILlmService
from vanna.integrations.mysql import MySQLRunner
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.tools import RunSqlTool
from vanna.tools.run_sql import RunSqlToolArgs

from app.config.settings import settings
from app.core.chroma_store import chroma_store, ChromaTrainingStore

logger = logging.getLogger(__name__)


# --- 自定义 RunSqlTool（捕获生成的 SQL） ---

class CapturingRunSqlTool(RunSqlTool):
    """扩展 RunSqlTool，捕获 LLM 生成的 SQL 语句并执行安全校验 + RLS 注入

    Vanna 2.0 的 RunSqlTool.execute() 不暴露 args.sql，
    本子类重写 execute() 来：
    1. 捕获 SQL 语句供 query_service 读取
    2. 在执行前通过 SQLSecurityGateway 校验 SQL 安全性
    3. 拦截危险 SQL，阻止执行
    4. 注入 RLS 行级安全条件（非 admin 用户）
    5. 添加查询超时提示
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._last_sql: str = ""
        self._last_blocked: bool = False
        self._last_block_reason: str = ""
        # RLS 用户上下文
        self._current_role: str = "admin"
        self._current_mvno_id: Any = None

    @property
    def last_sql(self) -> str:
        """获取最近一次执行的 SQL"""
        return self._last_sql

    @property
    def last_blocked(self) -> bool:
        """最近一次 SQL 是否被安全网关拦截"""
        return self._last_blocked

    @property
    def last_block_reason(self) -> str:
        """最近一次拦截原因"""
        return self._last_block_reason

    def set_user_context(self, role: str, mvno_id: Any) -> None:
        """设置当前查询的用户上下文（用于 RLS）

        Args:
            role: 用户角色 (admin/analyst/viewer)
            mvno_id: 用户所属 MVNO ID
        """
        self._current_role = role
        self._current_mvno_id = mvno_id

    def reset_user_context(self) -> None:
        """重置用户上下文为默认值（admin）"""
        self._current_role = "admin"
        self._current_mvno_id = None

    async def execute(self, context: ToolContext, args: RunSqlToolArgs) -> ToolResult:
        """执行 SQL 并捕获 SQL 语句，执行前进行安全校验 + RLS 注入"""
        # 捕获 SQL 语句
        self._last_sql = args.sql if hasattr(args, 'sql') else ""
        self._last_blocked = False
        self._last_block_reason = ""
        logger.debug("Captured SQL: %s", self._last_sql[:200])

        # --- SQL 安全网关校验 ---
        try:
            from app.core.sql_security import sql_gateway
            check_result = sql_gateway.validate_sql(self._last_sql)
            if not check_result.passed:
                # SQL 被拦截，不执行
                self._last_blocked = True
                self._last_block_reason = check_result.reason
                logger.warning(
                    "SQL BLOCKED by %s: %s | SQL: %.200s",
                    check_result.layer, check_result.reason, self._last_sql,
                )
                return ToolResult(
                    success=False,
                    result_for_llm=(
                        f"SQL 安全拦截：{check_result.reason}\n"
                        f"该 SQL 语句已被安全网关阻止执行。"
                        f"请修改查询条件后重试。"
                    ),
                    error=check_result.reason,
                    metadata={
                        "blocked": True,
                        "layer": check_result.layer,
                        "reason": check_result.reason,
                    },
                )

            # SQL 通过校验，可能被修改（如自动添加 LIMIT）
            if check_result.sql_after_check and check_result.sql_after_check != self._last_sql:
                logger.info("SQL modified by security gateway: auto-added LIMIT")
                # 更新 args 中的 SQL
                try:
                    args.sql = check_result.sql_after_check
                except Exception:
                    # 如果 args 不可变，创建新的
                    pass
                self._last_sql = check_result.sql_after_check

        except Exception as e:
            # 安全校验子系统自身故障：FAIL-CLOSED —— 宁可阻断，不可在未经验证下执行 SQL
            logger.error("SQL security check FAILED (fail-closed, blocking): %s", e)
            self._last_blocked = True
            self._last_block_reason = f"安全校验子系统异常，默认拦截: {e}"
            return ToolResult(
                success=False,
                result_for_llm=(
                    "SQL 安全校验子系统暂时不可用，出于安全优先原则已阻断本次执行。"
                    "请稍后重试。"
                ),
                error=self._last_block_reason,
                metadata={"blocked": True, "layer": "security_subsystem", "reason": self._last_block_reason},
            )

        # --- RLS 行级安全条件注入 ---
        try:
            from app.services.rls_service import rls_service
            rls_result = rls_service.inject_rls(
                sql=self._last_sql,
                role=self._current_role,
                mvno_id=self._current_mvno_id,
            )
            if rls_result.rls_applied:
                logger.info(
                    "RLS injected: tables=%s, condition=%s",
                    rls_result.rls_tables, rls_result.rls_condition,
                )
                try:
                    args.sql = rls_result.sql
                except Exception:
                    pass
                self._last_sql = rls_result.sql
        except Exception as e:
            logger.warning("RLS injection error (allowing without RLS): %s", e)

        # --- 添加查询超时提示 ---
        try:
            from app.services.query_service import _add_timeout_hint
            timed_sql = _add_timeout_hint(self._last_sql)
            if timed_sql != self._last_sql:
                logger.debug("Added MAX_EXECUTION_TIME hint to SQL")
                try:
                    args.sql = timed_sql
                except Exception:
                    pass
                self._last_sql = timed_sql
        except Exception as e:
            logger.debug("Failed to add timeout hint: %s", e)

        # 调用父类方法执行 SQL
        return await super().execute(context, args)


# --- 简单的用户解析器（Day 3 阶段，所有用户视为 admin） ---

class DefaultUserResolver(UserResolver):
    """默认用户解析器 - 开发阶段使用，所有请求视为 admin 用户"""

    async def resolve_user(self, request_context: RequestContext) -> User:
        """解析用户身份
        
        Day 3 阶段简单实现，后续 Day 6 会集成 JWT 认证。
        """
        return User(
            id="default_user",
            username="developer",
            email="dev@esim-platform.local",
            group_memberships=["admin"],
            metadata={"role": "admin"},
        )


class VannaAgentManager:
    """Vanna 2.0 Agent 管理器（单例模式）
    
    负责创建和配置 Vanna Agent，包括：
    - OpenAILlmService（DeepSeek-V3 兼容接口）
    - MySQLRunner（MySQL 数据库连接）
    - RunSqlTool（SQL 执行工具）
    - ToolRegistry（工具注册中心）
    """

    _instance: Optional["VannaAgentManager"] = None
    _agent: Optional[Agent] = None
    _run_sql_tool: Optional[CapturingRunSqlTool] = None
    _initialized: bool = False
    _chroma_available: bool = False

    def __new__(cls) -> "VannaAgentManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def initialize(self) -> None:
        """初始化 Vanna Agent
        
        在 FastAPI 应用启动时调用一次。
        配置 LLM 服务、数据库连接和工具。
        """
        if self._initialized:
            logger.info("Vanna Agent already initialized, skipping.")
            return

        logger.info("Initializing Vanna 2.0 Agent...")

        try:
            # 1. 配置 LLM 服务 (DeepSeek-V3 via OpenAI 兼容接口)
            llm_service = OpenAILlmService(
                model=settings.LLM_MODEL,
                api_key=settings.LLM_API_KEY,
                base_url=settings.LLM_BASE_URL,
            )
            logger.info("LLM service configured: model=%s, base_url=%s",
                        settings.LLM_MODEL, settings.LLM_BASE_URL)

            # 2. 配置 MySQL Runner
            sql_runner = MySQLRunner(
                host=settings.DATABASE_HOST,
                database=settings.DATABASE_NAME,
                user=settings.DATABASE_USER,
                password=settings.DATABASE_PASSWORD,
                port=settings.DATABASE_PORT,
            )
            logger.info("MySQL runner configured: %s@%s:%d/%s",
                        settings.DATABASE_USER, settings.DATABASE_HOST,
                        settings.DATABASE_PORT, settings.DATABASE_NAME)

            # 3. 创建 CapturingRunSqlTool（捕获生成的 SQL）
            self._run_sql_tool = CapturingRunSqlTool(sql_runner=sql_runner)
            logger.info("CapturingRunSqlTool created")

            # 4. 注册工具到 ToolRegistry
            tool_registry = ToolRegistry()
            tool_registry.register_local_tool(self._run_sql_tool, access_groups=["admin"])
            logger.info("ToolRegistry configured with 1 tool")

            # 5. 创建用户解析器
            user_resolver = DefaultUserResolver()

            # 6. 初始化 ChromaDB 训练数据存储
            # (如果 embedding 模型下载失败，训练数据不可用但不影响基本查询)
            chroma_available = False
            try:
                await chroma_store.initialize()
                self._chroma_available = True
                logger.info("ChromaDB training store initialized successfully")
            except Exception as e:
                logger.warning(
                    "ChromaDB initialization failed (training data disabled): %s\n"
                    "提示: 可前往 HuggingFace 手动下载 all-MiniLM-L6-v2 模型\n"
                    "下载地址: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2\n"
                    "放置路径: ~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/",
                    e
                )
                print(f"[CHROMADB] WARNING: Training store unavailable: {e}")
                print("[CHROMADB] Basic NL2SQL queries will still work without training data.")
                print("[CHROMADB] To enable: download all-MiniLM-L6-v2 from HuggingFace.")

            # 7. 创建 Agent Memory（对话记忆，使用内存模式）
            agent_memory = DemoAgentMemory()
            logger.info("Agent memory configured: In-Memory")

            # 8. 创建 Agent 配置
            agent_config = AgentConfig(
                max_tool_iterations=30,
                stream_responses=True,
                temperature=settings.LLM_TEMPERATURE,
                max_tokens=settings.LLM_MAX_TOKENS,
                auto_save_conversations=False,
                include_thinking_indicators=False,
            )

            # 9. 创建 Agent
            self._agent = Agent(
                llm_service=llm_service,
                tool_registry=tool_registry,
                user_resolver=user_resolver,
                agent_memory=agent_memory,
                config=agent_config,
            )

            self._initialized = True
            logger.info("Vanna 2.0 Agent initialized successfully!")

        except Exception as e:
            logger.error("Failed to initialize Vanna Agent: %s", e)
            self._initialized = False
            raise

    @property
    def agent(self) -> Agent:
        """获取 Vanna Agent 实例"""
        if not self._initialized or self._agent is None:
            raise RuntimeError(
                "Vanna Agent not initialized. "
                "Call initialize() during application startup."
            )
        return self._agent

    @property
    def is_initialized(self) -> bool:
        """检查 Agent 是否已初始化"""
        return self._initialized

    def get_last_sql(self) -> str:
        """获取最近一次执行的 SQL 语句"""
        if self._run_sql_tool is None:
            return ""
        return self._run_sql_tool.last_sql

    @property
    def chroma_available(self) -> bool:
        """ChromaDB 训练存储是否可用"""
        return self._chroma_available and chroma_store.is_initialized

    def retrieve_context(self, question: str, max_items: int = 5) -> str:
        """检索与问题相关的训练数据作为 LLM 上下文

        从 ChromaDB 的 DDL、文档、SQL 示例中检索相关内容，
        拼接为结构化文本，用于增强 Agent 的 SQL 生成能力。

        Args:
            question: 用户自然语言问题
            max_items: 每个 collection 检索的最大数量

        Returns:
            str: 拼接后的上下文文本。ChromaDB 不可用时返回空字符串。
        """
        if not self.chroma_available:
            return ""

        try:
            context = chroma_store.retrieve_context(question, max_items=max_items)
            if context:
                logger.debug("Retrieved %d chars of training context", len(context))
            return context
        except Exception as e:
            logger.warning("Failed to retrieve training context: %s", e)
            return ""

    def create_request_context(self) -> RequestContext:
        """创建默认的请求上下文（用于开发/测试）"""
        return RequestContext(data={
            "cookies": {},
            "headers": {},
            "remote_addr": "127.0.0.1",
            "query_params": {},
            "metadata": {},
        })

    async def shutdown(self) -> None:
        """关闭 Agent（释放资源）"""
        logger.info("Shutting down Vanna Agent...")
        self._agent = None
        self._initialized = False
        logger.info("Vanna Agent shut down.")


# 全局实例
vanna_manager = VannaAgentManager()
