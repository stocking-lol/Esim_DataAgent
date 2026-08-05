"""
限流中间件
---------
基于内存的滑动窗口限流，按 IP 地址限制请求频率。

配置项（从 security.yaml 和 settings 加载）：
  - query: 30 次/分钟（NL2SQL 查询）
  - default: 60 次/分钟（其他 API）
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class SlidingWindow:
    """滑动窗口计数器"""
    timestamps: list[float] = field(default_factory=list)

    def is_allowed(self, now: float, max_requests: int, window_seconds: int) -> bool:
        """检查是否允许请求

        Args:
            now: 当前时间戳
            max_requests: 窗口内最大请求数
            window_seconds: 窗口大小（秒）

        Returns:
            bool: 是否允许
        """
        # 清理过期时间戳
        cutoff = now - window_seconds
        self.timestamps = [t for t in self.timestamps if t > cutoff]

        if len(self.timestamps) >= max_requests:
            return False

        self.timestamps.append(now)
        return True

    def remaining(self, now: float, max_requests: int, window_seconds: int) -> int:
        """返回窗口内剩余配额"""
        cutoff = now - window_seconds
        active = [t for t in self.timestamps if t > cutoff]
        return max(0, max_requests - len(active))


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    API 限流中间件

    按 IP 地址进行限流，不同路径使用不同限制：
    - /api/v1/query: 更严格的限制（默认 30 次/分钟）
    - 其他路径: 默认限制（60 次/分钟）
    """

    def __init__(self, app, query_limit: int = 30, default_limit: int = 60):
        super().__init__(app)
        self.query_limit = query_limit
        self.default_limit = default_limit
        self.window_seconds = 60
        self._windows: dict[str, SlidingWindow] = defaultdict(SlidingWindow)
        self._lock = Lock()
        logger.info(
            "RateLimitMiddleware initialized: query=%d/min, default=%d/min",
            query_limit, default_limit,
        )

    def _get_client_ip(self, request: Request) -> str:
        """获取客户端真实 IP（支持代理转发）"""
        # 优先从 X-Forwarded-For 获取
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        # 其次从 X-Real-IP
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
        # 兜底：直接连接 IP
        return request.client.host if request.client else "unknown"

    def _get_limit_for_path(self, path: str) -> int:
        """根据路径获取限流配额"""
        if "/query" in path:
            return self.query_limit
        return self.default_limit

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # 健康检查和文档不限流
        if request.url.path in ("/health", "/", "/docs", "/openapi.json", "/redoc"):
            return await call_next(request)

        client_ip = self._get_client_ip(request)
        limit = self._get_limit_for_path(request.url.path)
        key = f"{client_ip}:{request.url.path.split('/')[3] if len(request.url.path.split('/')) > 3 else 'default'}"

        now = time.time()

        with self._lock:
            window = self._windows[key]
            allowed = window.is_allowed(now, limit, self.window_seconds)
            remaining = window.remaining(now, limit, self.window_seconds)

        if not allowed:
            logger.warning("Rate limit exceeded: ip=%s, path=%s", client_ip, request.url.path)
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=429,
                content={
                    "code": 429,
                    "message": "请求过于频繁，请稍后再试",
                    "data": {
                        "retry_after_seconds": self.window_seconds,
                        "limit": limit,
                    },
                },
                headers={
                    "Retry-After": str(self.window_seconds),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)

        # 在响应头中添加限流信息
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        return response
