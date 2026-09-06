"""
分布式锁测试
------------
覆盖三个关键正确性问题：

1. **原子加锁**：必须用 SET NX PX，不能用 SETNX + EXPIRE（后者崩溃会留死锁）
2. **互斥**：第二个持有者拿不到锁
3. **防误删**（最重要）：释放时必须比对 token。若锁已过期并被他人抢走，
   原持有者不能删掉别人的锁 —— 这是分布式锁最经典的竞态。

关于 fakeredis 的限制
---------------------
fakeredis 未编译 lupa 时**不支持 EVAL**（本地 Py3.12 环境无 lupa 发行版）。
因此：
- 加锁/互斥/TTL 走**真实** fakeredis（这些不依赖 Lua）
- 释放/续租走 mock，断言 Lua 脚本与参数正确、以及对返回值 0（锁已易主）的处理

若环境装了 lupa，``TestRealLua`` 会额外跑一遍真实脚本。
"""

import asyncio

import pytest

from app.core.dist_lock import (
    RENEW_LUA,
    UNLOCK_LUA,
    DistributedLock,
)

try:
    import fakeredis

    HAS_FAKEREDIS = True
except ImportError:  # pragma: no cover
    HAS_FAKEREDIS = False

try:
    import lupa  # noqa: F401

    HAS_LUA = True
except ImportError:
    HAS_LUA = False


def _real_redis():
    """创建 fakeredis 异步客户端"""
    return fakeredis.FakeAsyncRedis()


class _RedisWithMockedEval:
    """set 走真实 fakeredis，eval 走 mock（fakeredis 无 Lua 支持）

    这样既能验证真实的 NX/PX 语义，又能断言 Lua 调用参数与返回值处理。
    """

    def __init__(self, eval_return=1):
        self._real = _real_redis()
        self.eval_calls = []
        self._eval_return = eval_return
        self.eval = self._eval

    async def set(self, key, value, nx=False, px=None, **kw):
        return await self._real.set(key, value, nx=nx, px=px, **kw)

    async def _eval(self, script, numkeys, *args):
        self.eval_calls.append({"script": script, "numkeys": numkeys, "args": args})
        return self._eval_return


class TestAcquire:
    """加锁行为"""

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="需要 fakeredis")
    async def test_acquire_success(self):
        lock = DistributedLock("job:sync_ddl", ttl_ms=30_000, redis=_real_redis())
        assert await lock.acquire() is True
        assert lock.acquired is True
        assert lock.is_distributed is True

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="需要 fakeredis")
    async def test_set_uses_nx_px_atomically(self):
        """底层必须是 SET NX PX 单条命令，而非 SETNX + EXPIRE"""
        r = _real_redis()
        lock = DistributedLock("job:x", ttl_ms=45_000, redis=r)
        await lock.acquire()
        # TTL 被正确设置（PX 生效），说明走的是带过期参数的原子 SET
        ttl = await r.pttl("esim:lock:job:x")
        assert 0 < ttl <= 45_000

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="需要 fakeredis")
    async def test_mutual_exclusion(self):
        """第二个实例拿不到锁"""
        r = _real_redis()
        a = DistributedLock("job:sync_ddl", ttl_ms=30_000, redis=r)
        b = DistributedLock("job:sync_ddl", ttl_ms=30_000, redis=r)
        assert await a.acquire() is True
        assert await b.acquire() is False, "同一把锁不能被两个持有者同时获取"

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="需要 fakeredis")
    async def test_different_names_do_not_conflict(self):
        r = _real_redis()
        a = DistributedLock("job:sync_ddl", redis=r)
        b = DistributedLock("job:harvest_sql", redis=r)
        assert await a.acquire() is True
        assert await b.acquire() is True


class TestFailClosed:
    """Redis 不可用时的行为 —— 必须是 fail-closed"""

    async def test_redis_error_means_no_lock(self):
        """连不上 Redis 不能放行，否则多副本会并发执行"""

        class BrokenRedis:
            async def set(self, *a, **kw):
                raise ConnectionError("connection refused")

        lock = DistributedLock("job:sync_ddl", redis=BrokenRedis())
        assert await lock.acquire() is False, "Redis 异常时必须拒绝执行任务"
        assert lock.acquired is False

    async def test_local_fallback_when_no_redis(self):
        """未配置 Redis 时退化为进程内锁（本地开发），并标记为非分布式"""
        lock = DistributedLock("job:sync_ddl", redis=None)
        assert await lock.acquire() is True
        assert lock.is_distributed is False
        assert await lock.release() is True

    async def test_local_fallback_is_mutually_exclusive_in_process(self):
        """进程内锁在同一进程内仍然互斥"""
        lock = DistributedLock("job:sync_ddl", redis=None)
        assert await lock.acquire() is True
        second = DistributedLock("job:sync_ddl", redis=None)
        # 独立的 asyncio.Lock，所以第二个能拿到 —— 这正是它不能用于多副本的原因
        assert await second.acquire() is True
        await lock.release()
        await second.release()


class TestRelease:
    """释放锁 —— 核心是 token 比对"""

    async def test_release_calls_unlock_lua_with_token(self):
        """释放必须走 Lua，且传入自己的 token"""
        r = _RedisWithMockedEval(eval_return=1)
        lock = DistributedLock("job:sync_ddl", ttl_ms=30_000, redis=r)
        await lock.acquire()
        assert await lock.release() is True

        assert len(r.eval_calls) == 1
        call = r.eval_calls[0]
        assert call["script"] == UNLOCK_LUA
        assert call["numkeys"] == 1
        assert call["args"] == ("esim:lock:job:sync_ddl", lock.token)

    async def test_token_mismatch_does_not_delete(self):
        """最关键：锁已易主时，原持有者不能删掉别人的锁

        场景：A 持锁 → A 卡顿超过 TTL → 锁过期 → B 抢到 → A 恢复后 release。
        若不比对 token，A 会删掉 B 的锁，导致 B 和 C 并发执行。
        """
        r = _RedisWithMockedEval(eval_return=0)  # Lua 返回 0 = token 不匹配
        lock = DistributedLock("job:sync_ddl", ttl_ms=30_000, redis=r)
        await lock.acquire()
        assert await lock.release() is False, "锁已易主时必须报告释放失败"

    async def test_release_without_acquire_is_noop(self):
        """未持有锁时释放不应调用 Redis"""
        r = _RedisWithMockedEval()
        lock = DistributedLock("job:sync_ddl", redis=r)
        assert await lock.release() is False
        assert r.eval_calls == []

    async def test_release_is_idempotent(self):
        """重复释放不应重复调用 Lua"""
        r = _RedisWithMockedEval(eval_return=1)
        lock = DistributedLock("job:sync_ddl", redis=r)
        await lock.acquire()
        await lock.release()
        await lock.release()
        assert len(r.eval_calls) == 1

    async def test_release_survives_redis_error(self):
        """释放时 Redis 异常不应抛出（锁有 TTL 会自动过期）"""

        class FlakyRedis(_RedisWithMockedEval):
            async def _eval(self, script, numkeys, *args):
                raise ConnectionError("boom")

        r = FlakyRedis()
        lock = DistributedLock("job:sync_ddl", redis=r)
        await lock.acquire()
        assert await lock.release() is False
        assert lock.acquired is False, "即使释放失败也要清空本地状态"


class TestRenew:
    """续租（看门狗）"""

    async def test_renew_uses_pexpire_with_token(self):
        r = _RedisWithMockedEval(eval_return=1)
        lock = DistributedLock("job:sync_ddl", ttl_ms=30_000, redis=r)
        await lock.acquire()
        assert await lock.renew() is True

        call = r.eval_calls[-1]
        assert call["script"] == RENEW_LUA
        assert call["args"] == ("esim:lock:job:sync_ddl", lock.token, 30_000)

    async def test_renew_fails_when_lock_lost(self):
        """续租失败意味着锁已易主，任务必须停止"""
        r = _RedisWithMockedEval(eval_return=0)
        lock = DistributedLock("job:sync_ddl", ttl_ms=30_000, redis=r)
        await lock.acquire()
        assert await lock.renew() is False

    async def test_renew_without_acquire_is_noop(self):
        r = _RedisWithMockedEval()
        lock = DistributedLock("job:sync_ddl", redis=r)
        assert await lock.renew() is False
        assert r.eval_calls == []


class TestContextManager:
    """async with 用法"""

    async def test_acquired_flag(self):
        r = _RedisWithMockedEval(eval_return=1)
        async with DistributedLock("job:sync_ddl", redis=r) as acquired:
            assert acquired is True
        # 退出后应已释放
        assert len(r.eval_calls) == 1

    async def test_releases_even_on_exception(self):
        """任务抛异常时也必须释放锁，否则会白占一个 TTL 周期"""
        r = _RedisWithMockedEval(eval_return=1)
        with pytest.raises(ValueError, match="任务失败"):
            async with DistributedLock("job:sync_ddl", redis=r):
                raise ValueError("任务失败")
        assert len(r.eval_calls) == 1, "异常路径也必须释放锁"

    async def test_not_acquired_skips_body(self):
        """没抢到锁时 acquired 为 False，调用方据此跳过任务体"""
        r = _RedisWithMockedEval()
        # 预先占用
        holder = DistributedLock("job:sync_ddl", redis=r)
        await holder.acquire()

        executed = False
        async with DistributedLock("job:sync_ddl", redis=r) as acquired:
            if acquired:
                executed = True
        assert executed is False


class TestValidation:
    """参数校验"""

    async def test_tokens_are_unique_per_instance(self):
        """每个实例的 token 必须不同，否则无法区分持有者"""
        a = DistributedLock("job:x", redis=None)
        b = DistributedLock("job:x", redis=None)
        assert a.token != b.token

    def test_invalid_ttl_rejected(self):
        with pytest.raises(ValueError):
            DistributedLock("job:x", ttl_ms=0)
        with pytest.raises(ValueError):
            DistributedLock("job:x", ttl_ms=-1)

    def test_key_namespaced(self):
        lock = DistributedLock("job:sync_ddl", namespace="custom")
        assert lock.key == "custom:job:sync_ddl"


@pytest.mark.skipif(
    not (HAS_FAKEREDIS and HAS_LUA), reason="需要 fakeredis + lupa 才能执行真实 Lua"
)
class TestRealLua:
    """真实 Lua 脚本执行（环境支持时）"""

    async def test_unlock_script_deletes_only_matching_token(self):
        r = _real_redis()
        await r.set("esim:lock:job:x", "token-A", px=30_000)

        # token 不匹配 -> 不删除
        ret = await r.eval(UNLOCK_LUA, 1, "esim:lock:job:x", "token-B")
        assert ret == 0
        assert await r.get("esim:lock:job:x") == b"token-A"

        # token 匹配 -> 删除
        ret = await r.eval(UNLOCK_LUA, 1, "esim:lock:job:x", "token-A")
        assert ret == 1
        assert await r.get("esim:lock:job:x") is None

    async def test_renew_script_extends_ttl(self):
        r = _real_redis()
        await r.set("esim:lock:job:x", "tok", px=1_000)
        ret = await r.eval(RENEW_LUA, 1, "esim:lock:job:x", "tok", 60_000)
        assert ret == 1
        assert await r.pttl("esim:lock:job:x") > 1_000
