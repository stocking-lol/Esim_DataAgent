"""
查询缓存（性能优化）
-------------------

为「相同问题 + 相同角色 + 相同 MVNO」的重复查询提供短时 TTL 缓存，
避免对 Vanna Agent / LLM 的重复调用，显著降低高并发下的延迟与 API 成本。

设计要点：
- 仅缓存「成功」的查询结果（blocked / error 不缓存）
- 按 (question, role, mvno_id) 维度隔离，避免越权命中
- **双后端**：MemoryCacheBackend（进程内 OrderedDict，真 LRU 淘汰）与
  RedisCacheBackend（redis.asyncio + JSON 序列化，跨实例共享）
- **降级策略（fail-soft + 可恢复）**：Redis 连接/执行异常时自动降级为内存缓存，
  缓存故障不阻断主链路；降级后按 QUERY_CACHE_REDIS_RETRY_SECONDS 冷却期重试
  并 ping 探活，恢复后自动切回 Redis（早期实现只降不恢复，多副本下命中率被稀释）
- 后端选择：settings.QUERY_CACHE_BACKEND = memory / redis / auto
  （auto = 优先 Redis，失败降级 memory）
- TTL 默认 60s；QUERY_CACHE_ENABLED 可全局关闭

命中语义：key 为 `role|mvno_id|question.strip().lower()` 的**精确匹配**
（不含 conversation_id，不做语义相似）；value 为整个 QueryResult 的 JSON。
提升命中率需要问题归一化 + 向量相似检索，见 docs/pitfalls.md 相关讨论。
"""

import json
import logging
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Optional

from app.config.settings import settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "qc:"          # Redis key 前缀，避免与其他业务 key 冲突


@dataclass
class _CacheEntry:
    result: object
    expires_at: float


# ============================================================
# 后端抽象
# ============================================================

class CacheBackend(ABC):
    """缓存后端抽象（memory / redis 统一接口）"""

    @abstractmethod
    async def get(self, key: str) -> Optional[object]:
        """读取缓存；未命中或过期返回 None"""

    @abstractmethod
    async def put(self, key: str, result: object, ttl: int) -> None:
        """写入缓存（带 TTL）"""

    def clear(self) -> None:
        """清空（内存后端生效；Redis 后端不主动清库）"""


class MemoryCacheBackend(CacheBackend):
    """进程内 TTL 缓存（LRU 淘汰），单实例部署或 Redis 降级时的后端

    淘汰策略：先回收过期项；仍超容量则按 **LRU 逐个淘汰**最久未访问的条目。

    注意：早期实现在超容量时直接 ``clear()`` 全量清空，会造成周期性缓存雪崩
    （所有缓存同时失效，请求瞬间全部回源打向下游），已改为逐个 LRU 淘汰，
    使缓存容量平滑收敛而不是断崖式失效。
    """

    def __init__(self, max_size: int = 200) -> None:
        self._store: "OrderedDict[str, _CacheEntry]" = OrderedDict()
        self._max_size = max(1, max_size)
        self._hits = 0
        self._misses = 0

    async def get(self, key: str) -> Optional[object]:
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if time.time() > entry.expires_at:
            self._store.pop(key, None)
            self._misses += 1
            return None
        # 命中即刷新为最近使用（LRU 语义）
        self._store.move_to_end(key)
        self._hits += 1
        logger.debug("Cache HIT (mem, key=%s)", key[:40])
        return entry.result

    async def put(self, key: str, result: object, ttl: int) -> None:
        now = time.time()
        # 1) 优先回收已过期条目（不占用 LRU 淘汰名额）
        if len(self._store) >= self._max_size:
            expired = [k for k, v in self._store.items() if now > v.expires_at]
            for k in expired:
                self._store.pop(k, None)
        # 2) 写入并标记为最近使用
        self._store[key] = _CacheEntry(result=result, expires_at=now + ttl)
        self._store.move_to_end(key)
        # 3) 仍超容量 → 逐个淘汰最久未访问者，而非整体清空
        while len(self._store) > self._max_size:
            evicted, _ = self._store.popitem(last=False)
            logger.debug("Cache EVICT (mem, lru, key=%s)", evicted[:40])

    def clear(self) -> None:
        self._store.clear()
        self._hits = 0
        self._misses = 0

    def stats(self) -> dict:
        return {
            "backend": "memory",
            "size": len(self._store),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
        }


class RedisCacheBackend(CacheBackend):
    """Redis TTL 缓存（跨实例共享，多副本部署时缓存一致）"""

    def __init__(self, url: str, client=None) -> None:
        if client is not None:
            self._client = client
        else:
            import redis.asyncio
            self._client = redis.asyncio.from_url(url)
        self._hits = 0
        self._misses = 0

    # --- 序列化（QueryResult -> JSON -> bytes） ---

    @staticmethod
    def _serialize(result: object) -> str:
        # 延迟导入避免与 query_service 循环依赖
        from app.services.query_service import QueryResult
        from app.utils.json_encoder import dumps_json
        data = asdict(result) if isinstance(result, QueryResult) else result
        # 必须用 dumps_json 而非 default=str：后者会把 DECIMAL 金额 19.90
        # 序列化成字符串 "19.90"，缓存回读后前端无法按数值排序或绘图
        return dumps_json(data)

    @staticmethod
    def _deserialize(raw: str) -> object:
        from app.services.query_service import QueryResult
        return QueryResult(**json.loads(raw))

    # --- 接口 ---

    async def get(self, key: str) -> Optional[object]:
        raw = await self._client.get(_KEY_PREFIX + key)
        if raw is None:
            self._misses += 1
            return None
        try:
            self._hits += 1
            logger.debug("Cache HIT (redis, key=%s)", key[:40])
            return self._deserialize(raw.decode("utf-8"))
        except Exception as e:
            logger.warning("Cache deserialize error, treat as miss: %s", e)
            self._misses += 1
            return None

    async def put(self, key: str, result: object, ttl: int) -> None:
        await self._client.set(
            _KEY_PREFIX + key, self._serialize(result), ex=ttl)
        logger.debug("Cache PUT (redis, key=%s)", key[:40])

    def stats(self) -> dict:
        return {
            "backend": "redis",
            "size": "n/a",
            "max_size": "n/a",
            "hits": self._hits,
            "misses": self._misses,
        }


# ============================================================
# 门面：按配置选择后端 + 降级
# ============================================================

class QueryCache:
    """查询缓存门面：get/put 委托后端，Redis 故障时自动降级内存"""

    def __init__(self, max_size: int = 200) -> None:
        self._max_size = max_size
        self._backend: CacheBackend = self._build_backend(
            settings.QUERY_CACHE_BACKEND)
        self._backend_name = type(self._backend).__name__
        self._degraded = False
        self._degraded_at: float = 0.0
        # 配置期望使用 Redis 时才做恢复尝试（纯 memory 模式无需重试）
        self._redis_expected = settings.QUERY_CACHE_BACKEND in ("redis", "auto")

    def _build_backend(self, name: str) -> CacheBackend:
        if name in ("redis", "auto"):
            # auto/redis 都先尝试 Redis；auto 失败降级，redis 显式失败也降级（fail-soft）
            try:
                return RedisCacheBackend(settings.REDIS_URL)
            except Exception as e:
                logger.warning("Redis backend init failed (%s), using memory", e)
        return MemoryCacheBackend(max_size=self._max_size)

    async def _maybe_recover(self) -> None:
        """降级后按冷却期重试 Redis，恢复跨实例共享缓存。

        早期实现一旦降级就永不恢复（``_degraded`` 只置位不重置），
        Redis 短暂抖动会让该进程余生都走进程内缓存 —— 多副本下各 Pod 缓存
        互不可见，命中率被稀释且失效不同步。现改为冷却期后重试并 ping 探活。
        """
        if not self._degraded or not self._redis_expected:
            return
        retry_after = settings.QUERY_CACHE_REDIS_RETRY_SECONDS
        if retry_after <= 0:
            return
        if time.time() - self._degraded_at < retry_after:
            return
        # 冷却期已到：尝试重建并探活
        try:
            backend = RedisCacheBackend(settings.REDIS_URL)
            await backend._client.ping()
            self._backend = backend
            self._backend_name = type(backend).__name__
            self._degraded = False
            logger.info("Cache backend recovered to Redis")
        except Exception as e:
            # 仍不可用：顺延下次重试时间，避免每个请求都去撞超时
            self._degraded_at = time.time()
            logger.debug("Cache backend still unavailable: %s", e)

    async def get(self, question: str, role: str, mvno_id: Optional[int]) -> Optional[object]:
        if not settings.QUERY_CACHE_ENABLED:
            return None
        await self._maybe_recover()
        key = self._make_key(question, role, mvno_id)
        try:
            return await self._backend.get(key)
        except Exception as e:
            await self._degrade(e, "get")

    async def _degrade(self, e: Exception, op: str) -> None:
        """切换为内存后端（fail-soft），并记录降级时间用于后续恢复重试"""
        if not self._degraded:
            logger.warning(
                "Cache backend %s failed on %s (%s), degrading to memory",
                self._backend_name, op, e)
            self._backend = MemoryCacheBackend(max_size=self._max_size)
            self._backend_name = type(self._backend).__name__
            self._degraded = True
            self._degraded_at = time.time()

    async def put(self, question: str, role: str, mvno_id: Optional[int], result: object) -> None:
        if not settings.QUERY_CACHE_ENABLED:
            return
        await self._maybe_recover()
        key = self._make_key(question, role, mvno_id)
        try:
            await self._backend.put(key, result, settings.QUERY_CACHE_TTL_SECONDS)
        except Exception as e:
            await self._degrade(e, "put")

    @staticmethod
    def _make_key(question: str, role: str, mvno_id: Optional[int]) -> str:
        return f"{role}|{mvno_id}|{question.strip().lower()}"

    def stats(self) -> dict:
        base = {
            "enabled": settings.QUERY_CACHE_ENABLED,
            "ttl_seconds": settings.QUERY_CACHE_TTL_SECONDS,
            "degraded": self._degraded,
        }
        try:
            base.update(self._backend.stats())
        except Exception:
            base["backend"] = "unknown"
        return base

    def clear(self) -> None:
        """清空内存后端（测试用；Redis 后端不主动清库）"""
        try:
            self._backend.clear()
        except Exception as e:
            logger.warning("Cache clear error: %s", e)

    async def aclose(self) -> None:
        """关闭后端连接（服务退出时调用）"""
        if isinstance(self._backend, RedisCacheBackend):
            try:
                await self._backend._client.aclose()
            except Exception as e:
                logger.warning("Redis cache close error: %s", e)


# 全局单例
query_cache = QueryCache()
