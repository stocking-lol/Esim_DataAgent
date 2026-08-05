"""
API v1 路由聚合
"""
from fastapi import APIRouter

from app.api.v1 import query, train, auth, conversation, admin

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
api_router.include_router(query.router, prefix="/query", tags=["Query"])
api_router.include_router(train.router, prefix="/train", tags=["Train"])
api_router.include_router(conversation.router, prefix="/conversation", tags=["Conversation"])
api_router.include_router(admin.router, prefix="/admin", tags=["Admin"])
