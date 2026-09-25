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


# ============================================================
# 内存后端 LRU 淘汰（修复"超容量即全清"雪崩）
# ============================================================

@pytest.mark.asyncio
async def test_memory_backend_lru_evicts_oldest_not_clear():
    """超容量时必须逐个淘汰最久未访问者，不得整体清空。

    回归护栏：原实现在超容量时 `self._store.clear()` 全量清空，
    造成周期性缓存雪崩（所有缓存同时失效 → 请求瞬间全部回源）。
    """
    be = MemoryCacheBackend(max_size=2)
    await be.put("k1", _result(question="q1"), ttl=60)
    await be.put("k2", _result(question="q2"), ttl=60)
    await be.put("k3", _result(question="q3"), ttl=60)  # 触发淘汰

    assert be._store.get("k1") is None, "最旧的 k1 应被淘汰"
    assert be._store.get("k2") is not None, "k2 不应被清掉（旧实现会全清）"
    assert be._store.get("k3") is not None, "k3 应写入成功"
    assert len(be._store) == 2, "容量应收敛到 max_size"


@pytest.mark.asyncio
async def test_memory_backend_get_refreshes_lru():
    """命中会刷新 LRU 顺序：被访问过的旧 key 不应优先被淘汰。"""
    be = MemoryCacheBackend(max_size=2)
    await be.put("k1", _result(question="q1"), ttl=60)
    await be.put("k2", _result(question="q2"), ttl=60)
    # 访问 k1，使其成为最近使用
    assert await be.get("k1") is not None
    await be.put("k3", _result(question="q3"), ttl=60)  # 应淘汰 k2

    assert be._store.get("k1") is not None, "被访问过的 k1 应保留"
    assert be._store.get("k2") is None, "此时 k2 才是最久未访问"


@pytest.mark.asyncio
async def test_memory_backend_expired_then_evict():
    """优先回收过期项，不占用 LRU 淘汰名额。"""
    be = MemoryCacheBackend(max_size=2)
    await be.put("old", _result(question="old"), ttl=-1)   # 立即过期
    await be.put("k2", _result(question="q2"), ttl=60)
    await be.put("k3", _result(question="q3"), ttl=60)

    assert be._store.get("old") is None
    assert be._store.get("k2") is not None, "过期项被回收后 k2 不应被淘汰"
    assert be._store.get("k3") is not None


# ============================================================
# 降级可恢复（修复"一次抖动终身内存缓存"）
# ============================================================

@pytest.mark.asyncio
async def test_cache_degrades_then_recovers_after_cooldown(monkeypatch):
    """冷却期到点后应重试 Redis 并恢复，而非永久降级。

    回归护栏：原实现 `_degraded` 只置位不重置，多副本下命中率被稀释。
    """
    import time as _t

    import app.services.query_cache as qc_mod

    c = QueryCache(max_size=10)
    c._backend = MemoryCacheBackend(max_size=10)
    c._backend_name = "MemoryCacheBackend"
    c._degraded = True
    c._redis_expected = True
    c._degraded_at = _t.time() - 10_000  # 冷却期早已过去
    monkeypatch.setattr(settings := qc_mod.settings, "QUERY_CACHE_REDIS_RETRY_SECONDS", 30)

    import fakeredis.aioredis
    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(
        qc_mod, "RedisCacheBackend",
        lambda url: RedisCacheBackend(url, client=fake),
    )

    await c._maybe_recover()

    assert c._degraded is False, "Redis 恢复后应取消降级标记"
    assert isinstance(c._backend, RedisCacheBackend), "应切回 Redis 后端"


@pytest.mark.asyncio
async def test_cache_stays_degraded_within_cooldown(monkeypatch):
    """冷却期内不得重试（避免每个请求都去撞连接超时）。"""
    import time as _t

    import app.services.query_cache as qc_mod

    c = QueryCache(max_size=10)
    c._backend = MemoryCacheBackend(max_size=10)
    c._degraded = True
    c._redis_expected = True
    c._degraded_at = _t.time()  # 刚降级

    called = []

    def _should_not_be_called(url):
        called.append(url)
        raise AssertionError("冷却期内不应重建 Redis 后端")

    monkeypatch.setattr(qc_mod, "RedisCacheBackend", _should_not_be_called)

    await c._maybe_recover()

    assert c._degraded is True
    assert not called, "冷却期内不应尝试连接 Redis"


@pytest.mark.asyncio
async def test_memory_mode_never_recovers(monkeypatch):
    """QUERY_CACHE_BACKEND=memory 时不应尝试恢复 Redis。"""
    import app.services.query_cache as qc_mod

    c = QueryCache(max_size=10)
    c._degraded = True
    c._redis_expected = False  # 配置本就期望内存

    called = []
    monkeypatch.setattr(qc_mod, "RedisCacheBackend", lambda url: called.append(url))

    await c._maybe_recover()

    assert not called, "纯内存模式不应尝试连接 Redis"
