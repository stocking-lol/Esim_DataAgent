"""
pytest 全局配置和 fixture
------------------------
提供测试客户端、测试数据库和测试数据的初始化。

测试策略:
  - 使用 httpx.AsyncClient 作为异步测试客户端
  - 使用实际的 MySQL 数据库（esim_platform 测试库）
  - 提供预制的 JWT token 用于认证测试
  - 旁路 Vanna Agent 初始化（可选）
"""

import asyncio
import os
import sys
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# --- 环境变量（测试用） ---
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("JWT_SECRET_KEY", "test_secret_key_for_pytest")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "60")
os.environ.setdefault("ENABLE_METRICS", "true")


@pytest.fixture(scope="session")
def event_loop():
    """全局事件循环"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def app_instance():
    """创建 FastAPI 应用实例（不启动 lifespan）"""
    from app.main import app
    return app


@pytest_asyncio.fixture(scope="session")
async def client(app_instance) -> AsyncGenerator[AsyncClient, None]:
    """异步 HTTP 测试客户端

    自动注入 Authorization header（可选）。
    """
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        timeout=60.0,
    ) as ac:
        yield ac


# --- 认证 fixture ---

@pytest.fixture
def admin_token() -> str:
    """管理员 JWT token"""
    from app.core.auth import JWTManager
    return JWTManager.create_access_token({
        "sub": "1",
        "username": "admin",
        "role": "admin",
    })


@pytest.fixture
def analyst_token() -> str:
    """分析师 JWT token"""
    from app.core.auth import JWTManager
    return JWTManager.create_access_token({
        "sub": "2",
        "username": "analyst",
        "role": "analyst",
    })


@pytest.fixture
def admin_headers(admin_token) -> dict:
    """管理员请求头"""
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def analyst_headers(analyst_token) -> dict:
    """分析师请求头"""
    return {"Authorization": f"Bearer {analyst_token}"}


# --- 数据库 fixture ---

@pytest.fixture
def db_session():
    """数据库会话 fixture（用于直接数据库操作）"""
    from app.config.database import get_raw_db
    session = get_raw_db()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def cleanup_conversations(db_session):
    """每个测试后清理**测试自己创建的**对话数据

    历史教训（2026-08-31）：
        这个 fixture 原来直接执行 ``query(Conversation).delete()``，
        等于**清空整张表**。而它又是 autouse 的，于是每跑一次 pytest，
        开发环境里用户真实创建的对话（连同消息）会被全部抹掉——
        且不可恢复（除非有备份或从 query_audit_log 反推）。

    现在的做法：测试开始前对已存在的对话 id 做快照，结束后只删除
    「本次新增」的部分。既保证测试之间互不污染，又不会误伤存量数据。
    """
    from app.models.conversation import Conversation, ConversationMessage

    # 快照：测试开始前就存在的对话，一律视为存量数据，绝不删除
    pre_existing = {row[0] for row in db_session.query(Conversation.id).all()}

    yield

    try:
        # 必须先结束快照时开启的事务。
        # MySQL 默认隔离级别是 REPEATABLE READ：若沿用同一个事务去查，
        # 将看不到其他会话（API 请求、其他 fixture）在此期间插入的行，
        # 导致 created_ids 恒为空、测试脏数据残留。
        db_session.commit()

        q = db_session.query(Conversation.id)
        if pre_existing:
            q = q.filter(Conversation.id.notin_(pre_existing))
        created_ids = [row[0] for row in q.all()]

        if not created_ids:
            db_session.rollback()
            return

        db_session.query(ConversationMessage).filter(
            ConversationMessage.conversation_id.in_(created_ids)
        ).delete(synchronize_session=False)
        db_session.query(Conversation).filter(
            Conversation.id.in_(created_ids)
        ).delete(synchronize_session=False)
        db_session.commit()
    except Exception:  # pragma: no cover - 清理失败不应让测试误报
        db_session.rollback()
        raise


# --- 工具函数 ---

def assert_success_response(response, expected_code=200):
    """断言成功响应"""
    assert response.status_code == 200, f"HTTP {response.status_code}: {response.text}"
    data = response.json()
    assert data["code"] == expected_code, f"Expected code {expected_code}, got {data['code']}: {data.get('message')}"
    return data


def assert_error_response(response, expected_code):
    """断言错误响应"""
    data = response.json()
    assert data["code"] == expected_code, f"Expected code {expected_code}, got {data['code']}"
    return data
