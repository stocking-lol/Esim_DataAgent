"""
认证 API
-------
提供用户登录、令牌刷新、用户信息接口。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import auth_manager, get_current_user
from app.utils.errors import AuthenticationException, BadRequestException

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


# ============================================================
# 端点
# ============================================================

@router.post("/login", response_model=dict)
async def login(req: LoginRequest):
    """用户登录，获取 JWT 令牌

    演示账号：
    - admin / esim_admin_2026 （管理员）
    - analyst / esim_analyst_2026 （分析师）
    """
    result = auth_manager.login(req.username, req.password)
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
    return {
        "code": 200,
        "message": "success",
        "data": user,
    }


@router.post("/refresh")
async def refresh_token(user: dict = Depends(get_current_user)):
    """刷新令牌"""
    from app.core.auth import JWTManager
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
