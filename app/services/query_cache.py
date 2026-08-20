"""
查询缓存（性能优化）
-------------------

为「相同问题 + 相同角色 + 相同 MVNO」的重复查询提供短时 TTL 缓存，
避免对 Vanna Agent / LLM 的重复调用，显著降低高并发下的延迟与 API 成本。

设计要点：
- 仅缓存「成功」的查询结果（blocked / error 不缓存）
- 按 (question, role, mvno_id) 维度隔离，避免越权命中
- **双后端**：MemoryCacheBackend（进程内 dict，LRU 近似）与
  RedisCacheBackend（redis.asyncio + JSON 序列化，跨实例共享）
- **降级策略（fail-soft）**：Redis 连接/执行异常时自动降级为内存缓存，
  缓存故障不阻断主链路（与 RAG 检索失败降级的设计哲学一致）
- 后端选择：settings.QUERY_CACHE_BACKEND = memory / redis / auto
  （auto = 优先 Redis，失败降级 memory）
- TTL 默认 60s；QUERY_CACHE_ENABLED 可全局关闭
"""

import json
import logging
import time
from abc import ABC, abstractmethod
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
    """进程内 TTL 缓存（LRU 近似），单实例部署时的默认后端"""

    def __init__(self, max_size: int = 200) -> None:
        self._store: dict[str, _CacheEntry] = {}
        self._max_size = max_size
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
        self._hits += 1
        logger.debug("Cache HIT (mem, key=%s)", key[:40])
        return entry.result

    async def put(self, key: str, result: object, ttl: int) -> None:
        if len(self._store) >= self._max_size:
            now = time.time()
            expired = [k for k, v in self._store.items() if now > v.expires_at]
            for k in expired:
                self._store.pop(k, None)
            if len(self._store) >= self._max_size:
                self._store.clear()
        self._store[key] = _CacheEntry(
            result=result,
            expires_at=time.time() + ttl,
        )
        logger.debug("Cache PUT (mem, key=%s)", key[:40])

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
        data = asdict(result) if isinstance(result, QueryResult) else result
        return json.dumps(data, ensure_ascii=False, default=str)

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

    def _build_backend(self, name: str) -> CacheBackend:
        if name in ("redis", "auto"):
            # auto/redis 都先尝试 Redis；auto 失败降级，redis 显式失败也降级（fail-soft）
            try:
                return RedisCacheBackend(settings.REDIS_URL)
            except Exception as e:
                logger.warning("Redis backend init failed (%s), using memory", e)
        return MemoryCacheBackend(max_size=self._max_size)

    async def get(self, question: str, role: str, mvno_id: Optional[int]) -> Optional[object]:
        if not settings.QUERY_CACHE_ENABLED:
            return None
        key = self._make_key(question, role, mvno_id)
        try:
            return await self._backend.get(key)
        except Exception as e:
            await self._degrade(e, "get")

    async def _degrade(self, e: Exception, op: str) -> None:
        """切换为内存后端（fail-soft）"""
        if not self._degraded:
            logger.warning(
                "Cache backend %s failed on %s (%s), degrading to memory",
                self._backend_name, op, e)
            self._backend = MemoryCacheBackend(max_size=self._max_size)
            self._degraded = True

    async def put(self, question: str, role: str, mvno_id: Optional[int], result: object) -> None:
        if not settings.QUERY_CACHE_ENABLED:
            return
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
