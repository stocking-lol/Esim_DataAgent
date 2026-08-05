"""
对话管理 API
"""
from fastapi import APIRouter

router = APIRouter()


@router.get("/status")
async def conversation_status():
    """检查对话服务状态"""
    return {
        "code": 200,
        "message": "Conversation service is running",
        "data": {"status": "ready", "implementation": "pending"},
    }
