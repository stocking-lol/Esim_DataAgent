"""
认证 API
-------
提供用户注册、登录、令牌刷新、用户信息及资料更新接口。
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field

from app.core.auth import (
    JWTManager,
    auth_manager,
    get_current_user,
    login_with_fallback,
)
from app.services.auth_service import auth_service
from app.utils.errors import AuthenticationException, BadRequestException

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================
# 请求/响应模型
# ============================================================

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class RegisterRequest(BaseModel):
    """用户注册请求"""
    username: str = Field(..., min_length=3, max_length=100, description="用户名")
    email: EmailStr = Field(..., description="邮箱")
    password: str = Field(..., min_length=6, max_length=128, description="密码")
    role: str = Field(default="analyst", description="角色: admin/analyst/viewer")
    mvno_id: int | None = Field(default=None, description="关联 MVNO ID（可选）")


class UpdateProfileRequest(BaseModel):
    """更新个人资料请求"""
    email: EmailStr = Field(..., description="新邮箱")


# ============================================================
# 端点
# ============================================================

@router.post("/register", response_model=dict)
async def register(req: RegisterRequest):
    """用户注册

    创建新用户账号。默认角色为 analyst，可指定 role 和 mvno_id。

    Args:
        req: 注册请求（用户名、邮箱、密码、可选角色和 mvno_id）

    Returns:
        dict: 注册成功后的用户信息
    """
    try:
        user = auth_service.register_user(
            username=req.username,
            email=req.email,
            password=req.password,
            role=req.role,
            mvno_id=req.mvno_id,
        )
        return {
            "code": 200,
            "message": "注册成功",
            "data": user.to_dict(),
        }
    except ValueError as e:
        raise BadRequestException(str(e))


@router.post("/login", response_model=dict)
async def login(req: LoginRequest):
    """用户登录，获取 JWT 令牌

    优先使用数据库认证，回退到演示账号。
    演示账号：
    - admin / esim_admin_2026 （管理员）
    - analyst / esim_analyst_2026 （分析师）
    """
    result = login_with_fallback(req.username, req.password)
    if not result:
        raise AuthenticationException("用户名或密码错误")

    return {
        "code": 200,
        "message": "登录成功",
        "data": result,
    }


@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    """获取当前用户信息"""
    # 尝试从数据库获取更完整的用户信息
    db_user = auth_service.get_user_by_id(user["user_id"])
    if db_user:
        return {
            "code": 200,
            "message": "success",
            "data": db_user.to_dict(),
        }
    # 回退到 JWT payload 中的信息
    return {
        "code": 200,
        "message": "success",
        "data": user,
    }


@router.put("/me", response_model=dict)
async def update_profile(
    req: UpdateProfileRequest,
    user: dict = Depends(get_current_user),
):
    """更新当前用户资料（仅邮箱）

    Args:
        req: 包含新邮箱的请求
        user: 当前登录用户

    Returns:
        dict: 更新后的用户信息
    """
    try:
        updated_user = auth_service.update_email(user["user_id"], req.email)
        return {
            "code": 200,
            "message": "资料更新成功",
            "data": updated_user.to_dict(),
        }
    except ValueError as e:
        raise BadRequestException(str(e))


@router.post("/refresh")
async def refresh_token(user: dict = Depends(get_current_user)):
    """刷新令牌"""
    new_token = JWTManager.create_access_token({
        "sub": str(user["user_id"]),
        "username": user["username"],
        "role": user["role"],
    })
    return {
        "code": 200,
        "message": "令牌刷新成功",
        "data": {
            "access_token": new_token,
            "token_type": "bearer",
        },
    }
