"""
LLM 重试退避策略测试（指数退避 + 全抖动 Full Jitter）
====================================================
验证高并发安全设计：

  - 抖动范围：delay 恒落在 [0, cap]，cap = min(RETRY_MAX_DELAY, base·2^(attempt-1))
  - 指数增长：attempt 越大 cap 越大（并封顶）
  - 随机散开：多个并发失败的请求重试时机不重合（避免重试风暴）
  - 集成：超时/限流异常触发 jittered 重试，最终成功或抛 LLMException
"""

import random
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.llm import (
    LLMException,
    LLMService,
    MAX_RETRIES,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
)


class TestBackoffDelay:
    """纯函数级：抖动公式的正确性"""

    @pytest.fixture()
    def service(self):
        # 固定种子，保证可复现
        return LLMService(random_source=random.Random(42))

    def test_delay_within_cap(self, service):
        for attempt in (1, 2, 3):
            cap = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** (attempt - 1)))
            for _ in range(200):
                d = service._backoff_delay(attempt)
                assert 0.0 <= d <= cap

    def test_cap_grows_exponentially(self, service):
        d1_cap = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * 1)
        d2_cap = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * 2)
        d3_cap = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * 4)
        assert d1_cap < d2_cap < d3_cap

    def test_cap_ceiling(self, service):
        """退避不无限增长：超过 RETRY_MAX_DELAY 后封顶"""
        cap_10 = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** 9))
        assert cap_10 == RETRY_MAX_DELAY
        for _ in range(100):
            d = service._backoff_delay(10)
            assert d <= RETRY_MAX_DELAY

    def test_jitter_spreads_retries(self):
        """核心：同一 attempt 的多次采样应散开，而非固定同一时刻"""
        service = LLMService(random_source=random.Random(7))
        samples = [service._backoff_delay(1) for _ in range(200)]
        assert len(set(round(s, 3) for s in samples)) > 50, \
            "抖动未生效：所有重试几乎落在同一时刻"

    def test_deterministic_with_seed(self):
        """注入固定种子 → 结果可复现（便于 CI 断言）"""
        a = LLMService(random_source=random.Random(99))
        b = LLMService(random_source=random.Random(99))
        assert [a._backoff_delay(i) for i in (1, 2, 3)] == \
               [b._backoff_delay(i) for i in (1, 2, 3)]


class TestLLMCallRetry:
    """集成级：超时/限流触发 jittered 重试"""

    def _make_service(self, fake_client):
        svc = LLMService(random_source=random.Random(1))
        svc._client = fake_client
        return svc

    @pytest.mark.asyncio
    async def test_timeout_then_success(self):
        """第一次超时 → 等待抖动后重试 → 第二次成功"""
        import asyncio

        import httpx
        from openai import APITimeoutError
        from openai.types.chat import ChatCompletion

        _req = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")

        def _resp():
            # 用 dict 构造（pydantic 会自动校验为 ChatCompletion）
            return ChatCompletion(
                id="x",
                model="deepseek-chat",
                object="chat.completion",
                created=1,
                choices=[{
                    "index": 0,
                    "message": {"role": "assistant", "content": "SELECT 1"},
                    "finish_reason": "stop",
                }],
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

        calls = {"n": 0}

        async def fake_sleep(secs):
            # 校验 sleep 是抖动值（在 [0, base] 内），并把实际时长记下来
            # 注意：不能在此调用 asyncio.sleep——patch 的目标是全局 asyncio
            # 模块（llm.py 与测试共享同一对象），会递归调用 fake_sleep 自身
            assert 0.0 <= secs <= RETRY_BASE_DELAY
            calls["slept"] = secs

        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[APITimeoutError(request=_req), _resp()])
        svc = self._make_service(client)
        with patch("app.core.llm.asyncio.sleep", fake_sleep):
            result = await svc.generate_sql("测试问题", ddl="", documentation="", sql_examples="")
        assert result == "SELECT 1"
        assert "slept" in calls
        assert client.chat.completions.create.await_count == 2

    @pytest.mark.asyncio
    async def test_all_fail_raises(self):
        """持续超时 → 重试 MAX_RETRIES 次后抛 LLMException"""
        import asyncio

        import httpx
        from openai import RateLimitError

        _req = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=RateLimitError(
                "rate limited", response=httpx.Response(429, request=_req),
                body=None))
        svc = self._make_service(client)

        async def fake_sleep(secs):
            return

        with patch("app.core.llm.asyncio.sleep", fake_sleep):
            with pytest.raises(LLMException):
                await svc.generate_sql("问题")
        assert client.chat.completions.create.await_count == MAX_RETRIES

    @pytest.mark.asyncio
    async def test_api_error_no_retry(self):
        """非超时类 API 错误（如 401）直接抛出，不重试（避免浪费调用）"""
        import asyncio

        import httpx
        from openai import APIError

        _req = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=APIError("bad auth", request=_req, body=None))
        svc = self._make_service(client)

        async def fake_sleep(secs):
            return

        with patch("app.core.llm.asyncio.sleep", fake_sleep):
            with pytest.raises(LLMException):
                await svc.generate_sql("问题")
        assert client.chat.completions.create.await_count == 1

    @pytest.mark.asyncio
    async def test_api_error_message_guides_human_intervention(self):
        """API 错误（不可重试）必须带完整信息抛出，提示人工介入，而非静默失败"""
        import asyncio

        import httpx
        from openai import APIError

        _req = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=APIError("Incorrect API key provided", request=_req, body=None))
        svc = self._make_service(client)

        async def fake_sleep(secs):
            return

        with patch("app.core.llm.asyncio.sleep", fake_sleep):
            with pytest.raises(LLMException) as exc:
                await svc.generate_sql("问题")
        assert "人工" in str(exc.value) or "不可自动恢复" in str(exc.value)
        assert "Incorrect API key" in str(exc.value)
        assert client.chat.completions.create.await_count == 1

    @pytest.mark.asyncio
    async def test_connection_refused_retries(self):
        """连接被拒绝（非超时的 APIConnectionError）→ 瞬态，必须重试

        用户场景：『断开 LLM 连接』通常表现为连接被拒绝（ConnectError）而非
        超时——同样属于可重试的瞬态网络故障，不能落入 API 错误分支。
        """
        import asyncio

        import httpx
        from openai import APIConnectionError
        from openai.types.chat import ChatCompletion

        _req = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")

        def _resp():
            return ChatCompletion(
                id="x", model="deepseek-chat", object="chat.completion", created=1,
                choices=[{"index": 0,
                          "message": {"role": "assistant", "content": "SELECT 1"},
                          "finish_reason": "stop"}],
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

        err = APIConnectionError(message="Connection refused", request=_req)
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[err, _resp()])
        svc = self._make_service(client)

        async def fake_sleep(secs):
            assert 0.0 <= secs <= RETRY_BASE_DELAY
            return

        with patch("app.core.llm.asyncio.sleep", fake_sleep):
            result = await svc.generate_sql("问题")
        assert result == "SELECT 1"
        assert client.chat.completions.create.await_count == 2
