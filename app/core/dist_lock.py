"""
分布式锁
--------
多副本部署时，保证定时任务（sync_ddl / harvest_sql / prune_examples）
同一时刻只有一个副本在执行。

为什么需要
----------
K8s 里 app 是 2 副本，APScheduler 在每个副本进程内各跑一份。若不加锁，
同一个 job 会被执行 N 次。本项目的三个 job 都是"全量幂等 diff"，重复执行
不会写坏数据，但会：

- 重复写 ChromaDB（同一份 DDL 被 add 两次，浪费 embedding 计算）
- 重复发告警（漂移报告刷屏）
- 并发重建同一张表的 DDL 时产生竞态（先删后加之间另一个副本读到空）

为什么不用 Redis 的 SETNX + EXPIRE
----------------------------------
``SETNX`` 成功后若进程在 ``EXPIRE`` 前崩溃，锁将永不过期（死锁）。
必须用单条原子的 ``SET key value NX PX ttl``。

为什么释放要比对 token
----------------------
经典竞态：A 持锁 → A 因 GC/网络卡顿超过 TTL → 锁自动过期 → B 抢到锁
→ A 恢复后执行 DEL，把 **B 的锁删掉** → C 又抢到锁，A 和 C 同时执行。
因此 value 存随机 token，释放时用 Lua 脚本「比对再删」（GET+DEL 分两步仍非原子）。

Redis 不可用时的行为
--------------------
fail-closed：拿不到锁就不执行任务，并记录 warning。理由与项目的 SQL 安全网关
一致 —— 宁可这次不跑（下次自动补，因为 job 是幂等全量 diff），
也不能在未知状态下并发执行。

本地开发（无 Redis 或显式传入 redis=None）时退化为进程内 ``asyncio.Lock``，
仅保证单进程内互斥，并输出 warning 提示这在多副本下无效。

用法::

    async with DistributedLock("job:sync_ddl", ttl_ms=120_000, redis=client) as acquired:
        if not acquired:
            logger.info("另一个副本正在执行，本次跳过")
            return
        await do_sync()
"""

import asyncio
import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 释放锁：比对 token 再删。不能拆成 GET + DEL —— 两步之间锁可能已易主
UNLOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# 续租：同样要比对 token，锁已易主则放弃（返回 0）
RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""

DEFAULT_NAMESPACE = "esim:lock"


class DistributedLock:
    """基于 Redis 的分布式锁（async）

    Args:
        name: 锁名，建议用 ``job:<任务名>``
        ttl_ms: 锁自动过期时间（毫秒）。应显著大于任务最长耗时，
            建议 ``max_duration * 3``，避免任务未跑完锁就过期
        redis: Redis 异步客户端（需支持 ``set(nx=, px=)`` 与 ``eval``）。
            传 ``None`` 时退化为进程内锁，仅供本地开发/单实例使用
        namespace: key 前缀，避免与其他业务 key 冲突
    """

    def __init__(
        self,
        name: str,
        ttl_ms: int = 60_000,
        redis: Optional[Any] = None,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        if ttl_ms <= 0:
            raise ValueError(f"ttl_ms 必须为正数，收到 {ttl_ms}")
        self.name = name
        self.key = f"{namespace}:{name}"
        self.ttl_ms = ttl_ms
        self._redis = redis
        # 每次实例一个唯一 token：区分不同持有者，防止误删他人锁
        self._token = uuid.uuid4().hex
        self._acquired = False
        self._local_lock: Optional[asyncio.Lock] = None

    # --- 属性 ---

    @property
    def token(self) -> str:
        """本次加锁使用的 token（测试与排障用）"""
        return self._token

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def is_distributed(self) -> bool:
        """是否为真正的分布式锁（False 表示退化为进程内锁）"""
        return self._redis is not None

    # --- 核心操作 ---

    async def acquire(self) -> bool:
        """尝试加锁

        Returns:
            True 表示抢到锁，调用方可以执行任务；
            False 表示已被其他副本持有，或 Redis 不可用（fail-closed）
        """
        if self._redis is None:
            # 无 Redis：进程内互斥，仅单实例语义正确
            logger.warning(
                "分布式锁 '%s' 退化为进程内锁（未配置 Redis），"
                "多副本部署下无法保证互斥",
                self.name,
            )
            self._local_lock = self._local_lock or asyncio.Lock()
            await self._local_lock.acquire()
            self._acquired = True
            return True

        try:
            # SET key token NX PX ttl —— 原子操作，杜绝 SETNX+EXPIRE 的死锁窗口
            ok = await self._redis.set(
                self.key, self._token, nx=True, px=self.ttl_ms
            )
        except Exception as e:
            # fail-closed：连不上 Redis 就不执行，而不是无锁并发
            logger.warning(
                "获取分布式锁 '%s' 时 Redis 异常，本次任务跳过: %s",
                self.name, e,
            )
            self._acquired = False
            return False

        self._acquired = bool(ok)
        if self._acquired:
            logger.debug("获取分布式锁 '%s' 成功 (ttl=%dms)", self.name, self.ttl_ms)
        else:
            logger.debug("分布式锁 '%s' 已被其他副本持有，跳过", self.name)
        return self._acquired

    async def release(self) -> bool:
        """释放锁（只有 token 匹配时才真正删除）

        Returns:
            True 表示成功释放；False 表示未持有锁、锁已易主或 Redis 异常
        """
        if not self._acquired:
            return False

        if self._redis is None:
            if self._local_lock is not None:
                self._local_lock.release()
            self._acquired = False
            return True

        try:
            deleted = await self._redis.eval(
                UNLOCK_LUA, 1, self.key, self._token
            )
        except Exception as e:
            # 释放失败不致命：锁有 TTL 会自动过期
            logger.warning(
                "释放分布式锁 '%s' 时 Redis 异常（锁将在 %dms 后自动过期）: %s",
                self.name, self.ttl_ms, e,
            )
            self._acquired = False
            return False

        self._acquired = False
        if deleted:
            logger.debug("释放分布式锁 '%s' 成功", self.name)
        else:
            # 锁已过期并被其他副本抢到 —— 这正是比对 token 要防的场景
            logger.warning(
                "分布式锁 '%s' 释放时 token 不匹配，锁已易主（任务耗时可能超过 TTL %dms）",
                self.name, self.ttl_ms,
            )
        return bool(deleted)

    async def renew(self) -> bool:
        """续租（看门狗用）

        长任务应在执行中周期性调用（间隔 < ttl/3），把 TTL 重置回 ttl_ms。

        Returns:
            True 表示续租成功；False 表示锁已易主，当前持有者应立即停止任务
        """
        if not self._acquired or self._redis is None:
            return False
        try:
            ok = await self._redis.eval(
                RENEW_LUA, 1, self.key, self._token, self.ttl_ms
            )
        except Exception as e:
            logger.warning("续租分布式锁 '%s' 失败: %s", self.name, e)
            return False
        if not ok:
            logger.error(
                "分布式锁 '%s' 续租失败，锁已易主，当前任务应立即停止", self.name
            )
        return bool(ok)

    # --- 上下文管理器 ---

    async def __aenter__(self) -> bool:
        return await self.acquire()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        # 无论任务成功还是抛异常，都要释放锁，否则会占满一个 TTL 周期
        await self.release()
        return False


async def create_redis_client(url: Optional[str] = None) -> Optional[Any]:
    """创建 Redis 异步客户端，失败返回 None（调用方据此退化为进程内锁）

    Args:
        url: Redis URL，默认取 ``settings.REDIS_URL``

    Returns:
        Redis 客户端，或 None（redis 包缺失 / 连接失败）
    """
    try:
        import redis.asyncio as aioredis  # noqa: PLC0415 - 可选依赖，延迟导入
    except ImportError:
        logger.warning("未安装 redis 包，分布式锁不可用")
        return None

    if url is None:
        try:
            from app.config.settings import settings  # noqa: PLC0415
            url = settings.REDIS_URL
        except Exception:
            url = "redis://localhost:6379/0"

    try:
        client = aioredis.from_url(url)
        # 立即探活，避免带着一个连不上的客户端启动
        await client.ping()
        return client
    except Exception as e:
        logger.warning("Redis 不可用（%s），分布式锁将退化为进程内锁", e)
        return None
