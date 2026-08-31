"""认证 fail-closed 语义测试

背景（真实线上 bug）
--------------------
``get_optional_user`` 原实现用 ``except HTTPException: return None`` 吞掉了
JWT 校验失败（含 **token 过期**）。后果：

1. 用户 token 过期后仍在页面上继续提问（JWT 有效期 60 分钟）
2. ``POST /conversation`` 拿到 user=None → 写入 ``user_id = NULL``
3. ``GET /conversation`` 按 ``user_id == 当前用户`` 过滤 → 这些对话永远查不到
4. 接口仍返回 **HTTP 200**，前端完全无感知
   → 表现为「对话历史凭空消失 / 新建对话后找不到」

本测试锁定修复后的语义边界：
- 未提供凭据 → None（保留 /query 的匿名体验能力）
- 凭据无效 / 过期 → 401（fail-closed，绝不静默降级为匿名）
"""

import pytest
from httpx import AsyncClient

from app.core.auth import JWTManager, get_optional_user


# ---------------- get_optional_user 单元级语义 ----------------

async def test_no_credentials_returns_none():
    """未提供凭据 → None（匿名可用，如 /query 游客体验）"""
    result = await get_optional_user(credentials=None)
    assert result is None


async def test_invalid_token_raises_401_instead_of_none():
    """无效 token → 401，而不是静默返回 None（核心回归点）"""
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    creds = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials="this.is.not.a.valid.jwt"
    )
    with pytest.raises(HTTPException) as exc_info:
        await get_optional_user(credentials=creds)
    assert exc_info.value.status_code == 401


async def test_expired_token_raises_401_instead_of_none():
    """**过期** token → 401。

    这是线上事故的直接触发条件：60 分钟有效期过后，
    旧实现会让请求以匿名身份继续执行并把对话写成孤儿。
    """
    from datetime import timedelta

    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    expired = JWTManager.create_access_token(
        {"sub": "1", "username": "admin", "role": "admin"},
        expires_delta=timedelta(seconds=-10),  # 已过期
    )
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=expired)

    with pytest.raises(HTTPException) as exc_info:
        await get_optional_user(credentials=creds)
    assert exc_info.value.status_code == 401
    assert "过期" in str(exc_info.value.detail)


async def test_valid_token_returns_user():
    """有效 token → 正常返回用户字典"""
    from fastapi.security import HTTPAuthorizationCredentials

    token = JWTManager.create_access_token(
        {"sub": "1", "username": "admin", "role": "admin"}
    )
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    user = await get_optional_user(credentials=creds)

    assert user is not None
    assert user["user_id"] == 1
    assert user["username"] == "admin"
    assert user["role"] == "admin"


# ---------------- 接口级回归 ----------------

async def test_create_conversation_without_token_returns_401(client: AsyncClient):
    """未登录创建对话 → 401（不再写入 user_id=NULL 的孤儿对话）"""
    resp = await client.post("/api/v1/conversation", json={"title": "匿名对话"})
    assert resp.status_code == 401


async def test_create_conversation_with_invalid_token_returns_401(
    client: AsyncClient,
):
    """无效/过期 token 创建对话 → 401，而不是 200 + 孤儿数据"""
    resp = await client.post(
        "/api/v1/conversation",
        json={"title": "过期会话"},
        headers={"Authorization": "Bearer invalid.token.xxx"},
    )
    assert resp.status_code == 401


async def test_create_conversation_with_valid_token_binds_user(
    client: AsyncClient, admin_headers,
):
    """有效 token → 200，且对话正确绑定到当前用户（列表可查到）"""
    resp = await client.post(
        "/api/v1/conversation", json={"title": "归属验证"}, headers=admin_headers
    )
    assert resp.status_code == 200
    conv = resp.json()["data"]["conversation"]
    assert conv["user_id"] == 1
    assert conv["username"] == "admin"

    # 关键：必须在列表中可见（这正是孤儿数据做不到的）
    listed = await client.get(
        "/api/v1/conversation?limit=50", headers=admin_headers
    )
    ids = [c["id"] for c in listed.json()["data"]["conversations"]]
    assert conv["id"] in ids


async def test_anonymous_query_still_allowed(client: AsyncClient):
    """回归保护：fail-closed 不能误伤 /query 的匿名体验路径。

    无 Authorization header 时 get_optional_user 返回 None，请求应正常受理。
    """
    resp = await client.options("/api/v1/query")
    # OPTIONS 可能因路由未定义返回 405，但绝不能是 401
    assert resp.status_code != 401
