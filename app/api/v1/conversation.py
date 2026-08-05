"""
对话管理 API
------------
提供多轮对话的完整 CRUD 和消息发送功能。
支持在已有对话中继续提问，自动注入多轮上下文。

POST   /api/v1/conversation              - 创建新对话
GET    /api/v1/conversation              - 获取对话列表
GET    /api/v1/conversation/{id}         - 获取对话详情（含消息）
DELETE /api/v1/conversation/{id}         - 删除对话
POST   /api/v1/conversation/{id}/messages - 在对话中发送新消息
GET    /api/v1/conversation/status       - 服务状态
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.core.auth import get_current_user, get_optional_user
from app.services.conversation_service import ConversationService
from app.services.query_service import execute_query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Conversation"])


# --- 请求/响应模型 ---

class CreateConversationRequest(BaseModel):
    """创建对话请求"""
    title: Optional[str] = Field(
        None, max_length=200,
        description="对话标题（可选，默认自动生成）",
    )


class SendMessageRequest(BaseModel):
    """在对话中发送消息请求"""
    question: str = Field(
        ..., min_length=1, max_length=500,
        description="自然语言查询问题",
        examples=["本月各套餐销量"],
    )


class ApiResponse(BaseModel):
    """统一响应格式"""
    code: int = 200
    message: str = "success"
    data: Optional[dict] = None


# --- API 端点 ---

@router.get("/status")
async def conversation_status() -> ApiResponse:
    """检查对话服务状态"""
    return ApiResponse(
        data={
            "status": "ready",
            "implementation": "complete",
            "features": [
                "create_conversation",
                "list_conversations",
                "get_conversation_detail",
                "delete_conversation",
                "send_message_with_context",
            ],
        }
    )


@router.post("", response_model=ApiResponse)
async def create_conversation(
    req: CreateConversationRequest,
    user: Optional[dict] = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """创建新对话

    创建一个新的对话会话。登录用户自动关联 user_id 和 username。
    """
    svc = ConversationService(db)
    try:
        conv = svc.create_conversation(
            user_id=user["user_id"] if user else None,
            username=user["username"] if user else None,
            title=req.title,
        )
        return ApiResponse(
            data={
                "conversation": conv.to_dict(),
                "message": "对话创建成功",
            }
        )
    finally:
        svc.close()


@router.get("", response_model=ApiResponse)
async def list_conversations(
    user: Optional[dict] = Depends(get_optional_user),
    limit: int = Query(20, ge=1, le=100, description="返回条数"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """获取对话列表

    登录用户只能看到自己的对话；未登录用户返回空列表。
    """
    svc = ConversationService(db)
    try:
        if user:
            conversations = svc.list_conversations(
                user_id=user["user_id"],
                limit=limit,
                offset=offset,
            )
        else:
            conversations = []

        return ApiResponse(
            data={
                "conversations": conversations,
                "total": len(conversations),
                "limit": limit,
                "offset": offset,
            }
        )
    finally:
        svc.close()


@router.get("/{conversation_id}", response_model=ApiResponse)
async def get_conversation_detail(
    conversation_id: str,
    db: Session = Depends(get_db),
) -> ApiResponse:
    """获取对话详情（含所有消息）

    返回对话元数据和按时间正序排列的所有消息。
    """
    svc = ConversationService(db)
    try:
        result = svc.get_conversation_with_messages(conversation_id)
        if not result:
            return ApiResponse(
                code=404,
                message="对话不存在",
                data=None,
            )
        return ApiResponse(data=result)
    finally:
        svc.close()


@router.delete("/{conversation_id}", response_model=ApiResponse)
async def delete_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
) -> ApiResponse:
    """删除对话

    级联删除该对话下的所有消息。不可恢复。
    """
    svc = ConversationService(db)
    try:
        deleted = svc.delete_conversation(conversation_id)
        if not deleted:
            return ApiResponse(
                code=404,
                message="对话不存在",
                data=None,
            )
        return ApiResponse(
            data={
                "conversation_id": conversation_id,
                "deleted": True,
            }
        )
    finally:
        svc.close()


@router.post("/{conversation_id}/messages", response_model=ApiResponse)
async def send_message(
    conversation_id: str,
    req: SendMessageRequest,
    http_request: Request,
    user: Optional[dict] = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> ApiResponse:
    """在对话中发送新消息（支持多轮上下文）

    核心多轮对话端点。自动加载对话历史（最近5轮），
    拼接到 LLM prompt 中，支持追问场景如：
    - "改成按月统计"
    - "上次查的数据再按地区分组"
    - "其中漫游包占比多少"

    流程:
    1. 验证对话存在
    2. 加载历史消息构建上下文
    3. 调用 NL2SQL 查询（注入上下文）
    4. 将问答保存到对话历史
    5. 返回查询结果

    Returns:
        包含 SQL、数据、对话ID 的查询结果
    """
    svc = ConversationService(db)
    try:
        # 1. 验证对话存在
        conv = svc.get_conversation(conversation_id)
        if not conv:
            return ApiResponse(
                code=404,
                message="对话不存在",
                data=None,
            )

        # 2. 检查 Agent 是否可用
        from app.core.vanna_instance import vanna_manager
        if not vanna_manager.is_initialized:
            # Agent 未初始化，仍保存用户消息，返回 503
            svc.add_message(
                conversation_id=conversation_id,
                role="user",
                content=req.question,
            )
            svc.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content="查询服务暂不可用，请稍后重试。",
                sql_status="error",
                error_message="Vanna Agent 未初始化",
            )
            return ApiResponse(
                code=503,
                message="查询服务暂不可用，Vanna Agent 未初始化",
                data={
                    "conversation_id": conversation_id,
                    "question": req.question,
                },
            )

        # 3. 获取客户端 IP
        forwarded = http_request.headers.get("X-Forwarded-For")
        if forwarded:
            ip_address = forwarded.split(",")[0].strip()
        else:
            ip_address = http_request.client.host if http_request.client else "unknown"

        # 4. 执行查询（query_service 内部已集成多轮上下文加载）
        result = await execute_query(
            question=req.question,
            conversation_id=conversation_id,
            ip_address=ip_address,
            user_id=user["user_id"] if user else None,
            username=user["username"] if user else None,
        )

        # 5. 保存问答到对话历史
        sql_status = "blocked" if result.blocked else ("error" if result.error else "success")
        svc.save_query_turn(
            conversation_id=conversation_id,
            question=req.question,
            sql=result.sql,
            data_summary=result.summary,
            row_count=result.row_count,
            execution_time_ms=result.execution_time_ms,
            sql_status=sql_status,
            error_message=result.error if sql_status != "success" else None,
        )

        # 6. 返回结果
        if result.blocked:
            return ApiResponse(
                code=1001,
                message=f"安全拦截: {result.block_reason}",
                data={
                    "conversation_id": conversation_id,
                    "question": result.question,
                    "sql": result.sql,
                    "blocked": True,
                    "block_reason": result.block_reason,
                    "execution_time_ms": result.execution_time_ms,
                },
            )

        if result.error and not result.sql and not result.data:
            return ApiResponse(
                code=500,
                message=f"查询失败: {result.error}",
                data={
                    "conversation_id": conversation_id,
                    "question": result.question,
                    "sql": result.sql,
                    "data": [],
                    "columns": [],
                    "row_count": 0,
                    "execution_time_ms": result.execution_time_ms,
                    "error": result.error,
                },
            )

        return ApiResponse(
            data={
                "conversation_id": conversation_id,
                "question": result.question,
                "sql": result.sql,
                "data": result.data if len(result.data) <= 100 else result.data[:100],
                "columns": result.columns,
                "row_count": result.row_count,
                "execution_time_ms": result.execution_time_ms,
                "summary": result.summary,
                "truncated": len(result.data) > 100,
                "masked_columns": result.masked_columns,
            }
        )
    finally:
        svc.close()
