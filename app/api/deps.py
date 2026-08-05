"""
依赖注入模块
-----------
提供认证检查、数据库会话等 FastAPI 依赖项。
"""
from fastapi import Depends, Header

from app.core.auth import get_current_user, get_optional_user, require_admin


# 重新导出常用依赖
__all__ = [
    "get_current_user",
    "get_optional_user",
    "require_admin",
    "get_db",
]


# 数据库依赖（从 database 模块导入）
from app.config.database import get_db
