"""
ResilientOpenAILlmService 重连机制测试
======================================
验证 Vanna Agent 生产路径的 LLM 调用具备 jittered 重连：

  - stream_request（流式，核心 SQL 生成路径）：
    建立流阶段连接错误 → 抖动重试 → 流建立成功后正常产出
  - send_request（非流式）：整体重试
  - 瞬态错误（连接/限流）→ 重试；其他错误（鉴权/4xx/5xx）→ 不重试
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import APIConnectionError, RateLimitError

from app.core.vanna_instance import ResilientOpenAILlmService


def _make_service(create_side_effect):
    """构造 ResilientOpenAILlmService，用 mock 覆盖其 OpenAI 客户端"""
    svc = ResilientOpenAILlmService.__new__(ResilientOpenAILlmService)
    svc._random = __import__("random").Random(7)
    svc.model = "test-model"
    client = MagicMock()
    client.chat.completions.create = MagicMock(side_effect=create_side_effect)
    svc._client = client
    return svc


def _req():
    """构造一个最小 LlmRequest"""
    from vanna.core.llm import LlmMessage, LlmRequest
    from vanna.core.user import User
    return LlmRequest(
        user=User(id="tester", username="tester", email="t@t.local",
                  group_memberships=["admin"], metadata={}),
        messages=[LlmMessage(role="user", content="测试")],
    )


def _chunk(content=None, finish=None):
    """构造最小流事件（仅含 stream_request 用到的属性）"""
    delta = type("D", (), {"content": content, "tool_calls": None})()
    choice = type("C", (), {"delta": delta, "finish_reason": finish})()
    return type("E", (), {"choices": [choice]})()


class TestResilientStreamRequest:
    @pytest.mark.asyncio
    async def test_connection_error_retries_then_streams(self):
        """建立流时前 2 次连接失败 → 抖动重试 → 第 3 次成功并正常产出文本"""
        import httpx

        stream_events = [_chunk(content="SELECT "), _chunk(content="1"),
                         _chunk(finish="stop")]
        _httpx_req = httpx.Request("POST", "http://x")
        err = APIConnectionError(message="Connection error.", request=_httpx_req)

        svc = _make_service([err, err, iter(stream_events)])

        async def fake_sleep(secs):
            assert 0.0 <= secs  # 抖动值非负
            return

        with patch("app.core.vanna_instance.asyncio.sleep", fake_sleep):
            chunks = []
            async for c in svc.stream_request(_req()):
                chunks.append(c)
        assert svc._client.chat.completions.create.call_count == 3
        texts = [c.content for c in chunks if c.content]
        assert "".join(texts) == "SELECT 1"

    @pytest.mark.asyncio
    async def test_all_attempts_fail_raises(self):
        """持续连接失败 → 3 次尝试后抛出（不吞错）"""
        import httpx

        _httpx_req = httpx.Request("POST", "http://x")
        err = APIConnectionError(message="Connection error.", request=_httpx_req)
        svc = _make_service([err, err, err])

        async def fake_sleep(secs):
            return

        with patch("app.core.vanna_instance.asyncio.sleep", fake_sleep):
            with pytest.raises(APIConnectionError):
                async for _ in svc.stream_request(_req()):
                    pass
        assert svc._client.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_non_transient_error_no_retry(self):
        """非瞬态错误（认证失败等）→ 不重试，直接抛出"""
        import httpx
        from openai import AuthenticationError

        _httpx_req = httpx.Request("POST", "http://x")
        svc = _make_service([AuthenticationError(
            "bad key", response=httpx.Response(401, request=_httpx_req),
            body=None)])

        async def fake_sleep(secs):
            return

        with patch("app.core.vanna_instance.asyncio.sleep", fake_sleep):
            with pytest.raises(AuthenticationError):
                async for _ in svc.stream_request(_req()):
                    pass
        assert svc._client.chat.completions.create.call_count == 1


class TestResilientSendRequest:
    @pytest.mark.asyncio
    async def test_rate_limit_then_success(self):
        """限流 → 抖动重试 → 第二次成功"""
        import httpx

        from vanna.core.llm import LlmResponse

        ok = LlmResponse(content="SELECT 1", tool_calls=None, finish_reason="stop")
        req = httpx.Request("POST", "http://x")
        err = RateLimitError("429", response=httpx.Response(429, request=req), body=None)

        # super().send_request 内部会调用 _client；直接 mock 父类方法
        svc = _make_service([])
        with patch.object(
            type(svc).__mro__[1], "send_request",
            new=AsyncMock(side_effect=[err, ok]),
        ), patch("app.core.vanna_instance.asyncio.sleep", new=AsyncMock()):
            resp = await svc.send_request(_req())
        assert resp.content == "SELECT 1"
