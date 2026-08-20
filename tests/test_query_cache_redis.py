"""
Redis 查询缓存测试
=================
覆盖 CacheBackend 双后端与降级策略：

  - RedisCacheBackend：QueryResult 序列化往返 / 未命中 / TTL / 键隔离
  - QueryCache 门面：按配置选后端、Redis 故障自动降级内存（fail-soft）、
    缓存关闭开关
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.query_cache import (
    MemoryCacheBackend,
    QueryCache,
    RedisCacheBackend,
)
from app.services.query_service import QueryResult


def _result(**kw) -> QueryResult:
    base = dict(
        question="本月新增多少用户", sql="SELECT COUNT(*) FROM users",
        row_count=1, data=[{"cnt": 1}], columns=["cnt"],
        summary="1 个新用户",
    )
    base.update(kw)
    return QueryResult(**base)


class TestRedisBackend:
    @pytest.fixture()
    async def fake_redis(self):
        import fakeredis.aioredis
        return fakeredis.aioredis.FakeRedis()

    @pytest.mark.asyncio
    async def test_roundtrip(self, fake_redis):
        """QueryResult 完整往返（含 data/columns/summary）"""
        b = RedisCacheBackend("redis://x", client=fake_redis)
        await b.put("k", _result(), 60)
        got = await b.get("k")
        assert got.question == "本月新增多少用户"
        assert got.sql.startswith("SELECT COUNT")
        assert got.data == [{"cnt": 1}]
        assert got.summary == "1 个新用户"

    @pytest.mark.asyncio
    async def test_miss(self, fake_redis):
        b = RedisCacheBackend("redis://x", client=fake_redis)
        assert await b.get("missing") is None

    @pytest.mark.asyncio
    async def test_ttl_expires(self, fake_redis):
        """TTL=1s 过期后命中失败"""
        b = RedisCacheBackend("redis://x", client=fake_redis)
        await b.put("k", _result(), ttl=1)
        assert await b.get("k") is not None
        import asyncio
        await asyncio.sleep(1.2)
        assert await b.get("k") is None

    @pytest.mark.asyncio
    async def test_key_isolation(self, fake_redis):
        """role/mvno 维度隔离，避免越权命中"""
        b = RedisCacheBackend("redis://x", client=fake_redis)
        await b.put("admin|1|q", _result(question="q"), 60)
        assert await b.get("analyst|1|q") is None
        assert await b.get("admin|2|q") is None
        assert await b.get("admin|1|q") is not None


class TestQueryCacheFacade:
    @pytest.mark.asyncio
    async def test_redis_failure_degrades_to_memory(self):
        """Redis 后端故障 → 自动降级内存，get 返回 None 且主链路不中断"""
        class _BoomBackend:
            async def get(self, key):
                raise ConnectionError("redis down")
            async def put(self, key, v, ttl):
                raise ConnectionError("redis down")
            def clear(self):
                pass

        qc = QueryCache(max_size=10)
        qc._backend = _BoomBackend()
        qc._backend_name = "boom"
        result = await qc.get("q", "admin", None)
        assert result is None
        assert isinstance(qc._backend, MemoryCacheBackend)
        assert qc._degraded is True

    @pytest.mark.asyncio
    async def test_memory_backend_roundtrip(self):
        qc = QueryCache(max_size=10)
        # 强制走内存后端
        qc._backend = MemoryCacheBackend(max_size=10)
        qc._backend_name = "memory"
        await qc.put("q", "admin", None, _result())
        got = await qc.get("q", "admin", None)
        assert got is not None and got.question.startswith("本月")

    @pytest.mark.asyncio
    async def test_memory_backend_ttl(self):
        qc = QueryCache(max_size=10)
        qc._backend = MemoryCacheBackend(max_size=10)
        qc._backend_name = "memory"
        import time
        # 手动写入一个已过期的 entry
        await qc._backend.put("x", _result(), 60)
        qc._backend._store["x"].expires_at = time.time() - 1
        assert await qc.get("x", "admin", None) is None

    def test_stats_reports_backend(self):
        qc = QueryCache(max_size=10)
        st = qc.stats()
        assert "backend" in st
        assert st["enabled"] is True or st["enabled"] is False

    def test_clear_memory_backend(self):
        qc = QueryCache(max_size=10)
        qc._backend = MemoryCacheBackend(max_size=10)
        qc._backend_name = "memory"
        import asyncio
        asyncio.run(qc._backend.put("x", _result(), 60))
        assert qc.stats()["size"] == 1
        qc.clear()
        assert qc.stats()["size"] == 0
