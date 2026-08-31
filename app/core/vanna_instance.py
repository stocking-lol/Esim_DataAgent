"""
Vanna 2.0 Agent 配置与初始化
--------------------------
使用 Vanna 2.0 Agent 架构，配置 LLM + MySQL + 工具，提供单例模式。
Vanna 2.0 采用 Agent-based 架构，通过 send_message() 处理自然语言查询。
"""

import asyncio
import contextvars
import logging
import random
from typing import Any, AsyncGenerator, Dict, Optional

from vanna import Agent, AgentConfig, ToolRegistry
from vanna.core.tool import ToolContext, ToolResult
from vanna.core.user import RequestContext, User, UserResolver
from vanna.integrations.openai import OpenAILlmService
from vanna.integrations.mysql import MySQLRunner
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.tools import RunSqlTool
from vanna.tools.run_sql import RunSqlToolArgs

from openai import APIConnectionError, RateLimitError

from app.config.settings import settings
from app.core.chroma_store import chroma_store, ChromaTrainingStore
from app.core.llm import MAX_RETRIES, compute_backoff_delay

logger = logging.getLogger(__name__)


# ============================================================
# 带 jittered 重连的 LLM 服务（包装 Vanna 的 OpenAILlmService）
# ============================================================

class ResilientOpenAILlmService(OpenAILlmService):
    """包装 Vanna OpenAILlmService：为 LLM 调用加入「指数退避 + 全抖动」重连

    背景：
      生产核心路径（Agent 流式生成 SQL）直接使用 Vanna 的 OpenAILlmService，
      其内部 LLM 调用**没有重试**——连接错误（如网络抖动、API 瞬时不可达）
      会直接抛出，导致整条查询失败。自研 LLMService 的抖动重试只覆盖了
      辅助路径（摘要/纠错），未覆盖 Agent 生成路径。

    本类重写 stream_request / send_request，把 openai 调用包进与
    app.core.llm.compute_backoff_delay 一致的 jittered 重试循环：
      - 瞬态错误（APIConnectionError：连接失败/超时；RateLimitError：限流）→ 重试
      - 其他错误（4xx/5xx、鉴权失败）→ 直接抛出，不浪费调用
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._random = random

    async def _retry_call(self, fn):
        """对 openai 调用做 jittered 重试（连接/限流类瞬态错误）

        fn 可返回普通值或 awaitable（同步/异步调用均可重试）。
        """
        import inspect
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = fn()
                if inspect.isawaitable(result):
                    result = await result
                return result
            except (APIConnectionError, RateLimitError) as e:
                last_error = e
                if attempt >= MAX_RETRIES:
                    break
                delay = compute_backoff_delay(attempt, rng=self._random)
                logger.warning(
                    "LLM call attempt %d/%d failed (%s: %.80s), retrying in %.2fs (jittered)...",
                    attempt, MAX_RETRIES, type(e).__name__, str(e)[:80], delay,
                )
                await asyncio.sleep(delay)
        logger.error("LLM call failed after %d attempts: %s", MAX_RETRIES, last_error)
        raise last_error

    async def send_request(self, request) -> Any:
        """非流式请求：整体重试（幂等，payload 相同响应相同）"""
        parent = OpenAILlmService.send_request  # 显式父类方法（lambda 内 zero-arg super 不可用）
        return await self._retry_call(lambda: parent(self, request))

    async def stream_request(self, request) -> AsyncGenerator[Any, None]:
        """流式请求：仅在「建立流」阶段重试（连接错误发生在 create 时）

        流一旦建立并开始迭代，中途错误无法重放，直接向上一层抛出。
        流的处理逻辑与父类 OpenAILlmService.stream_request 保持一致
        （工具调用累积、终结 chunk 等），仅替换 create 建立流部分。
        """
        import json

        from vanna.core.llm import LlmStreamChunk
        from vanna.core.llm.models import ToolCall

        payload = self._build_payload(request)

        # 建立流（连接/限流错误在此处 jittered 重试）
        stream = await self._retry_call(
            lambda: self._client.chat.completions.create(**payload, stream=True))

        # 以下与父类逻辑一致：流式文本 + 工具调用累积
        tc_builders: Dict[int, Dict[str, Optional[str]]] = {}
        last_finish: Optional[str] = None

        for event in stream:
            if not getattr(event, "choices", None):
                continue
            choice = event.choices[0]
            delta = getattr(choice, "delta", None)
            if delta is None:
                last_finish = getattr(choice, "finish_reason", last_finish)
                continue

            content_piece: Optional[str] = getattr(delta, "content", None)
            if content_piece:
                yield LlmStreamChunk(content=content_piece)

            streamed_tool_calls = getattr(delta, "tool_calls", None)
            if streamed_tool_calls:
                for tc in streamed_tool_calls:
                    idx = getattr(tc, "index", 0) or 0
                    b = tc_builders.setdefault(
                        idx, {"id": None, "name": None, "arguments": ""})
                    if getattr(tc, "id", None):
                        b["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            b["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            b["arguments"] = (b["arguments"] or "") + fn.arguments

            last_finish = getattr(choice, "finish_reason", last_finish)

        # 终结 chunk（工具调用或完成信号）——与父类一致
        final_tool_calls = []
        for b in tc_builders.values():
            if not b.get("name"):
                continue
            args_raw = b.get("arguments") or "{}"
            try:
                loaded = json.loads(args_raw)
                args_dict = loaded if isinstance(loaded, dict) else {"args": loaded}
            except Exception:
                args_dict = {"_raw": args_raw}
            final_tool_calls.append(
                ToolCall(
                    id=b.get("id") or "tool_call",
                    name=b["name"] or "tool",
                    arguments=args_dict,
                )
            )
        if final_tool_calls:
            yield LlmStreamChunk(tool_calls=final_tool_calls, finish_reason=last_finish)
        else:
            yield LlmStreamChunk(finish_reason=last_finish or "stop")


# --- 自定义 RunSqlTool（捕获生成的 SQL） ---

# 请求级 RLS 上下文与捕获状态（ContextVar：并发异步请求各自隔离，坑② 修复）
_rls_role_var: "contextvars.ContextVar[str]" = contextvars.ContextVar("rls_role", default="admin")
_rls_mvno_var: "contextvars.ContextVar[Any]" = contextvars.ContextVar("rls_mvno", default=None)
_last_sql_var: "contextvars.ContextVar[str]" = contextvars.ContextVar("captured_last_sql", default="")
_last_blocked_var: "contextvars.ContextVar[bool]" = contextvars.ContextVar("captured_last_blocked", default=False)
_last_block_reason_var: "contextvars.ContextVar[str]" = contextvars.ContextVar("captured_last_block_reason", default="")


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
        """获取最近一次执行的 SQL（请求级 ContextVar 优先）"""
        return _last_sql_var.get() or self._last_sql

    @property
    def last_blocked(self) -> bool:
        """最近一次 SQL 是否被安全网关拦截（请求级 ContextVar 优先）"""
        return _last_blocked_var.get() or self._last_blocked

    @property
    def last_block_reason(self) -> str:
        """最近一次拦截原因（请求级 ContextVar 优先）"""
        return _last_block_reason_var.get() or self._last_block_reason

    def set_user_context(self, role: str, mvno_id: Any) -> None:
        """设置当前查询的用户上下文（用于 RLS）

        使用 ContextVar 保存：并发异步请求各自持有独立上下文，
        避免共享单例字段被其他请求覆盖（坑② 修复）。
        """
        _rls_role_var.set(role)
        _rls_mvno_var.set(mvno_id)
        self._current_role = role
        self._current_mvno_id = mvno_id

    def reset_user_context(self) -> None:
        """重置用户上下文与捕获状态为默认值"""
        _rls_role_var.set("admin")
        _rls_mvno_var.set(None)
        _last_sql_var.set("")
        _last_blocked_var.set(False)
        _last_block_reason_var.set("")
        self._current_role = "admin"
        self._current_mvno_id = None
        self._last_sql = ""
        self._last_blocked = False
        self._last_block_reason = ""

    async def execute(self, context: ToolContext, args: RunSqlToolArgs) -> ToolResult:
        """执行 SQL 并捕获 SQL 语句，执行前进行安全校验 + RLS 注入"""
        # 读取请求级 RLS 上下文（ContextVar，随任务隔离）
        role = _rls_role_var.get()
        mvno_id = _rls_mvno_var.get()
        self._current_role = role
        self._current_mvno_id = mvno_id
        # 捕获 SQL 语句
        self._last_sql = args.sql if hasattr(args, 'sql') else ""
        self._last_blocked = False
        self._last_block_reason = ""
        _last_sql_var.set(self._last_sql)
        _last_blocked_var.set(False)
        _last_block_reason_var.set("")
        logger.debug("Captured SQL: %s", self._last_sql[:200])

        # --- SQL 安全网关校验 ---
        try:
            from app.core.sql_security import sql_gateway
            check_result = sql_gateway.validate_sql(self._last_sql)
            if not check_result.passed:
                # SQL 被拦截，不执行
                self._last_blocked = True
                self._last_block_reason = check_result.reason
                _last_blocked_var.set(True)
                _last_block_reason_var.set(check_result.reason)
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
                _last_sql_var.set(self._last_sql)

        except Exception as e:
            # 安全校验子系统自身故障：FAIL-CLOSED —— 宁可阻断，不可在未经验证下执行 SQL
            logger.error("SQL security check FAILED (fail-closed, blocking): %s", e)
            self._last_blocked = True
            self._last_block_reason = f"安全校验子系统异常，默认拦截: {e}"
            _last_blocked_var.set(True)
            _last_block_reason_var.set(self._last_block_reason)
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
                role=role,
                mvno_id=mvno_id,
            )
            if rls_result.rls_applied:
                logger.info(
                    "RLS injected: tables=%s, condition=%s",
                    rls_result.rls_tables, rls_result.rls_condition,
                )
                if rls_result.rls_condition == "no mvno_id: access denied (1=0)":
                    # 坑③ 修复：非 admin 用户无租户归属，必须拒绝而不是放行
                    self._last_blocked = True
                    self._last_block_reason = "当前用户无租户归属（mvno_id），已拒绝访问"
                    _last_blocked_var.set(True)
                    _last_block_reason_var.set(self._last_block_reason)
                    return ToolResult(
                        success=False,
                        result_for_llm=(
                            "访问被拒绝：当前账号未绑定运营商（mvno_id），"
                            "无法查询业务数据。请联系管理员绑定租户后重试。"
                        ),
                        error=self._last_block_reason,
                        metadata={
                            "blocked": True,
                            "layer": "rls",
                            "reason": self._last_block_reason,
                        },
                    )
                # 坑⑦：RLS 注入后二次校验（verify_rls），失败即拦截
                try:
                    ok, reason = rls_service.verify_rls(
                        sql=self._last_sql, role=role, mvno_id=mvno_id
                    )
                except Exception as e:
                    ok, reason = False, f"RLS 校验异常: {e}"
                if not ok:
                    self._last_blocked = True
                    self._last_block_reason = f"RLS 校验失败: {reason}"
                    _last_blocked_var.set(True)
                    _last_block_reason_var.set(self._last_block_reason)
                    return ToolResult(
                        success=False,
                        result_for_llm=(
                            f"SQL 安全拦截：{self._last_block_reason}\n"
                            f"该 SQL 语句已被安全网关阻止执行。"
                        ),
                        error=self._last_block_reason,
                        metadata={
                            "blocked": True,
                            "layer": "rls_verify",
                            "reason": self._last_block_reason,
                        },
                    )
                try:
                    args.sql = rls_result.sql
                except Exception:
                    pass
                self._last_sql = rls_result.sql
                _last_sql_var.set(self._last_sql)
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
                _last_sql_var.set(self._last_sql)
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
            #    使用 ResilientOpenAILlmService：为 Agent 生成路径加入
            #    指数退避 + 全抖动重连（连接/限流错误自动重试）
            llm_service = ResilientOpenAILlmService(
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

    async def aretrieve_context(self, question: str, max_items: int = 5) -> str:
        """retrieve_context 的异步版本，供 FastAPI async 请求路径调用

        内部把阻塞的检索操作丢进线程池，避免阻塞事件循环。http 模式下检索
        是一次网络往返，同步调用会让多副本的并发能力退化。
        详见 docs/pitfalls_chromadb_server.md 坑①。

        Args:
            question: 用户自然语言问题
            max_items: 每个 collection 检索的最大数量

        Returns:
            str: 拼接后的上下文文本。ChromaDB 不可用时返回空字符串。
        """
        if not self.chroma_available:
            return ""

        try:
            context = await chroma_store.aretrieve_context(
                question, max_items=max_items
            )
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
