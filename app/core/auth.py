"""
JWT 认证模块
-----------
提供 JWT 令牌的生成、验证和 FastAPI 依赖注入。
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config.settings import settings

logger = logging.getLogger(__name__)

# Bearer token scheme
security_scheme = HTTPBearer(auto_error=False)


class JWTManager:
    """JWT 令牌管理器"""

    @staticmethod
    def create_access_token(
        data: dict,
        expires_delta: Optional[timedelta] = None,
    ) -> str:
        """生成 JWT access token

        Args:
            data: 要编码的数据（必须包含 sub/user_id）
            expires_delta: 过期时间增量

        Returns:
            str: JWT 令牌
        """
        to_encode = data.copy()
        expire = datetime.now(timezone.utc) + (
            expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        )
        to_encode.update({
            "exp": expire,
            "iat": datetime.now(timezone.utc),
        })
        return jwt.encode(
            to_encode,
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )

    @staticmethod
    def verify_token(token: str) -> dict:
        """验证 JWT 令牌

        Args:
            token: JWT 令牌字符串

        Returns:
            dict: 解码后的 payload

        Raises:
            HTTPException: 令牌无效或过期
        """
        try:
            payload = jwt.decode(
                token,
                settings.JWT_SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM],
            )
            return payload
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="令牌已过期",
            )
        except jwt.InvalidTokenError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="令牌无效",
            )


# 模拟用户数据库（Day 5 临时方案，后续可对接真实用户系统）
DEMO_USERS = {
    "admin": {
        "user_id": 1,
        "username": "admin",
        "password": "esim_admin_2026",
        "role": "admin",
    },
    "analyst": {
        "user_id": 2,
        "username": "analyst",
        "password": "esim_analyst_2026",
        "role": "analyst",
    },
}


class AuthManager:
    """认证管理器"""

    @staticmethod
    def authenticate(username: str, password: str) -> Optional[dict]:
        """验证用户名密码

        Returns:
            dict: 用户信息（不含密码）或 None
        """
        user = DEMO_USERS.get(username)
        if user and user["password"] == password:
            return {
                "user_id": user["user_id"],
                "username": user["username"],
                "role": user["role"],
            }
        return None

    @staticmethod
    def login(username: str, password: str) -> Optional[dict]:
        """用户登录，返回 token 和用户信息"""
        user = AuthManager.authenticate(username, password)
        if not user:
            return None

        token = JWTManager.create_access_token({
            "sub": str(user["user_id"]),
            "username": user["username"],
            "role": user["role"],
        })

        return {
            "access_token": token,
            "token_type": "bearer",
            "user": user,
        }


class DBAuthManager:
    """基于数据库的认证管理器

    查询 app_users 表进行认证。
    若数据库不可用，调用方可回退到 AuthManager（DEMO_USERS）。
    """

    @staticmethod
    def authenticate(username: str, password: str) -> Optional[dict]:
        """通过 app_users 表验证用户名密码

        Returns:
            dict: 用户信息（不含密码）或 None
        """
        try:
            from app.services.auth_service import auth_service

            user = auth_service.authenticate_user(username, password)
            if not user:
                return None
            return {
                "user_id": user.id,
                "username": user.username,
                "role": user.role,
                "mvno_id": user.mvno_id,
            }
        except Exception as e:
            logger.warning("DBAuthManager 查询失败，将回退到 DEMO_USERS: %s", e)
            return None

    @staticmethod
    def login(username: str, password: str) -> Optional[dict]:
        """用户登录，返回 token 和用户信息

        优先使用数据库认证，失败返回 None（调用方可回退到 AuthManager）。
        """
        user = DBAuthManager.authenticate(username, password)
        if not user:
            return None

        token = JWTManager.create_access_token({
            "sub": str(user["user_id"]),
            "username": user["username"],
            "role": user["role"],
        })

        return {
            "access_token": token,
            "token_type": "bearer",
            "user": user,
        }


# 全局实例
jwt_manager = JWTManager()
auth_manager = AuthManager()
db_auth_manager = DBAuthManager()


def login_with_fallback(username: str, password: str) -> Optional[dict]:
    """登录流程：DB 优先，回退到 DEMO_USERS

    Args:
        username: 用户名
        password: 密码

    Returns:
        dict: 登录结果（含 token 和用户信息）或 None
    """
    # 1. 尝试数据库认证
    result = DBAuthManager.login(username, password)
    if result:
        return result

    # 2. 回退到内存中的 DEMO_USERS
    result = AuthManager.login(username, password)
    if result:
        logger.info("用户 '%s' 通过 DEMO_USERS 认证（数据库中未找到）", username)
        return result

    return None


# ============================================================
# FastAPI 依赖注入
# ============================================================

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
) -> dict:
    """获取当前认证用户（必需）

    用法:
        @router.get("/protected")
        async def protected(user: dict = Depends(get_current_user)):
            ...
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = JWTManager.verify_token(credentials.credentials)
    return {
        "user_id": int(payload.get("sub", 0)),
        "username": payload.get("username", ""),
        "role": payload.get("role", ""),
    }


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
) -> Optional[dict]:
    """获取当前用户（可选，未认证返回 None）"""
    if credentials is None:
        return None
    try:
        payload = JWTManager.verify_token(credentials.credentials)
        return {
            "user_id": int(payload.get("sub", 0)),
            "username": payload.get("username", ""),
            "role": payload.get("role", ""),
        }
    except HTTPException:
        return None


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """要求管理员权限"""
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return user
