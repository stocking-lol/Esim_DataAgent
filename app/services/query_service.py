"""
NL2SQL 查询服务
--------------
使用 Vanna 2.0 Agent 处理自然语言查询，提取 SQL 和数据结果。
支持普通查询和流式查询两种模式。

Vanna 2.0 Agent 返回 UiComponent 流：
  - SimpleTextComponent: 文本消息（状态更新、摘要等）
  - DataFrameComponent: 查询结果数据表
  - Plotly 图表等

本服务解析这些组件，提取 SQL 语句和数据结果。
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional

from vanna.core.components import UiComponent

from app.core.vanna_instance import vanna_manager
from app.config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class QueryResult:
    """NL2SQL 查询结果"""
    question: str
    sql: str = ""
    data: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    execution_time_ms: float = 0.0
    summary: str = ""
    error: Optional[str] = None
    conversation_id: Optional[str] = None
    blocked: bool = False                    # 是否被安全网关拦截
    block_reason: str = ""                   # 拦截原因
    masked_columns: list[str] = field(default_factory=list)  # 被脱敏的列
    retry_count: int = 0                     # 自我纠错重试次数
    corrections: list[str] = field(default_factory=list)  # 纠错历史（每次失败的错误信息）
    corrected_sql: str = ""                  # 最终修正成功的 SQL
    chart: dict = field(default_factory=dict)  # 图表配置（可视化）


async def execute_query(
    question: str,
    conversation_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    user_role: str = "admin",
    user_mvno_id: Optional[int] = None,
) -> QueryResult:
    """执行自然语言查询（非流式）

    使用 Vanna 2.0 Agent 处理查询，聚合所有 UiComponent 返回结构化结果。
    集成安全网关（输入过滤 + SQL 校验）、RLS 行级安全、数据脱敏和审计日志。

    Args:
        question: 用户自然语言问题
        conversation_id: 可选的多轮对话ID
        ip_address: 请求来源IP（用于审计）
        user_id: 用户ID（用于审计）
        username: 用户名（用于审计）
        user_role: 用户角色 (admin/analyst/viewer)，用于 RLS
        user_mvno_id: 用户所属 MVNO ID，用于 RLS 行级安全

    Returns:
        QueryResult: 包含 SQL、数据、执行时间等信息的结构化结果

    Raises:
        RuntimeError: Agent 未初始化
        ValueError: 查询无结果或执行失败
    """
    start_time = time.perf_counter()

    if not vanna_manager.is_initialized:
        raise RuntimeError("Vanna Agent 未初始化，请先调用 initialize()")

    agent = vanna_manager.agent
    request_context = vanna_manager.create_request_context()

    # --- 设置 RLS 用户上下文 ---
    if vanna_manager._run_sql_tool:
        vanna_manager._run_sql_tool.set_user_context(user_role, user_mvno_id)

    result = QueryResult(question=question, conversation_id=conversation_id)

    # --- 第一层安全：输入过滤 ---
    try:
        from app.core.sql_security import sql_gateway
        input_check = sql_gateway.check_input(question)
        if not input_check.passed:
            result.blocked = True
            result.block_reason = input_check.reason
            result.error = f"输入被安全网关拦截: {input_check.reason}"
            result.execution_time_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.warning("Input blocked: %s", input_check.reason)
            _audit_log(result, "blocked", ip_address, user_id, username)
            return result
    except Exception as e:
        logger.warning("Input filter error (allowing): %s", e)

    try:
        logger.info("Processing query: '%s'", question[:100])

        # 从 ChromaDB 检索训练上下文，增强 Agent 的 SQL 生成能力
        training_context = vanna_manager.retrieve_context(question)

        # --- 多轮对话上下文 ---
        conversation_context = ""
        if conversation_id:
            try:
                from app.services.conversation_service import ConversationService
                conv_svc = ConversationService()
                conversation_context = conv_svc.build_context_for_agent(
                    conversation_id=conversation_id,
                    current_question=question,
                )
                conv_svc.close()
                if conversation_context:
                    logger.debug("Loaded %d chars of conversation context",
                                 len(conversation_context))
            except Exception as e:
                logger.warning("Failed to load conversation context: %s", e)

        # 拼接最终问题：训练上下文 + 多轮上下文 + 原始问题
        parts = []
        if training_context:
            parts.append(f"请根据以下业务知识和数据库信息回答问题。\n\n{training_context}")
        if conversation_context:
            parts.append(conversation_context)

        # 显式约束：禁止模型查询系统表或执行 SHOW TABLES，避免安全拦截
        system_note = (
            "【重要约束】只允许使用上面给出的白名单业务表。"
            "禁止使用 information_schema、performance_schema、mysql 等系统表；"
            "禁止执行 SHOW TABLES、DESCRIBE、EXPLAIN 等元数据查询；"
            "不要编造不存在的表名。"
        )
        parts.append(system_note)

        if parts:
            parts.append(f"用户问题: {question}")
            augmented_question = "\n\n".join(parts)
            logger.debug("Query augmented: training=%d chars, conversation=%d chars",
                         len(training_context), len(conversation_context))
        else:
            augmented_question = question

        # 调用 Agent.send_message()，收集所有组件
        sql_statements: list[str] = []
        all_text: list[str] = []

        async for component in agent.send_message(
            request_context=request_context,
            message=augmented_question,
            conversation_id=conversation_id,
        ):
            extracted = _extract_from_component(component)
            if extracted["type"] == "sql":
                sql_statements.append(extracted["content"])
            elif extracted["type"] == "data":
                result.data = extracted["data"]
                result.columns = extracted["columns"]
                result.row_count = extracted["row_count"]
            elif extracted["type"] == "text":
                all_text.append(extracted["content"])
            elif extracted["type"] == "error":
                result.error = extracted["content"]
            # "skip" 类型：Vanna 内部 UI 组件，忽略

        # 聚合 SQL（优先从 CapturingRunSqlTool 获取）
        captured_sql = vanna_manager.get_last_sql()
        if captured_sql:
            result.sql = captured_sql.strip()
        else:
            for sql in reversed(sql_statements):
                if sql.strip():
                    result.sql = sql.strip()
                    break

        # 生成自然语言摘要（使用 LLM，替代原始组件文本拼接）
        if result.data is not None and result.sql and not result.blocked:
            try:
                from app.core.llm import llm_service
                result.summary = await llm_service.generate_summary(
                    question=question,
                    sql=result.sql,
                    data=result.data,
                    columns=result.columns,
                )
            except Exception as e:
                logger.warning("Summary generation failed, fallback to raw text: %s", e)
                if all_text:
                    result.summary = "\n".join(all_text[-3:])
        elif all_text:
            # 无数据时保留组件文本作为状态说明
            result.summary = "\n".join(all_text[-3:])

        elapsed = (time.perf_counter() - start_time) * 1000
        result.execution_time_ms = round(elapsed, 2)

        # --- 慢查询检测 ---
        slow_threshold_ms = settings.SLOW_QUERY_THRESHOLD_SECONDS * 1000
        if elapsed > slow_threshold_ms and not result.blocked:
            logger.warning(
                "Slow query detected: %.0fms (threshold=%ds) | SQL: %.200s | question: %.100s",
                elapsed, settings.SLOW_QUERY_THRESHOLD_SECONDS,
                result.sql, question,
            )

        # 检查 SQL 是否被安全网关拦截
        if vanna_manager._run_sql_tool and vanna_manager._run_sql_tool.last_blocked:
            result.blocked = True
            result.block_reason = vanna_manager._run_sql_tool.last_block_reason
            result.error = f"SQL 被安全网关拦截: {result.block_reason}"
            # 记录安全拦截业务指标
            try:
                from app.middleware.metrics import metrics
                metrics.record_security_block(reason=(result.block_reason or "unknown")[:50])
            except Exception:
                pass

        # --- 数据脱敏 ---
        if result.data and not result.blocked:
            try:
                from app.services.masking_service import masking_service
                result.data, result.masked_columns = masking_service.mask_query_result(
                    result.data, result.columns, role=user_role
                )
                if result.masked_columns:
                    logger.info("Masked columns: %s (role=%s)", result.masked_columns, user_role)
            except Exception as e:
                logger.warning("Data masking error: %s", e)

        # --- 可视化图表配置生成 ---
        if result.data and result.columns and not result.blocked:
            try:
                from app.services.visualization import generate_chart_config
                result.chart = generate_chart_config(
                    data=result.data,
                    columns=result.columns,
                    question=question,
                )
                logger.info("Chart config generated: type=%s", result.chart.get("type"))
            except Exception as e:
                logger.warning("Chart generation error: %s", e)

        if not result.sql and not result.data:
            result.error = "Agent 未生成 SQL 或返回数据"
            logger.warning("Query returned no SQL or data: '%s'", question[:100])
        else:
            logger.info(
                "Query completed: sql_preview='%.100s...', rows=%d, time=%.0fms",
                result.sql, result.row_count, elapsed,
            )

        # --- 审计日志 ---
        audit_status = "blocked" if result.blocked else ("error" if result.error else "success")
        _audit_log(result, audit_status, ip_address, user_id, username,
                   user_role=user_role, user_mvno_id=user_mvno_id)

        return result

    except Exception as e:
        elapsed = (time.perf_counter() - start_time) * 1000
        result.execution_time_ms = round(elapsed, 2)
        result.error = str(e)
        logger.error("Query failed: %s", e, exc_info=True)
        _audit_log(result, "error", ip_address, user_id, username,
                   user_role=user_role, user_mvno_id=user_mvno_id)
        return result
    finally:
        # 重置 RLS 用户上下文
        if vanna_manager._run_sql_tool:
            vanna_manager._run_sql_tool.reset_user_context()


async def execute_query_with_retry(
    question: str,
    conversation_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    user_role: str = "admin",
    user_mvno_id: Optional[int] = None,
    max_retries: int = 2,
) -> QueryResult:
    """带自我纠错回路的查询执行

    对 `execute_query` 进行外层包装：当查询因「可重试的 SQL 错误」
    （语法错误、表/列不存在、列歧义）失败时，将错误信息反馈给 LLM 重新生成 SQL，
    最多重试 `max_retries` 次。安全拦截、权限不足、超时等不可重试错误不会触发重试。

    Args:
        question: 用户自然语言问题
        conversation_id: 可选的多轮对话ID
        ip_address: 请求来源IP（用于审计）
        user_id: 用户ID（用于审计）
        username: 用户名（用于审计）
        user_role: 用户角色（用于 RLS）
        user_mvno_id: 用户所属 MVNO ID（用于 RLS）
        max_retries: 最大纠错重试次数（默认 2，即最多 3 次尝试）

    Returns:
        QueryResult: 包含纠错次数、纠错历史与最终 SQL
    """
    from app.core.error_classifier import (
        classify_sql_error,
        get_correction_hint,
    )
    from app.services.query_cache import query_cache

    last_result: Optional[QueryResult] = None
    augmented_question = question
    last_failed_error: Optional[str] = None

    # --- 查询缓存命中检查（相同问题+角色+MVNO 的成功结果）---
    cached = query_cache.get(question, user_role, user_mvno_id)
    if cached is not None:
        logger.info("Query cache HIT for role=%s, mvno=%s", user_role, user_mvno_id)
        return cached  # type: ignore[return-value]

    for attempt in range(1, max_retries + 2):  # 首次 + max_retries 次重试
        result = await execute_query(
            question=augmented_question,
            conversation_id=conversation_id,
            ip_address=ip_address,
            user_id=user_id,
            username=username,
            user_role=user_role,
            user_mvno_id=user_mvno_id,
        )
        last_result = result

        # 记录上一次失败尝试的错误信息（纠错历史），用于审计与前端展示
        if attempt > 1 and last_failed_error:
            result.corrections.append(last_failed_error)
            result.retry_count = attempt - 1

        # --- 决策：是否需要重试 ---
        # 安全拦截 / 权限不足 / 超时 -> 不重试
        if result.blocked:
            logger.info("Query blocked (no retry): %s", result.block_reason)
            break

        # 无错误 -> 成功，结束
        if not result.error:
            if attempt > 1:
                logger.info("Query succeeded after %d correction(s)", attempt - 1)
                # 记录自我纠错成功指标
                try:
                    from app.middleware.metrics import metrics
                    metrics.record_correction(success=True)
                except Exception:
                    pass
            break

        # 分类错误，判断是否可重试
        classification = classify_sql_error(result.error)
        if not classification.retryable or attempt >= max_retries + 1:
            if classification.retryable:
                logger.info(
                    "Retryable error but reached max attempts (%d): %s",
                    max_retries, classification.category,
                )
            else:
                logger.info(
                    "Non-retryable error (%s): %s",
                    classification.category, result.error[:120],
                )
            break

        # --- 触发纠错重试 ---
        hint = get_correction_hint(result.error)
        logger.warning(
            "Query attempt %d failed (%s), triggering correction. Error: %.120s",
            attempt, classification.category, result.error,
        )

        # 保存本次失败错误，供下一轮纠错历史记录
        last_failed_error = result.error

        # 构造带错误反馈的增强问题，引导 LLM 修正
        augmented_question = (
            f"{question}\n\n"
            f"【重要修正提示】你上一次生成的 SQL 执行失败，错误类型：{classification.category.value}。"
            f"{hint}\n"
            f"原始错误信息：{result.error}\n"
            f"请仔细检查表名与列名是否真实存在、语法是否正确，重新生成正确的 SQL。"
        )

    # 标记最终修正成功的 SQL
    if last_result is not None and not last_result.error and last_result.sql:
        last_result.corrected_sql = last_result.sql

    # --- 写入查询缓存（仅缓存成功结果）---
    if (
        last_result is not None
        and not last_result.error
        and not last_result.blocked
        and last_result.sql
    ):
        try:
            query_cache.put(question, user_role, user_mvno_id, last_result)
        except Exception as e:
            logger.warning("Query cache put error: %s", e)

    return last_result or QueryResult(question=question)


async def execute_query_stream(
    question: str,
    conversation_id: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """执行自然语言查询（流式 SSE）

    使用 Vanna 2.0 Agent 处理查询，逐步 yield SSE 事件。

    Args:
        question: 用户自然语言问题
        conversation_id: 可选的多轮对话ID

    Yields:
        dict: SSE 事件 {"type": "status"|"sql"|"data"|"summary"|"done"|"error", "data": ...}
    """
    start_time = time.perf_counter()

    if not vanna_manager.is_initialized:
        yield {"type": "error", "data": "Vanna Agent 未初始化"}
        return

    agent = vanna_manager.agent
    request_context = vanna_manager.create_request_context()

    try:
        # 状态：开始处理
        yield {"type": "status", "data": "正在分析问题..."}

        # 从 ChromaDB 检索训练上下文
        training_context = vanna_manager.retrieve_context(question)
        if training_context:
            augmented_question = (
                f"请根据以下业务知识和数据库信息回答问题。\n\n"
                f"{training_context}\n\n"
                f"用户问题: {question}"
            )
        else:
            augmented_question = question

        sql_found = False
        data_found = False

        async for component in agent.send_message(
            request_context=request_context,
            message=augmented_question,
            conversation_id=conversation_id,
        ):
            extracted = _extract_from_component(component)

            if extracted["type"] == "sql":
                sql_found = True
                yield {"type": "sql", "data": extracted["content"]}

            elif extracted["type"] == "data":
                data_found = True
                yield {
                    "type": "data",
                    "data": extracted["data"],
                    "columns": extracted["columns"],
                }

            elif extracted["type"] == "text":
                yield {"type": "status", "data": extracted["content"]}

            elif extracted["type"] == "error":
                yield {"type": "error", "data": extracted["content"]}

        # 完成
        elapsed = (time.perf_counter() - start_time) * 1000

        if not sql_found and not data_found:
            yield {"type": "error", "data": "Agent 未返回有效结果"}

        yield {
            "type": "done",
            "data": {
                "execution_time_ms": round(elapsed, 2),
                "sql_generated": sql_found,
                "data_returned": data_found,
            },
        }

    except Exception as e:
        logger.error("Stream query failed: %s", e, exc_info=True)
        yield {"type": "error", "data": str(e)}


def _audit_log(
    result: QueryResult,
    status: str,
    ip_address: Optional[str] = None,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    user_role: str = "admin",
    user_mvno_id: Optional[int] = None,
) -> None:
    """写入审计日志（静默失败，不影响主流程）"""
    try:
        from app.services.audit_service import audit_service
        audit_service.log_query(
            question=result.question,
            generated_sql=result.sql,
            execution_status=status,
            error_message=result.error if status != "success" else None,
            execution_time_ms=int(result.execution_time_ms),
            row_count=result.row_count,
            user_id=user_id,
            username=username,
            ip_address=ip_address,
            conversation_id=result.conversation_id,
        )

        # 记录 RLS 上下文到日志
        if user_role != "admin" and user_mvno_id is not None:
            logger.debug(
                "RLS context: role=%s, mvno_id=%d | user=%s | SQL: %.200s",
                user_role, user_mvno_id, username or user_id or "unknown", result.sql,
            )

        # 慢查询审计警告
        slow_threshold_ms = settings.SLOW_QUERY_THRESHOLD_SECONDS * 1000
        if status == "success" and result.execution_time_ms > slow_threshold_ms:
            logger.warning(
                "Slow query audit: %.0fms (threshold=%ds) | user=%s | ip=%s | SQL: %.200s",
                result.execution_time_ms,
                settings.SLOW_QUERY_THRESHOLD_SECONDS,
                username or user_id or "unknown",
                ip_address or "unknown",
                result.sql,
            )
    except Exception:
        pass  # 审计日志失败不影响主流程


def _extract_from_component(component: UiComponent) -> dict:
    """从 UiComponent 中提取结构化信息

    Vanna 2.0 的 UiComponent 结构：
    {
        "timestamp": "...",
        "rich_component": <DataFrameComponent | SimpleTextComponent | ...>,
        "simple_component": <SimpleTextComponent | None>
    }

    Returns:
        dict: {"type": "sql"|"data"|"text"|"error"|"skip", ...}
    """
    try:
        rich = component.rich_component
        simple = component.simple_component

        # 调试：记录组件类型
        comp_type = type(rich).__name__ if rich else (type(simple).__name__ if simple else "None")
        logger.debug("Component type: %s", comp_type)

        # 跳过 Vanna Agent 内部 UI 状态组件（避免污染摘要/上下文）
        skip_types = {"StatusBarUpdate", "ChatInputUpdate", "StatusBar", "ChatInput"}
        if comp_type in skip_types:
            return {"type": "skip"}

        # --- 处理 rich_component ---
        if rich is not None:
            # DataFrameComponent / 有 rows+columns 的数据组件
            if hasattr(rich, "rows") and hasattr(rich, "columns"):
                rows = getattr(rich, "rows", [])
                cols = getattr(rich, "columns", [])
                if rows and cols:
                    logger.debug("Extracted data: %d rows, %d columns", len(rows), len(cols))
                    return {
                        "type": "data",
                        "data": rows,
                        "columns": [str(c) for c in cols],
                        "row_count": len(rows),
                    }
                return {"type": "text", "content": "查询无结果"}

            # 带 text 属性的组件（SimpleTextComponent, CodeBlockComponent 等）
            if hasattr(rich, "text"):
                text = getattr(rich, "text", "")
                if not text:
                    return {"type": "text", "content": str(rich)}

                # 检测是否是 SQL / 代码块
                if _looks_like_sql(text):
                    return {"type": "sql", "content": text}

                # 检测 Markdown 代码块 (```sql ... ```)
                md_sql = _extract_sql_from_markdown(text)
                if md_sql:
                    return {"type": "sql", "content": md_sql}

                return {"type": "text", "content": str(text)}

            # 带 content 属性的组件
            if hasattr(rich, "content"):
                content = getattr(rich, "content", "")
                if content:
                    if _looks_like_sql(str(content)):
                        return {"type": "sql", "content": str(content)}
                    return {"type": "text", "content": str(content)[:500]}

            # dict 形式
            if isinstance(rich, dict):
                if "sql" in rich:
                    return {"type": "sql", "content": rich["sql"]}
                if "text" in rich:
                    return {"type": "text", "content": rich["text"]}
                if "rows" in rich:
                    return {
                        "type": "data",
                        "data": rich["rows"],
                        "columns": rich.get("columns", []),
                        "row_count": len(rich["rows"]),
                    }

            # 默认：序列化
            logger.debug("Unknown rich_component type: %s", type(rich).__name__)
            return {"type": "text", "content": str(rich)[:500]}

        # --- 处理 simple_component ---
        if simple is not None:
            if hasattr(simple, "text"):
                text = getattr(simple, "text", "")
                return {"type": "text", "content": str(text)}
            if hasattr(simple, "content"):
                return {"type": "text", "content": str(getattr(simple, "content", ""))[:500]}
            return {"type": "text", "content": str(simple)[:300]}

        # 兜底：检查 component 本身
        comp_dict = component.model_dump() if hasattr(component, "model_dump") else {}
        logger.debug("Fallback component dump keys: %s", list(comp_dict.keys())[:10])
        return {"type": "text", "content": str(comp_dict.get("content", ""))[:200]}

    except Exception as e:
        logger.debug("Failed to extract component: %s", e)
        return {"type": "text", "content": str(component)[:200]}


def _extract_sql_from_markdown(text: str) -> str:
    """从 Markdown 代码块中提取 SQL"""
    import re
    pattern = r'```(?:sql)?\s*\n(.*?)\n```'
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""


def _looks_like_sql(text: str) -> bool:
    """检测文本是否像 SQL 语句"""
    text_stripped = text.strip().upper()
    sql_keywords = ["SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "CREATE",
                    "ALTER", "DROP", "SHOW", "DESCRIBE", "EXPLAIN"]
    for kw in sql_keywords:
        if text_stripped.startswith(kw):
            return True
    return False


def _add_timeout_hint(sql: str) -> str:
    """为 SQL 添加 MAX_EXECUTION_TIME 超时提示

    在 SELECT 语句中注入 MySQL 优化器提示 /*+ MAX_EXECUTION_TIME(ms) */，
    超时后 MySQL 会自动终止查询。对非 SELECT 语句不做修改。

    Args:
        sql: 原始 SQL 语句

    Returns:
        str: 添加了超时提示的 SQL 语句
    """
    if not sql or not sql.strip():
        return sql

    timeout_ms = settings.QUERY_TIMEOUT_SECONDS * 1000
    stripped = sql.lstrip()

    # 去除前导注释/空白，找到第一个有效关键字
    upper = stripped.upper()

    # 只对 SELECT 查询添加超时提示
    if upper.startswith("SELECT"):
        # 在 SELECT 后注入提示
        # 匹配 "SELECT" 或 "SELECT DISTINCT" 等
        insert_pos = len("SELECT")
        # 检查是否已有 MAX_EXECUTION_TIME 提示（避免重复添加）
        if "MAX_EXECUTION_TIME" in upper[:200]:
            return sql
        # 计算原始 sql 中的前导空白长度
        leading = len(sql) - len(stripped)
        return sql[:leading + insert_pos] + f" /*+ MAX_EXECUTION_TIME({timeout_ms}) */" + sql[leading + insert_pos:]

    # WITH ... SELECT 也可以加，但 MySQL 的 MAX_EXECUTION_TIME 只对 SELECT 有效
    # 对 WITH 语句，在末尾的 SELECT 上加比较复杂，这里简化处理
    return sql
