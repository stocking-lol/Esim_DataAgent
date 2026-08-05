"""
NL2SQL 查询 API
---------------
提供自然语言查询接口：普通查询和流式 SSE 查询。

POST /api/v1/query        - 普通查询，返回 SQL + 数据
POST /api/v1/query/stream - 流式 SSE 查询，逐步返回进度
GET  /api/v1/query/status - 查询服务状态
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.services.query_service import execute_query, execute_query_with_retry, execute_query_stream
from app.core.vanna_instance import vanna_manager
from app.core.auth import get_optional_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["NL2SQL Query"])


# --- 请求/响应模型 ---

class QueryRequest(BaseModel):
    """查询请求"""
    question: str = Field(
        ..., min_length=1, max_length=500,
        description="自然语言查询问题",
        examples=["本月新增多少eSIM用户"],
    )
    conversation_id: Optional[str] = Field(
        None, description="多轮对话ID（可选）",
    )


class QueryResponse(BaseModel):
    """查询响应"""
    code: int = 200
    message: str = "success"
    data: dict = Field(default_factory=dict)


# --- 辅助函数 ---

def _get_client_ip(request: Request) -> str:
    """获取客户端真实 IP"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


# --- API 端点 ---

@router.get("/status")
async def query_status() -> QueryResponse:
    """查询服务状态"""
    return QueryResponse(
        data={
            "service": "NL2SQL Query Service",
            "agent_initialized": vanna_manager.is_initialized,
            "llm_model": "deepseek-chat",
        }
    )


@router.post("", response_model=QueryResponse)
async def natural_language_query(
    request: QueryRequest,
    http_request: Request,
) -> QueryResponse:
    """自然语言查询（非流式）

    接收自然语言问题，返回生成的 SQL 和查询结果。
    集成安全网关、数据脱敏和审计日志。
    """
    logger.info("POST /query: '%s'", request.question[:100])

    # 获取可选用户信息
    try:
        from app.core.auth import JWTManager
        from fastapi.security import HTTPBearer
        auth_header = http_request.headers.get("Authorization", "")
        user_info = None
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            payload = JWTManager.verify_token(token)
            user_info = {
                "user_id": int(payload.get("sub", 0)),
                "username": payload.get("username", ""),
            }
    except Exception:
        user_info = None

    ip_address = _get_client_ip(http_request)

    # --- 第一层安全：输入过滤（防御降级）---
    # 即使 Vanna Agent 未初始化，也先执行输入过滤，阻断明显的注入/Prompt注入，
    # 并返回 1001；同时写入审计日志，保证安全事件可追溯。
    try:
        from app.core.sql_security import sql_gateway
        input_check = sql_gateway.check_input(request.question)
        if not input_check.passed:
            logger.warning("Input blocked at API layer: %s", input_check.reason)
            try:
                from app.services.audit_service import audit_service
                audit_service.log_query(
                    question=request.question,
                    generated_sql="",
                    execution_status="blocked",
                    error_message=f"输入被安全网关拦截: {input_check.reason}",
                    execution_time_ms=0,
                    row_count=0,
                    user_id=user_info["user_id"] if user_info else None,
                    username=user_info["username"] if user_info else None,
                    ip_address=ip_address,
                    conversation_id=request.conversation_id,
                )
            except Exception:
                pass
            return QueryResponse(
                code=1001,
                message=f"安全拦截: {input_check.reason}",
                data={
                    "question": request.question,
                    "blocked": True,
                    "block_reason": input_check.reason,
                    "execution_time_ms": 0,
                },
            )
    except Exception as e:
        logger.warning("Input filter error at API layer (allowing): %s", e)

    # Agent 初始化检查
    if not vanna_manager.is_initialized:
        raise HTTPException(
            status_code=503,
            detail="Vanna Agent 未初始化，服务暂不可用",
        )

    result = await execute_query_with_retry(
        question=request.question,
        conversation_id=request.conversation_id,
        ip_address=ip_address,
        user_id=user_info["user_id"] if user_info else None,
        username=user_info["username"] if user_info else None,
    )

    # 安全拦截
    if result.blocked:
        http_request.state._security_blocked = True
        return QueryResponse(
            code=1001,
            message=f"安全拦截: {result.block_reason}",
            data={
                "question": result.question,
                "sql": result.sql,
                "blocked": True,
                "block_reason": result.block_reason,
                "execution_time_ms": result.execution_time_ms,
            },
        )

    if result.error and not result.sql and not result.data:
        return QueryResponse(
            code=500,
            message=f"查询执行失败: {result.error}",
            data={
                "question": result.question,
                "sql": result.sql,
                "data": [],
                "columns": [],
                "row_count": 0,
                "execution_time_ms": result.execution_time_ms,
                "error": result.error,
            },
        )

    return QueryResponse(
        data={
            "question": result.question,
            "sql": result.sql,
            "data": result.data if len(result.data) <= 100 else result.data[:100],
            "columns": result.columns,
            "row_count": result.row_count,
            "execution_time_ms": result.execution_time_ms,
            "summary": result.summary,
            "truncated": len(result.data) > 100,
            "conversation_id": result.conversation_id,
            "masked_columns": result.masked_columns,
            "retry_count": result.retry_count,
            "corrections": result.corrections,
            "chart": result.chart,
        },
    )


@router.post("/stream")
async def natural_language_query_stream(request: QueryRequest):
    """自然语言查询（流式 SSE）

    使用 Server-Sent Events 逐步返回查询进度和结果。

    SSE 事件类型:
    - status: 状态更新消息
    - sql: 生成的 SQL 语句
    - data: 查询结果数据
    - done: 查询完成（含执行时间）
    - error: 错误信息
    """
    if not vanna_manager.is_initialized:
        return EventSourceResponse(
            _error_generator("Vanna Agent 未初始化"),
            media_type="text/event-stream",
        )

    logger.info("POST /query/stream: '%s'", request.question[:100])

    async def event_generator():
        try:
            async for event in execute_query_stream(
                question=request.question,
                conversation_id=request.conversation_id,
            ):
                yield {
                    "event": event["type"],
                    "data": json.dumps(event, ensure_ascii=False),
                }
        except Exception as e:
            logger.error("SSE stream error: %s", e)
            yield {
                "event": "error",
                "data": json.dumps({"type": "error", "data": str(e)}, ensure_ascii=False),
            }

    return EventSourceResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _error_generator(message: str):
    """生成 SSE 错误事件"""
    yield {
        "event": "error",
        "data": json.dumps({"type": "error", "data": message}, ensure_ascii=False),
    }
