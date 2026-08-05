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


async def execute_query(
    question: str,
    conversation_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
) -> QueryResult:
    """执行自然语言查询（非流式）

    使用 Vanna 2.0 Agent 处理查询，聚合所有 UiComponent 返回结构化结果。
    集成安全网关（输入过滤 + SQL 校验）、数据脱敏和审计日志。

    Args:
        question: 用户自然语言问题
        conversation_id: 可选的多轮对话ID
        ip_address: 请求来源IP（用于审计）
        user_id: 用户ID（用于审计）
        username: 用户名（用于审计）

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
        if training_context:
            # 将训练上下文注入到问题中，帮助 LLM 理解业务术语和表结构
            augmented_question = (
                f"请根据以下业务知识和数据库信息回答问题。\n\n"
                f"{training_context}\n\n"
                f"用户问题: {question}"
            )
            logger.debug("Query augmented with %d chars of training context",
                         len(training_context))
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

        # 聚合 SQL（优先从 CapturingRunSqlTool 获取）
        captured_sql = vanna_manager.get_last_sql()
        if captured_sql:
            result.sql = captured_sql.strip()
        else:
            for sql in reversed(sql_statements):
                if sql.strip():
                    result.sql = sql.strip()
                    break

        # 聚合摘要文本
        if all_text:
            result.summary = "\n".join(all_text[-3:])  # 取最后3条作为摘要

        elapsed = (time.perf_counter() - start_time) * 1000
        result.execution_time_ms = round(elapsed, 2)

        # 检查 SQL 是否被安全网关拦截
        if vanna_manager._run_sql_tool and vanna_manager._run_sql_tool.last_blocked:
            result.blocked = True
            result.block_reason = vanna_manager._run_sql_tool.last_block_reason
            result.error = f"SQL 被安全网关拦截: {result.block_reason}"

        # --- 数据脱敏 ---
        if result.data and not result.blocked:
            try:
                from app.services.masking_service import masking_service
                result.data, result.masked_columns = masking_service.mask_query_result(
                    result.data, result.columns
                )
                if result.masked_columns:
                    logger.info("Masked columns: %s", result.masked_columns)
            except Exception as e:
                logger.warning("Data masking error: %s", e)

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
        _audit_log(result, audit_status, ip_address, user_id, username)

        return result

    except Exception as e:
        elapsed = (time.perf_counter() - start_time) * 1000
        result.execution_time_ms = round(elapsed, 2)
        result.error = str(e)
        logger.error("Query failed: %s", e, exc_info=True)
        _audit_log(result, "error", ip_address, user_id, username)
        return result


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
        dict: {"type": "sql"|"data"|"text"|"error", "content": ..., ...}
    """
    try:
        rich = component.rich_component
        simple = component.simple_component

        # 调试：记录组件类型
        comp_type = type(rich).__name__ if rich else (type(simple).__name__ if simple else "None")
        logger.debug("Component type: %s", comp_type)

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
