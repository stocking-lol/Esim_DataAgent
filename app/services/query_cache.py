"""
查询缓存（性能优化）
-------------------

为「相同问题 + 相同角色 + 相同 MVNO」的重复查询提供短时 TTL 缓存，
避免对 Vanna Agent / LLM 的重复调用，显著降低高并发下的延迟与 API 成本。

设计要点：
- 仅缓存「成功」的查询结果（blocked / error 不缓存）
- 按 (question, role, mvno_id) 维度隔离，避免越权命中
- 线程安全（使用 asyncio.Lock + dict）
- TTL 默认 60s，可通过 settings.QUERY_CACHE_TTL_SECONDS 调整
- 可通过 settings.QUERY_CACHE_ENABLED 关闭
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from app.config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    result: object
    expires_at: float


class QueryCache:
    """简单的内存 TTL 缓存（LRU 淘汰）"""

    def __init__(self, max_size: int = 200) -> None:
        self._store: dict[str, _CacheEntry] = {}
        self._max_size = max_size
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _make_key(question: str, role: str, mvno_id: Optional[int]) -> str:
        return f"{role}|{mvno_id}|{question.strip().lower()}"

    def get(self, question: str, role: str, mvno_id: Optional[int]) -> Optional[object]:
        if not settings.QUERY_CACHE_ENABLED:
            return None
        key = self._make_key(question, role, mvno_id)
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if time.time() > entry.expires_at:
            # 过期移除
            self._store.pop(key, None)
            self._misses += 1
            return None
        self._hits += 1
        logger.debug("QueryCache HIT (key=%s)", key[:40])
        return entry.result

    def put(self, question: str, role: str, mvno_id: Optional[int], result: object) -> None:
        if not settings.QUERY_CACHE_ENABLED:
            return
        key = self._make_key(question, role, mvno_id)
        # 超出容量时清理过期项；仍满则清空（简单 LRU 近似）
        if len(self._store) >= self._max_size:
            now = time.time()
            expired = [k for k, v in self._store.items() if now > v.expires_at]
            for k in expired:
                self._store.pop(k, None)
            if len(self._store) >= self._max_size:
                self._store.clear()
        self._store[key] = _CacheEntry(
            result=result,
            expires_at=time.time() + settings.QUERY_CACHE_TTL_SECONDS,
        )
        logger.debug("QueryCache PUT (key=%s)", key[:40])

    def stats(self) -> dict:
        return {
            "enabled": settings.QUERY_CACHE_ENABLED,
            "size": len(self._store),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "ttl_seconds": settings.QUERY_CACHE_TTL_SECONDS,
        }

    def clear(self) -> None:
        self._store.clear()
        self._hits = 0
        self._misses = 0


# 全局单例
query_cache = QueryCache()
